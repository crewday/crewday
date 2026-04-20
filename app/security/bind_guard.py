"""Refuse to boot on an untrusted interface.

See ``docs/specs/01-architecture.md`` §"Runtime invariants" #6,
``docs/specs/15-security-privacy.md`` §"Binding policy", and
``docs/specs/16-deployment-operations.md`` §"Bind model".

Contract
--------

:func:`assert_bind_allowed` is the single entry point. It is called
from :mod:`app.main` (and, in the future, from the CLI ``serve``
command) **before** the ASGI server opens its listening socket, so a
misconfigured ``CREWDAY_BIND_HOST`` fails loudly at start-up instead
of quietly exposing the process.

The rules, in order:

1. Loopback (``127.0.0.0/8``, ``::1``, ``localhost``) always passes.
2. ``0.0.0.0`` / ``::`` never pass on their own; they require
   ``allow_public=True`` (env ``CREWDAY_ALLOW_PUBLIC_BIND=1``). The
   wildcard binds every interface regardless of trust, so there is
   no "interface" to match — the operator has to own that decision.
3. Any other address is matched against the local interface table.
   If the interface hosting the address has a name that matches a
   glob in ``trusted_globs`` (default ``["tailscale*"]``), the bind
   passes.
4. Otherwise — including "address not assigned to any interface" —
   refuse, unless ``allow_public`` is set.

The interface-name check is what gives the rule its safety margin:
the spec deliberately does **not** trust CIDR ranges like CGNAT
(100.64.0.0/10), because those also appear on ISP carrier-grade
NAT, mobile carriers, and shared-IP VPS. An address there is only
trustworthy when it's actually on a Tailscale (or similar) mesh
interface.

Implementation is stdlib-only (no ``psutil`` / ``netifaces`` /
``ifaddr``). Linux is the only supported enumeration target: IPv4
comes from the ``SIOCGIFCONF`` ioctl (Linux-specific ioctl number
``0x8912`` and ``struct ifreq`` layout) and IPv6 from
``/proc/net/if_inet6``. Every other platform — macOS, the BSDs,
Windows — raises :class:`BindGuardError` at enumeration time so an
untested host fails loudly rather than silently default-opening.
Loopback and wildcard-refusal short-circuit before enumeration, so
``127.0.0.1`` and a refused ``0.0.0.0`` still behave correctly on
any platform.
"""

from __future__ import annotations

import array
import fcntl
import fnmatch
import ipaddress
import socket
import struct
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Final

__all__ = ["BindGuardError", "assert_bind_allowed"]


class BindGuardError(RuntimeError):
    """Raised when a bind target violates the public-interface policy.

    A ``RuntimeError`` subclass so callers that just want "loud start-up
    failure" can catch the broader type; the narrow class lets tests and
    :mod:`app.main` (which maps onto :class:`~app.main.PublicBindRefused`)
    tell this refusal apart from other boot-time RuntimeErrors.
    """


# Literal wildcards. Matching is case-insensitive elsewhere because
# interface-name globs are fnmatch, but these strings are IP literals so
# exact-match is correct.
_WILDCARD_V4: Final[str] = "0.0.0.0"
_WILDCARD_V6: Final[str] = "::"
_LOOPBACK_NAMES: Final[frozenset[str]] = frozenset({"localhost"})

# ``SIOCGIFCONF`` returns an array of ``struct ifreq``. On Linux + glibc
# the struct is 40 bytes: 16-byte name padded with NULs, then a 16-byte
# sockaddr (only the first 8 bytes matter for AF_INET — family + port +
# 4-byte address + 8 bytes of padding). We read only the IPv4 address
# off each entry and ignore non-AF_INET families; AF_INET6 is never
# returned by SIOCGIFCONF anyway (see /proc/net/if_inet6 for v6).
_IFREQ_SIZE: Final[int] = 40
_IFNAMSIZ: Final[int] = 16
_SIOCGIFCONF: Final[int] = 0x8912
# Buffer grows if the kernel reports it needs more; start at 8 KiB (≈200
# interface entries) which covers every realistic host.
_IFCONF_BUF_MIN: Final[int] = 8 * 1024
_IFCONF_BUF_MAX: Final[int] = 1 * 1024 * 1024

# ``/proc/net/if_inet6`` columns: <addr-hex> <iface-idx> <prefix-len>
# <scope> <flags> <name>. The address is a 32-char lowercase hex string
# with no colons.
_IF_INET6_PATH: Final[Path] = Path("/proc/net/if_inet6")


