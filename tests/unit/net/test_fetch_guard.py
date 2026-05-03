"""Unit tests for :mod:`app.net.fetch_guard`.

Covers spec §15 "SSRF" (``docs/specs/15-security-privacy.md`` lines
487-512). Three layers of behaviour:

* Pure SSRF gates: :func:`is_public_ip`, :func:`assert_allowed_scheme`,
  :func:`resolve_public_address`. No I/O — exercised with parametrised
  IP / scheme tables and stub resolvers.
* :class:`PinnedDnsBackend`: rewrites ``connect_tcp(host, ...)`` to
  the pinned IP regardless of the requested host. Tested with a stub
  upstream that records the rewritten host.
* :func:`safe_fetch`: end-to-end pipeline. Uses
  :class:`httpx.MockTransport` for the body / status assertions
  (the SSRF gate fires before the transport is consulted, so the
  reject-private-IP cases use a pure resolver stub and never touch
  the transport).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from unittest.mock import patch

import httpcore
import httpx
import pytest

from app.net.fetch_guard import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    ENV_ALLOW_PRIVATE,
    PUBLIC_FETCH_SCHEMES,
    FetchGuardBlocked,
    FetchGuardError,
    FetchGuardSizeLimit,
    FetchGuardTimeout,
    PinnedDnsBackend,
    assert_allowed_scheme,
    is_public_ip,
    resolve_public_address,
    safe_fetch,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _fixed_resolver(ips: list[str]) -> Any:
    """Resolver stub that always returns ``ips`` regardless of input."""

    def _resolve(host: str, port: int) -> Iterable[str]:
        return list(ips)

    return _resolve


# ---------------------------------------------------------------------------
# is_public_ip
# ---------------------------------------------------------------------------


class TestIsPublicIp:
    """Reject every range an SSRF probe might target."""

    @pytest.mark.parametrize(
        ("label", "ip"),
        [
            # ---- IPv4 ranges ----
            ("loopback-v4", "127.0.0.1"),
            ("loopback-v4-high", "127.255.255.254"),
            ("rfc1918-10", "10.0.0.5"),
            ("rfc1918-10-edge", "10.255.255.255"),
            ("rfc1918-172-low", "172.16.0.1"),
            ("rfc1918-172-mid", "172.20.0.5"),
            ("rfc1918-172-high", "172.31.255.255"),
            ("rfc1918-192", "192.168.1.1"),
            ("link-local-v4", "169.254.169.254"),
            ("multicast-v4", "224.0.0.1"),
            ("multicast-v4-high", "239.255.255.255"),
            ("broadcast", "255.255.255.255"),
            ("unspecified-v4", "0.0.0.0"),
            ("reserved-v4", "240.0.0.1"),
            ("cgnat", "100.64.0.1"),
            ("cgnat-end", "100.127.255.254"),
            # ---- IPv6 ranges ----
            ("loopback-v6", "::1"),
            ("unspecified-v6", "::"),
            ("link-local-v6", "fe80::1"),
            ("rfc4193-unique-local", "fc00::1"),
            ("rfc4193-unique-local-fd", "fd00::1"),
            ("multicast-v6", "ff00::1"),
            ("documentation-v6", "2001:db8::1"),
            # ---- IPv4-mapped IPv6 ----
            ("v4mapped-loopback", "::ffff:127.0.0.1"),
            ("v4mapped-rfc1918", "::ffff:10.0.0.1"),
            # ---- Garbage ----
            ("not-an-ip", "definitely-not-an-ip"),
            ("empty-string", ""),
        ],
    )
    def test_rejected(self, label: str, ip: str) -> None:
        assert is_public_ip(ip) is False, f"{label}: {ip} should be rejected"

    @pytest.mark.parametrize(
        ("label", "ip"),
        [
            ("cloudflare-v4", "1.1.1.1"),
            ("google-v4", "8.8.8.8"),
            ("cloudflare-v6", "2606:4700:4700::1111"),
            ("google-v6", "2001:4860:4860::8888"),
            ("github-v4", "140.82.114.4"),
        ],
    )
    def test_accepted(self, label: str, ip: str) -> None:
        assert is_public_ip(ip) is True, f"{label}: {ip} should be accepted"


# ---------------------------------------------------------------------------
# assert_allowed_scheme
# ---------------------------------------------------------------------------


class TestAssertAllowedScheme:
    """Default allow-list = ``{http, https}``; everything else rejected."""

    @pytest.mark.parametrize("scheme", ["http", "https", "HTTP", "HTTPS"])
    def test_default_accepts_http_https(self, scheme: str) -> None:
        assert_allowed_scheme(scheme)

    @pytest.mark.parametrize(
        "scheme",
        ["ftp", "file", "gopher", "data", "javascript", "ws", "wss", ""],
    )
    def test_default_rejects_other(self, scheme: str) -> None:
        with pytest.raises(FetchGuardBlocked) as exc_info:
            assert_allowed_scheme(scheme)
        assert exc_info.value.reason == "bad_scheme"

    def test_caller_can_narrow(self) -> None:
        """A caller restricting to ``https`` rejects ``http``."""
        with pytest.raises(FetchGuardBlocked) as exc_info:
            assert_allowed_scheme("http", allowed=("https",))
        assert exc_info.value.reason == "bad_scheme"
        # ``https`` still passes when the allow-list is narrowed.
        assert_allowed_scheme("https", allowed=("https",))


# ---------------------------------------------------------------------------
# resolve_public_address
# ---------------------------------------------------------------------------


class TestResolvePublicAddress:
    """Mixed + private + empty result sets all reject."""

    def test_all_public_returns_first(self) -> None:
        ip = resolve_public_address(
            "example.com",
            443,
            resolver=_fixed_resolver(["1.1.1.1", "2606:4700::1"]),
        )
        assert ip == "1.1.1.1"

    def test_mixed_public_private_rejected(self) -> None:
        with pytest.raises(FetchGuardBlocked) as exc_info:
            resolve_public_address(
                "example.com",
                443,
                resolver=_fixed_resolver(["1.1.1.1", "127.0.0.1"]),
            )
        assert exc_info.value.reason == "private_address"

    def test_empty_result_rejected(self) -> None:
        with pytest.raises(FetchGuardBlocked) as exc_info:
            resolve_public_address("example.com", 443, resolver=_fixed_resolver([]))
        assert exc_info.value.reason == "empty_dns"

    def test_private_only_rejected(self) -> None:
        with pytest.raises(FetchGuardBlocked) as exc_info:
            resolve_public_address(
                "example.com", 443, resolver=_fixed_resolver(["10.0.0.5"])
            )
        assert exc_info.value.reason == "private_address"

    def test_allow_private_hosts_requires_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kwarg alone is not enough — env must also be set."""
        monkeypatch.delenv(ENV_ALLOW_PRIVATE, raising=False)
        with pytest.raises(FetchGuardBlocked) as exc_info:
            resolve_public_address(
                "example.com",
                443,
                resolver=_fixed_resolver(["127.0.0.1"]),
                allow_private_hosts=True,
            )
        assert exc_info.value.reason == "private_address"

    def test_allow_private_hosts_with_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both flips → loopback resolution accepted."""
        monkeypatch.setenv(ENV_ALLOW_PRIVATE, "1")
        ip = resolve_public_address(
            "example.com",
            443,
            resolver=_fixed_resolver(["127.0.0.1"]),
            allow_private_hosts=True,
        )
        assert ip == "127.0.0.1"

    def test_env_alone_is_not_enough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env set but kwarg absent → still rejects private addresses."""
        monkeypatch.setenv(ENV_ALLOW_PRIVATE, "1")
        with pytest.raises(FetchGuardBlocked) as exc_info:
            resolve_public_address(
                "example.com",
                443,
                resolver=_fixed_resolver(["127.0.0.1"]),
            )
        assert exc_info.value.reason == "private_address"


