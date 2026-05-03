"""Unit tests for :mod:`app.util.forwarded` (cd-ca0u).

Exercises the trusted-proxy seam used by the ``/metrics`` CIDR check
and any future audit / rate-limit consumer: when the TCP peer is in
the trusted-proxy registry we honour the rightmost
``X-Forwarded-For`` entry; otherwise we ignore the header entirely
and treat the peer as the source IP.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Callable

import pytest

from app.util.forwarded import parse_trusted_proxies, resolve_source_ip


def _trusted(
    *entries: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return parse_trusted_proxies(list(entries))


class TestResolveSourceIp:
    def test_resolve_no_peer_returns_none(self) -> None:
        assert resolve_source_ip(None, "1.1.1.1", _trusted("127.0.0.0/8")) is None

    def test_resolve_trusted_peer_takes_rightmost_xff(self) -> None:
        result = resolve_source_ip(
            "127.0.0.1",
            "1.1.1.1, 2.2.2.2, 3.3.3.3",
            _trusted("127.0.0.0/8"),
        )
        assert result == "3.3.3.3"

    def test_resolve_trusted_peer_no_xff_falls_back_to_peer(self) -> None:
        assert (
            resolve_source_ip("127.0.0.1", None, _trusted("127.0.0.0/8")) == "127.0.0.1"
        )

    def test_resolve_trusted_peer_empty_xff_falls_back_to_peer(self) -> None:
        # Whitespace-only and bare empty both fall back to the peer.
        assert (
            resolve_source_ip("127.0.0.1", "", _trusted("127.0.0.0/8")) == "127.0.0.1"
        )
        assert (
            resolve_source_ip("127.0.0.1", "   ", _trusted("127.0.0.0/8"))
            == "127.0.0.1"
        )

    def test_resolve_untrusted_peer_ignores_xff(self) -> None:
        # 203.0.113.0/24 (TEST-NET-3) is outside the trusted set; XFF
        # is ignored entirely — this is the spoof-safe path.
        assert (
            resolve_source_ip("203.0.113.42", "1.1.1.1", _trusted("127.0.0.0/8"))
            == "203.0.113.42"
        )

    def test_resolve_no_trusted_proxies_ignores_xff(self) -> None:
        # Empty registry = no proxies trusted = peer wins.
        assert resolve_source_ip("127.0.0.1", "1.1.1.1", ()) == "127.0.0.1"

    def test_resolve_ipv6_peer_in_ipv6_trusted_cidr(self) -> None:
        result = resolve_source_ip(
            "::1",
            "2001:db8::1",
            _trusted("::1/128"),
        )
        assert result == "2001:db8::1"

    def test_resolve_ipv4_peer_with_ipv6_trusted_only(self) -> None:
        # Cross-family lookup: v4 peer never matches a v6-only registry,
        # so XFF is ignored.
        assert (
            resolve_source_ip("10.0.0.1", "1.1.1.1", _trusted("::1/128")) == "10.0.0.1"
        )

    def test_resolve_xff_with_whitespace(self) -> None:
        result = resolve_source_ip(
            "127.0.0.1",
            "1.1.1.1 ,  2.2.2.2 ",
            _trusted("127.0.0.0/8"),
        )
        assert result == "2.2.2.2"

    def test_resolve_unparseable_peer_treated_as_untrusted(self) -> None:
        # A garbage peer string can't match any CIDR; XFF is ignored
        # and the original peer string is returned (the caller — the
        # /metrics gate — will reject it via its own parse step).
        assert (
            resolve_source_ip("not-an-ip", "1.1.1.1", _trusted("127.0.0.0/8"))
            == "not-an-ip"
        )

    def test_resolve_strips_brackets_around_ipv6_xff_entry(self) -> None:
        # RFC 7239 brackets v6 in ``Forwarded``; some proxies leak the
        # same shape into ``X-Forwarded-For``. The helper unbrackets
        # so the downstream allowlist parser can match.
        result = resolve_source_ip(
            "127.0.0.1",
            "[2001:db8::1]",
            _trusted("127.0.0.0/8"),
        )
        assert result == "2001:db8::1"

    def test_resolve_xff_with_only_commas_falls_back_to_peer(self) -> None:
        # Pathological input: nothing but separators / whitespace.
        # All entries get filtered out and we fall back to the peer.
        assert (
            resolve_source_ip("127.0.0.1", ", ,", _trusted("127.0.0.0/8"))
            == "127.0.0.1"
        )


class TestParseTrustedProxies:
    def test_parse_trusted_proxies_drops_invalid(
        self,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        allow_propagated_log_capture("app.util.forwarded")
        with caplog.at_level(logging.WARNING, logger="app.util.forwarded"):
            networks = parse_trusted_proxies(["127.0.0.0/8", "garbage", "::1/128"])
        assert len(networks) == 2
        assert ipaddress.ip_network("127.0.0.0/8") in networks
        assert ipaddress.ip_network("::1/128") in networks
        assert any(
            "trusted-proxy CIDR entry rejected" in record.message
            and getattr(record, "cidr", None) == "garbage"
            for record in caplog.records
        )

    def test_parse_trusted_proxies_accepts_v4_and_v6(self) -> None:
        networks = parse_trusted_proxies(["10.0.0.0/8", "2001:db8::/32"])
        assert len(networks) == 2
        assert ipaddress.ip_network("10.0.0.0/8") in networks
        assert ipaddress.ip_network("2001:db8::/32") in networks

    def test_parse_trusted_proxies_empty_input(self) -> None:
        assert parse_trusted_proxies([]) == ()