def _is_loopback(host: str) -> bool:
    """Return ``True`` when ``host`` addresses the loopback device.

    Accepts the common spellings: any IPv4 in ``127.0.0.0/8``, the
    IPv6 ``::1``, and the hostname ``localhost``. Everything else
    (including a hostname that happens to resolve to loopback) is
    treated as non-loopback — hostnames are the operator's contract
    with the runtime, and we should not second-guess a resolver.
    """
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback


def _is_wildcard(host: str) -> bool:
    """Return ``True`` when ``host`` is the any-address wildcard."""
    return host in (_WILDCARD_V4, _WILDCARD_V6)


def _normalise_ip(host: str) -> str | None:
    """Return the canonical string form of an IP literal, or ``None``.

    Hostnames return ``None`` — the guard refuses to resolve them,
    because a resolver answer that later changes (DNS flip, /etc/hosts
    edit, NSS cache invalidation) would bypass the trust decision made
    here. Operators point ``CREWDAY_BIND_HOST`` at a concrete address.
    """
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        return None


def _read_linux_ipv4_interfaces() -> dict[str, str]:
    """Return ``{normalised_ipv4: iface_name}`` via ``SIOCGIFCONF``.

    The ioctl approach matches what ``ip addr`` / ``ifconfig`` do
    under the hood and needs no extra dependency. We grow the buffer
    to a sane ceiling so hosts with hundreds of container bridges
    (our dev box has 40+) still enumerate correctly; beyond that we'd
    rather surface an error than silently truncate.
    """
    mapping: dict[str, str] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        buf_size = _IFCONF_BUF_MIN
        while True:
            names = array.array("B", b"\x00" * buf_size)
            addr_ptr = names.buffer_info()[0]
            ifconf = struct.pack("iL", buf_size, addr_ptr)
            out = fcntl.ioctl(sock.fileno(), _SIOCGIFCONF, ifconf)
            out_len = struct.unpack("iL", out)[0]
            # Kernel filled the buffer: it may have had more to say.
            # Double and retry. Bail before we allocate absurd amounts.
            if out_len >= buf_size:
                if buf_size >= _IFCONF_BUF_MAX:
                    # Refuse rather than silently truncate — a truncated
                    # enumeration could hide the interface that actually
                    # owns the bind address and mis-classify it as
                    # "unknown", which is a safe-fail but an operator
                    # with >25k interfaces deserves a clear error.
                    raise OSError(
                        "SIOCGIFCONF returned >="
                        f"{_IFCONF_BUF_MAX} bytes; refusing to truncate"
                    )
                buf_size *= 2
                continue
            raw = names.tobytes()[:out_len]
            break

    for offset in range(0, len(raw), _IFREQ_SIZE):
        entry = raw[offset : offset + _IFREQ_SIZE]
        if len(entry) < _IFREQ_SIZE:
            break
        name = entry[:_IFNAMSIZ].split(b"\x00", 1)[0].decode("ascii", "replace")
        # sockaddr_in: family (2 bytes) + port (2) + addr (4) + pad (8).
        family = struct.unpack("H", entry[_IFNAMSIZ : _IFNAMSIZ + 2])[0]
        if family != socket.AF_INET:
            continue
        addr_bytes = entry[_IFNAMSIZ + 4 : _IFNAMSIZ + 8]
        ip = socket.inet_ntoa(addr_bytes)
        mapping[ipaddress.ip_address(ip).compressed] = name
    return mapping


def _read_linux_ipv6_interfaces() -> dict[str, str]:
    """Return ``{normalised_ipv6: iface_name}`` from ``/proc/net/if_inet6``.

    The file is absent on non-Linux kernels and on Linux builds with
    IPv6 disabled — both are legitimate and we simply return an empty
    mapping. A permission error is surfaced so the operator sees the
    cause; ``/proc/net/if_inet6`` is world-readable by default, so a
    failure here indicates a LSM / namespace issue the user should
    know about rather than a guard-silent edge case.
    """
    mapping: dict[str, str] = {}
    if not _IF_INET6_PATH.is_file():
        return mapping
    for line in _IF_INET6_PATH.read_text(encoding="ascii").splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        hex_addr, _idx, _prefix, _scope, _flags, name = parts[:6]
        if len(hex_addr) != 32:
            continue
        groups = ":".join(hex_addr[i : i + 4] for i in range(0, 32, 4))
        try:
            canonical = ipaddress.ip_address(groups).compressed
        except ValueError:
            continue
        mapping[canonical] = name
    return mapping