# ---------------------------------------------------------------------------
# PinnedDnsBackend
# ---------------------------------------------------------------------------


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    """Upstream-stub that records the host its parent dialled."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        self.calls.append((host, port))
        # We never actually open a socket — this stub feeds tests of
        # the rewrite logic only. ``AsyncMockStream`` is httpcore's
        # own no-op stream class, used here as a sentinel; the test
        # never reads from it.
        return httpcore.AsyncMockStream([])

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        return None


class TestPinnedDnsBackend:
    """The backend ignores caller-supplied host and dials the pinned IP."""

    @pytest.mark.asyncio
    async def test_connect_tcp_uses_pinned_ip(self) -> None:
        upstream = _RecordingBackend()
        backend = PinnedDnsBackend(resolved_ip="203.0.113.5", upstream=upstream)
        await backend.connect_tcp(host="evil.test", port=443, timeout=5.0)
        # The host the upstream saw is the pinned IP, not the caller's
        # ``evil.test`` — that's the SSRF defeat. If a future refactor
        # ever forwarded the caller's host directly, this assertion
        # trips.
        assert upstream.calls == [("203.0.113.5", 443)]


# ---------------------------------------------------------------------------
# safe_fetch — full pipeline
# ---------------------------------------------------------------------------


def _stub_response(
    status: int = 200,
    body: bytes = b"OK",
    headers: list[tuple[str, str]] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers=headers if headers is not None else [],
        content=body,
    )


class TestSafeFetchSchemes:
    """Reject every non-http(s) scheme up-front."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/feed.ics",
            "file:///etc/passwd",
            "gopher://example.com/1",
            "javascript:alert(1)",
            "data:text/plain,oops",
        ],
    )
    async def test_non_http_rejected(self, url: str) -> None:
        with pytest.raises(FetchGuardBlocked) as exc_info:
            await safe_fetch(url, resolver=_fixed_resolver(["1.1.1.1"]))
        assert exc_info.value.reason == "bad_scheme"

    @pytest.mark.asyncio
    async def test_caller_can_restrict_to_https(self) -> None:
        with pytest.raises(FetchGuardBlocked) as exc_info:
            await safe_fetch(
                "http://example.com/x",
                resolver=_fixed_resolver(["1.1.1.1"]),
                allowed_schemes=("https",),
            )
        assert exc_info.value.reason == "bad_scheme"


