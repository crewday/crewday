"""Unit tests for :mod:`app.security.bind_guard` (cd-z2g).

The guard enforces ``docs/specs/15-security-privacy.md`` §"Binding
policy" — loopback always passes, trusted-interface addresses pass,
wildcards + public addresses require the explicit opt-in. Tests
swap the interface-enumeration helper so results are deterministic
regardless of the CI host's actual network stack.

Covered branches:

* loopback (IPv4, IPv6, ``localhost``) pass;
* wildcard ``0.0.0.0`` / ``::`` require ``allow_public``;
* trusted interface (glob match, case-sensitive) passes;
* multiple globs accepted;
* untrusted interface requires ``allow_public``;
* IP not found on any interface is refused by default;
* hostname literals are refused without opt-in;
* invalid IP literals are refused without opt-in;
* the error message mentions the override env var.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from app.security.bind_guard import (
    BindGuardError,
    _is_loopback,
    _matches_any,
    _normalise_ip,
    assert_bind_allowed,
)


@pytest.fixture
def fixed_interfaces() -> Iterator[dict[str, str]]:
    """Stub ``_enumerate_interfaces`` with a reproducible mapping.

    The mapping mirrors a typical bare-metal box: loopback, a public
    NIC, a tailscale mesh interface, and a docker bridge. Individual
    tests can override by passing their own dict to the patch.
    """
    interfaces = {
        "127.0.0.1": "lo",
        "::1": "lo",
        "217.182.203.57": "enp6s0",
        "100.72.198.118": "tailscale0",
        "fd7a:115c:a1e0::6735:c676": "tailscale0",
        "172.17.0.1": "docker0",
    }
    with patch(
        "app.security.bind_guard._enumerate_interfaces",
        return_value=interfaces,
    ):
        yield interfaces


# ---------------------------------------------------------------------------
# Loopback
# ---------------------------------------------------------------------------


class TestLoopback:
    """Rule 1: loopback ALWAYS passes, regardless of other settings."""

    @pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "::1", "localhost"])
    def test_loopback_host_passes(
        self, host: str, fixed_interfaces: dict[str, str]
    ) -> None:
        # Must not raise.
        assert_bind_allowed(
            host, 8000, trusted_globs=["tailscale*"], allow_public=False
        )

    def test_localhost_case_insensitive(self, fixed_interfaces: dict[str, str]) -> None:
        assert_bind_allowed(
            "LocalHost", 8000, trusted_globs=["tailscale*"], allow_public=False
        )

    def test_loopback_passes_even_without_trusted_globs(
        self, fixed_interfaces: dict[str, str]
    ) -> None:
        """An operator with an empty ``trusted_interfaces`` list still
        gets loopback for free — the refusal logic never looks at the
        globs for a loopback address.
        """
        assert_bind_allowed("127.0.0.1", 8000, trusted_globs=[], allow_public=False)


# ---------------------------------------------------------------------------
# Wildcards
# ---------------------------------------------------------------------------


class TestWildcardV4:
    """Rule 2: ``0.0.0.0`` never passes without ``allow_public``."""

    def test_refused_without_opt_in(self, fixed_interfaces: dict[str, str]) -> None:
        with pytest.raises(BindGuardError, match="CREWDAY_ALLOW_PUBLIC_BIND"):
            assert_bind_allowed(
                "0.0.0.0",
                8000,
                trusted_globs=["tailscale*"],
                allow_public=False,
            )

    def test_allowed_with_opt_in(self, fixed_interfaces: dict[str, str]) -> None:
        assert_bind_allowed(
            "0.0.0.0",
            8000,
            trusted_globs=["tailscale*"],
            allow_public=True,
        )

    def test_trusted_glob_does_not_rescue_wildcard(
        self, fixed_interfaces: dict[str, str]
    ) -> None:
        """Even if ``*`` would match a wildcard, the rule short-circuits."""
        with pytest.raises(BindGuardError):
            assert_bind_allowed(
                "0.0.0.0",
                8000,
                trusted_globs=["*"],
                allow_public=False,
            )


class TestWildcardV6:
    """Rule 2, IPv6 half."""

    def test_refused_without_opt_in(self, fixed_interfaces: dict[str, str]) -> None:
        with pytest.raises(BindGuardError, match="CREWDAY_ALLOW_PUBLIC_BIND"):
            assert_bind_allowed(
                "::", 8000, trusted_globs=["tailscale*"], allow_public=False
            )

    def test_allowed_with_opt_in(self, fixed_interfaces: dict[str, str]) -> None:
        assert_bind_allowed("::", 8000, trusted_globs=["tailscale*"], allow_public=True)


# ---------------------------------------------------------------------------
# Trusted interface match
# ---------------------------------------------------------------------------


class TestTrustedInterface:
    """Rule 3: concrete address on a glob-matched interface passes."""

    def test_tailscale_ipv4_passes_with_tailscale_glob(
        self, fixed_interfaces: dict[str, str]
    ) -> None:
        assert_bind_allowed(
            "100.72.198.118",
            8000,
            trusted_globs=["tailscale*"],
            allow_public=False,
        )

    def test_tailscale_ipv6_passes_with_tailscale_glob(
        self, fixed_interfaces: dict[str, str]
    ) -> None:
        assert_bind_allowed(
            "fd7a:115c:a1e0::6735:c676",
            8000,
            trusted_globs=["tailscale*"],
            allow_public=False,
        )

    def test_multiple_globs_accepted(self, fixed_interfaces: dict[str, str]) -> None:
        """Operators extend past the default by replacing the env var;
        the guard accepts any glob in the list."""
        assert_bind_allowed(
            "100.72.198.118",
            8000,
            trusted_globs=["wg*", "nebula*", "tailscale*"],
            allow_public=False,
        )

    def test_ipv6_address_is_normalised_before_lookup(
        self, fixed_interfaces: dict[str, str]
    ) -> None:
        """An equivalent non-canonical literal still matches.

        ``fd7a:115c:a1e0:0:0:0:6735:c676`` and
        ``fd7a:115c:a1e0::6735:c676`` are the same address; the guard
        normalises through :mod:`ipaddress` before matching so the
        operator's exact spelling doesn't change the trust decision.
        """
        assert_bind_allowed(
            "fd7a:115c:a1e0:0:0:0:6735:c676",
            8000,
            trusted_globs=["tailscale*"],
            allow_public=False,
        )

    def test_glob_is_case_sensitive(self, fixed_interfaces: dict[str, str]) -> None:
        """Linux interface names are case-sensitive; so are the globs.

        An operator who sets ``TAILSCALE*`` (upper-case) must not get
        a pass on a ``tailscale0`` interface — that's a typo, and
        silently matching would hide a real configuration mistake.
        """
        with pytest.raises(BindGuardError):
            assert_bind_allowed(
                "100.72.198.118",
                8000,
                trusted_globs=["TAILSCALE*"],
                allow_public=False,
            )


# ---------------------------------------------------------------------------
# Public / untrusted interface
# ---------------------------------------------------------------------------


class TestUntrustedInterface:
    """Rule 4: concrete address off a trusted interface needs opt-in."""

    def test_public_ip_without_opt_in_raises(
        self, fixed_interfaces: dict[str, str]
    ) -> None:
        with pytest.raises(BindGuardError, match="CREWDAY_ALLOW_PUBLIC_BIND"):
            assert_bind_allowed(
                "217.182.203.57",
                8000,
                trusted_globs=["tailscale*"],
                allow_public=False,
            )

    def test_public_ip_with_opt_in_allowed(
        self, fixed_interfaces: dict[str, str]
    ) -> None:
        assert_bind_allowed(
            "217.182.203.57",
            8000,
            trusted_globs=["tailscale*"],
            allow_public=True,
        )

    def test_error_message_names_interface_and_ip(
        self, fixed_interfaces: dict[str, str]
    ) -> None:
        with pytest.raises(BindGuardError) as excinfo:
            assert_bind_allowed(
                "217.182.203.57",
                8000,
                trusted_globs=["tailscale*"],
                allow_public=False,
            )
        msg = str(excinfo.value)
        assert "enp6s0" in msg
        assert "217.182.203.57" in msg
        assert "CREWDAY_ALLOW_PUBLIC_BIND" in msg

    def test_docker_bridge_not_trusted_by_default(
        self, fixed_interfaces: dict[str, str]
    ) -> None:
        """Container bridges look loopback-ish but aren't the loopback
        device; without an explicit ``docker*`` glob they must refuse."""
        with pytest.raises(BindGuardError):
            assert_bind_allowed(
                "172.17.0.1",
                8000,
                trusted_globs=["tailscale*"],
                allow_public=False,
            )


# ---------------------------------------------------------------------------
# Address not on any local interface
# ---------------------------------------------------------------------------


class TestUnknownInterface:
    """Defence-in-depth: an IP we can't place on any interface refuses."""

    def test_unknown_ip_refused_without_opt_in(
        self, fixed_interfaces: dict[str, str]
    ) -> None:
        with pytest.raises(BindGuardError) as excinfo:
            assert_bind_allowed(
                "203.0.113.7",  # TEST-NET-3, unassigned
                8000,
                trusted_globs=["tailscale*"],
                allow_public=False,
            )
        # The message surfaces "unknown" so the operator sees that the
        # address isn't even pinned to anything we can reason about.
        assert "unknown" in str(excinfo.value)

    def test_unknown_ip_allowed_with_opt_in(
        self, fixed_interfaces: dict[str, str]
    ) -> None:
        assert_bind_allowed(
            "203.0.113.7",
            8000,
            trusted_globs=["tailscale*"],
            allow_public=True,
        )