def _enumerate_interfaces() -> dict[str, str]:
    """Return a single ``{ip: iface_name}`` mapping for this host.

    This is the indirection tests patch via :mod:`unittest.mock` —
    rather than stubbing the underlying ``fcntl`` / ``/proc`` readers
    individually, every test swaps this function for a fixed dict so
    the resulting assertion is independent of the host interfaces
    that happen to exist while CI runs.

    Raises :class:`BindGuardError` on any non-Linux platform (macOS,
    BSDs, Windows — the ioctl number and ``struct ifreq`` layout are
    Linux-specific; ``/proc/net/if_inet6`` is Linux-only) and on any
    unexpected OSError from the Linux readers. We prefer a loud boot
    failure to a silent default-open.
    """
    if not sys.platform.startswith("linux"):
        raise BindGuardError(
            f"bind guard enumeration is not supported on {sys.platform!r}; "
            "run crewday on Linux (see docs/specs/16 §Bind model)"
        )
    try:
        ipv4 = _read_linux_ipv4_interfaces()
    except OSError as exc:
        raise BindGuardError(
            f"failed to enumerate IPv4 interfaces via SIOCGIFCONF: {exc}"
        ) from exc
    # IPv6 readers are best-effort (the file is Linux-only and may
    # be absent); a missing file yields an empty mapping, which is
    # fine — if the operator's bind is a v6 literal that we can't
    # verify, the guard will refuse at the match step.
    ipv6 = _read_linux_ipv6_interfaces()
    # v4 and v6 keys are disjoint because they're different address
    # families, so ``|`` is safe.
    return ipv4 | ipv6


def _matches_any(iface: str, globs: Iterable[str]) -> bool:
    """Return ``True`` when ``iface`` matches at least one glob.

    fnmatch is case-insensitive on Windows and case-sensitive on
    POSIX; interface names on Linux are case-sensitive (``eth0`` and
    ``Eth0`` would be distinct devices), so we use the case-sensitive
    :func:`fnmatch.fnmatchcase` for a stable cross-platform rule.
    """
    return any(fnmatch.fnmatchcase(iface, glob) for glob in globs)


def assert_bind_allowed(
    host: str,
    port: int,
    *,
    trusted_globs: list[str],
    allow_public: bool,
) -> None:
    """Refuse to boot on an untrusted interface.

    ``host`` is the literal the operator set via
    ``CREWDAY_BIND_HOST``; ``port`` is carried only for the error
    message. ``trusted_globs`` is the operator-supplied
    :attr:`~app.config.Settings.trusted_interfaces` list — the
    default is ``["tailscale*"]``, replaced wholesale when overridden.
    ``allow_public`` is the explicit opt-in
    (:attr:`~app.config.Settings.allow_public_bind`) and is the only
    way a bare wildcard or an interface-less address passes.

    Raises :class:`BindGuardError` on refusal. The error message
    quotes the host, the resolved interface (or "unknown"), and the
    override env var so operators can act on the text alone.
    """
    # Rule 1: loopback always wins.
    if _is_loopback(host):
        return

    # Rule 2: wildcards ONLY with the explicit opt-in. The wildcard
    # binds every interface regardless of trust, so the match step
    # below is meaningless for it.
    if _is_wildcard(host):
        if allow_public:
            return
        raise BindGuardError(
            f"refusing to bind to wildcard address {host!r} on port {port}. "
            "Set CREWDAY_ALLOW_PUBLIC_BIND=1 to override "
            "(see docs/specs/15-security-privacy.md §Binding policy)."
        )

    # Rule 3/4: concrete address — match it to a local interface.
    normalised = _normalise_ip(host)
    if normalised is None:
        # A hostname. We deliberately don't resolve: see ``_normalise_ip``.
        if allow_public:
            return
        raise BindGuardError(
            f"refusing to bind to non-literal host {host!r} on port {port}: "
            "bind_host must be a concrete IP address. "
            "Set CREWDAY_ALLOW_PUBLIC_BIND=1 to override "
            "(see docs/specs/15-security-privacy.md §Binding policy)."
        )

    interfaces = _enumerate_interfaces()
    iface = interfaces.get(normalised)

    if iface is not None and _matches_any(iface, trusted_globs):
        return

    if allow_public:
        return

    # Use the textual form we were given in the error so it matches
    # the env var the operator set; ``normalised`` is only for the
    # lookup. ``iface or 'unknown'`` keeps the message intelligible
    # when the address isn't assigned anywhere.
    iface_label = iface if iface is not None else "unknown"
    raise BindGuardError(
        f"refusing to bind to public interface {iface_label} "
        f"(IP {host}). Set CREWDAY_ALLOW_PUBLIC_BIND=1 to override."
    )