class TestSafeFetchPrivateAddresses:
    """Each SSRF range collapses to ``reason='private_address'``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("label", "ip"),
        [
            ("loopback-v4", "127.0.0.1"),
            ("rfc1918-10", "10.1.2.3"),
            ("rfc1918-172", "172.16.5.10"),
            ("rfc1918-192", "192.168.1.5"),
            ("link-local-v4", "169.254.169.254"),
            ("multicast-v4", "224.1.2.3"),
            ("unspecified-v4", "0.0.0.0"),
            ("broadcast-v4", "255.255.255.255"),
            ("loopback-v6", "::1"),
            ("link-local-v6", "fe80::1"),
            ("rfc4193", "fc00::1"),
            ("multicast-v6", "ff00::1"),
            ("unspecified-v6", "::"),
        ],
    )
    async def test_rejected(self, label: str, ip: str) -> None:
        with pytest.raises(FetchGuardBlocked) as exc_info:
            await safe_fetch(
                "https://attacker.test/x",
                resolver=_fixed_resolver([ip]),
            )
        assert exc_info.value.reason == "private_address", label


class TestSafeFetchHappyPath:
    """A public-IP resolution + cooperative server returns the response."""

    @pytest.mark.asyncio
    async def test_public_resolver_accepted(self) -> None:
        """The mock transport sees the request, returns 200, body buffered."""

        def handler(request: httpx.Request) -> httpx.Response:
            # The MockTransport short-circuits the network entirely;
            # we still go through ``resolve_public_address`` first.
            return _stub_response(status=200, body=b"hello")

        # We patch the transport-builder so the mock takes over the
        # network leg without disturbing the SSRF resolution path.
        with patch(
            "app.net.fetch_guard._build_pinned_transport",
            return_value=httpx.MockTransport(handler),
        ):
            response = await safe_fetch(
                "https://example.com/feed",
                resolver=_fixed_resolver(["1.1.1.1"]),
            )
        assert response.status_code == 200
        assert response.content == b"hello"

    @pytest.mark.asyncio
    async def test_public_dns_hostname_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mock the resolver to confirm the DNS path is consulted once."""
        calls: list[tuple[str, int]] = []

        def _resolver(host: str, port: int) -> Iterable[str]:
            calls.append((host, port))
            return ["8.8.8.8"]

        def handler(request: httpx.Request) -> httpx.Response:
            return _stub_response(status=200, body=b"")

        with patch(
            "app.net.fetch_guard._build_pinned_transport",
            return_value=httpx.MockTransport(handler),
        ):
            await safe_fetch("https://dns.example.com/x", resolver=_resolver)

        assert calls == [("dns.example.com", 443)]