# ---------------------------------------------------------------------------
# Hostname / malformed input
# ---------------------------------------------------------------------------


class TestHostnameRefusal:
    """The guard never resolves hostnames; they are opt-in only."""

    def test_hostname_refused_without_opt_in(
        self, fixed_interfaces: dict[str, str]
    ) -> None:
        with pytest.raises(BindGuardError, match="bind_host must be a concrete IP"):
            assert_bind_allowed(
                "example.com",
                8000,
                trusted_globs=["tailscale*"],
                allow_public=False,
            )

    def test_hostname_allowed_with_opt_in(
        self, fixed_interfaces: dict[str, str]
    ) -> None:
        assert_bind_allowed(
            "example.com",
            8000,
            trusted_globs=["tailscale*"],
            allow_public=True,
        )

    def test_garbage_literal_refused(self, fixed_interfaces: dict[str, str]) -> None:
        with pytest.raises(BindGuardError):
            assert_bind_allowed(
                "not-an-ip-at-all-$$",
                8000,
                trusted_globs=["tailscale*"],
                allow_public=False,
            )


# ---------------------------------------------------------------------------
# Platform refusal
# ---------------------------------------------------------------------------


class TestNonLinuxNotSupported:
    """Enumeration is Linux-only; any other platform refuses clearly.

    The ``SIOCGIFCONF`` ioctl number and ``struct ifreq`` layout are
    Linux-specific; ``/proc/net/if_inet6`` is also Linux-only. Rather
    than silently falling back to a best-effort enumeration that
    could miss the interface owning the bind address, the guard
    refuses on every non-Linux host — loopback and wildcard-refusal
    still short-circuit before enumeration, so those paths stay
    correct everywhere.
    """

    @pytest.mark.parametrize("platform", ["win32", "darwin", "freebsd13"])
    def test_non_linux_raises_on_non_loopback(
        self, monkeypatch: pytest.MonkeyPatch, platform: str
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        with pytest.raises(BindGuardError, match="not supported"):
            assert_bind_allowed(
                "203.0.113.7",
                8000,
                trusted_globs=["tailscale*"],
                allow_public=False,
            )

    @pytest.mark.parametrize("platform", ["win32", "darwin", "freebsd13"])
    def test_non_linux_loopback_still_passes(
        self, monkeypatch: pytest.MonkeyPatch, platform: str
    ) -> None:
        """Loopback short-circuits before we ever touch interface
        enumeration, so unsupported-OS errors can't mask a safe bind."""
        monkeypatch.setattr(sys, "platform", platform)
        assert_bind_allowed(
            "127.0.0.1", 8000, trusted_globs=["tailscale*"], allow_public=False
        )

    @pytest.mark.parametrize("platform", ["win32", "darwin", "freebsd13"])
    def test_non_linux_wildcard_without_opt_in_still_refuses(
        self, monkeypatch: pytest.MonkeyPatch, platform: str
    ) -> None:
        """Wildcard refusal also short-circuits enumeration."""
        monkeypatch.setattr(sys, "platform", platform)
        with pytest.raises(BindGuardError, match="CREWDAY_ALLOW_PUBLIC_BIND"):
            assert_bind_allowed(
                "0.0.0.0", 8000, trusted_globs=["tailscale*"], allow_public=False
            )


# ---------------------------------------------------------------------------
# Enumeration error surfacing
# ---------------------------------------------------------------------------


class TestEnumerationFailure:
    """An OSError from the ioctl surfaces as a BindGuardError."""

    def test_oserror_surfaces_as_bindguard_error(self) -> None:
        def raising() -> dict[str, str]:
            raise OSError("simulated ENETDOWN")

        # Patch the Linux reader directly so the public
        # ``_enumerate_interfaces`` path wraps the OSError.
        with (
            patch.object(sys, "platform", "linux"),
            patch(
                "app.security.bind_guard._read_linux_ipv4_interfaces",
                side_effect=OSError("simulated ENETDOWN"),
            ),
            patch(
                "app.security.bind_guard._read_linux_ipv6_interfaces",
                side_effect=raising,
            ),
            pytest.raises(BindGuardError, match="SIOCGIFCONF"),
        ):
            assert_bind_allowed(
                "203.0.113.7",
                8000,
                trusted_globs=["tailscale*"],
                allow_public=False,
            )

    def test_ioctl_buffer_ceiling_refuses_to_truncate(self) -> None:
        """A kernel that keeps filling the buffer must surface an error.

        Silently truncating would hide the interface hosting the bind
        address, so a host with an unrealistic >25k-interface count
        would fall through to "unknown" and refuse even trusted
        binds silently. Raising from the reader converts this into a
        loud, actionable failure.
        """
        # Fake ioctl that always reports "buffer too small" (out_len ==
        # buf_size) to force the loop to hit the ceiling. We patch the
        # module-level ``fcntl.ioctl`` so the reader's retry logic is
        # the code under test.
        import struct as _struct

        def fake_ioctl(_fd: int, _req: int, buf: bytes | bytearray) -> bytes:
            # Return a struct that claims the full buffer was used.
            size = _struct.unpack("iL", buf)[0]
            return _struct.pack("iL", size, 0)

        with (
            patch.object(sys, "platform", "linux"),
            patch("app.security.bind_guard.fcntl.ioctl", side_effect=fake_ioctl),
            pytest.raises(BindGuardError, match="SIOCGIFCONF"),
        ):
            assert_bind_allowed(
                "203.0.113.7",
                8000,
                trusted_globs=["tailscale*"],
                allow_public=False,
            )


# ---------------------------------------------------------------------------
# Private helpers (targeted unit tests)
# ---------------------------------------------------------------------------


class TestIsLoopback:
    @pytest.mark.parametrize(
        "host", ["127.0.0.1", "127.0.0.42", "::1", "localhost", "LOCALHOST"]
    )
    def test_recognises_loopback(self, host: str) -> None:
        assert _is_loopback(host) is True

    @pytest.mark.parametrize(
        "host",
        [
            "0.0.0.0",
            "::",
            "10.0.0.1",
            "217.182.203.57",
            "example.com",
            "not-an-ip",
        ],
    )
    def test_rejects_non_loopback(self, host: str) -> None:
        assert _is_loopback(host) is False


class TestNormaliseIp:
    def test_ipv4_passthrough(self) -> None:
        assert _normalise_ip("10.0.0.1") == "10.0.0.1"

    def test_ipv6_compressed(self) -> None:
        assert (
            _normalise_ip("fd7a:115c:a1e0:0:0:0:6735:c676")
            == "fd7a:115c:a1e0::6735:c676"
        )

    def test_hostname_returns_none(self) -> None:
        assert _normalise_ip("example.com") is None

    def test_garbage_returns_none(self) -> None:
        assert _normalise_ip("$$$") is None


class TestMatchesAny:
    def test_single_glob(self) -> None:
        assert _matches_any("tailscale0", ["tailscale*"]) is True

    def test_no_match(self) -> None:
        assert _matches_any("eth0", ["tailscale*", "wg*"]) is False

    def test_empty_globs(self) -> None:
        assert _matches_any("tailscale0", []) is False

    def test_exact_name(self) -> None:
        assert _matches_any("wg0", ["wg0"]) is True


# ---------------------------------------------------------------------------
# Smoke: real enumeration returns something sane on Linux
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="Linux/macOS only")
class TestRealEnumerationSmoke:
    """One integration-ish check that the live reader finds loopback."""

    def test_loopback_present_in_real_enumeration(self) -> None:
        from app.security.bind_guard import _enumerate_interfaces

        mapping = _enumerate_interfaces()
        # Every POSIX host has loopback; if this ever fails the
        # enumerator has regressed in a way that would leak public
        # IPs on real boot.
        assert "127.0.0.1" in mapping
        assert mapping["127.0.0.1"] == "lo"