class TestSafeFetchSizeLimit:
    """Streaming body that exceeds the cap raises :class:`FetchGuardSizeLimit`."""

    @pytest.mark.asyncio
    async def test_oversize_body_aborts(self) -> None:
        big_body = b"X" * (200 * 1024)  # 200 KiB

        def handler(request: httpx.Request) -> httpx.Response:
            return _stub_response(status=200, body=big_body)

        with (
            patch(
                "app.net.fetch_guard._build_pinned_transport",
                return_value=httpx.MockTransport(handler),
            ),
            pytest.raises(FetchGuardSizeLimit),
        ):
            await safe_fetch(
                "https://example.com/big",
                resolver=_fixed_resolver(["1.1.1.1"]),
                max_body_bytes=100 * 1024,  # 100 KiB cap
            )

    @pytest.mark.asyncio
    async def test_under_cap_accepted(self) -> None:
        body = b"X" * (50 * 1024)  # 50 KiB

        def handler(request: httpx.Request) -> httpx.Response:
            return _stub_response(status=200, body=body)

        with patch(
            "app.net.fetch_guard._build_pinned_transport",
            return_value=httpx.MockTransport(handler),
        ):
            response = await safe_fetch(
                "https://example.com/ok",
                resolver=_fixed_resolver(["1.1.1.1"]),
                max_body_bytes=100 * 1024,
            )
        assert response.content == body


class TestSafeFetchTimeout:
    """A transport that raises :class:`httpx.TimeoutException` maps to guard."""

    @pytest.mark.asyncio
    async def test_timeout_translates_to_fetch_guard_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated read timeout")

        with (
            patch(
                "app.net.fetch_guard._build_pinned_transport",
                return_value=httpx.MockTransport(handler),
            ),
            pytest.raises(FetchGuardTimeout),
        ):
            await safe_fetch(
                "https://example.com/slow",
                resolver=_fixed_resolver(["1.1.1.1"]),
                timeout_seconds=0.1,
            )


class TestExceptionHierarchy:
    """All custom exceptions subclass :class:`FetchGuardError`."""

    def test_blocked_subclasses_base(self) -> None:
        assert issubclass(FetchGuardBlocked, FetchGuardError)

    def test_size_subclasses_base(self) -> None:
        assert issubclass(FetchGuardSizeLimit, FetchGuardError)

    def test_timeout_subclasses_base(self) -> None:
        assert issubclass(FetchGuardTimeout, FetchGuardError)


class TestPublicConstants:
    """Constants exposed at module level for callers to introspect."""

    def test_default_max_body_bytes(self) -> None:
        assert DEFAULT_MAX_BODY_BYTES == 1 * 1024 * 1024

    def test_default_timeout_seconds(self) -> None:
        assert DEFAULT_TIMEOUT_SECONDS == 5.0

    def test_public_schemes(self) -> None:
        assert frozenset({"http", "https"}) == PUBLIC_FETCH_SCHEMES
