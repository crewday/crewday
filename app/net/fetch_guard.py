"""SSRF-guarded HTTP fetch helper.

Canonical implementation of the guarded portion of the §15 "SSRF"
contract. Server-side fetches whose URL is operator- or user-supplied
and not explicitly carved out — iCal feeds (§04), Turnstile
verification (cd-6qi3), future LLM-tool fetches (§11) — run through
this module so a single audit point owns the rules. Outbound webhooks
(§10) are intentionally outside this mandatory guard per §15.

The module exposes two surfaces:

* **Pure SSRF primitives** (sync, no I/O): :func:`is_public_ip`,
  :func:`assert_allowed_scheme`, :func:`resolve_public_address`,
  :class:`Resolver`, :data:`PUBLIC_FETCH_SCHEMES`. These are the
  building blocks that already-existing per-feature fetchers (e.g.
  :mod:`app.adapters.ical.validator`) consume — they bring their own
  transport machinery (multi-hop redirects, content-sniff, custom
  error vocabularies) but defer the SSRF gates here so the rules
  cannot drift between features.

* **All-in-one async fetch** (async, full pipeline):
  :func:`safe_fetch`. New callers that need a single GET / POST with
  built-in SSRF, size-cap, and timeout enforcement should reach for
  this. It builds an :class:`httpx.AsyncClient` whose underlying
  connection pool resolves the host **once** before connect and pins
  the resulting IP into :class:`httpcore.AsyncConnectionPool` via a
  custom :class:`PinnedDnsBackend`. Subsequent re-resolution by the
  kernel cannot redirect the connection to a private IP after the
  IP-validation step (the classic DNS-rebind TOCTOU).

Why two surfaces, not one? The iCal validator's pipeline is
*synchronous*, multi-hop, and content-sniffs the body — wrapping it
in :func:`safe_fetch` would force an async refactor of the whole
:class:`app.adapters.ical.ports.IcalValidator` port, which is out of
scope for this module's bring-up. Instead we share what is genuinely
shared (the IP classifier, the resolver, the scheme allow-list) and
let each feature own its transport layer until / unless an explicit
unification ticket lands.

Exception hierarchy
-------------------

All errors raised by this module subclass :class:`FetchGuardError`:

* :class:`FetchGuardBlocked` — the request never went out: bad scheme,
  resolved to a non-public IP, or empty resolver result.
* :class:`FetchGuardSizeLimit` — body exceeded ``max_body_bytes`` mid-
  stream; the connection was aborted.
* :class:`FetchGuardTimeout` — connect or read deadline elapsed.

Per-feature fetchers translate these into their own vocabulary (see
:mod:`app.adapters.ical.validator` for the iCal mapping).

Configuration
-------------

The dev / e2e ``allow_private_hosts`` carve-out requires **two
independent flips**: the kwarg ``allow_private_hosts=True`` on the
caller AND the env var ``CREWDAY_NET_GUARD_ALLOW_PRIVATE=1``. A single
flip is not enough — a feature that hard-coded ``allow_private_hosts=
True`` cannot be exploited unless an operator also flips the env.
Production deployments leave the env unset; the loopback / RFC 1918
blocklist always fires.

See ``docs/specs/15-security-privacy.md`` §"SSRF",
``docs/specs/04-properties-and-stays.md`` §"SSRF guard".
"""

from __future__ import annotations

import ipaddress
import os
import socket
import ssl
from collections.abc import Callable, Iterable, Mapping
from typing import Final, Literal

import anyio
import httpcore
import httpx
from httpcore._backends.base import SOCKET_OPTION

__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "ENV_ALLOW_PRIVATE",
    "PUBLIC_FETCH_SCHEMES",
    "FetchGuardBlocked",
    "FetchGuardError",
    "FetchGuardSizeLimit",
    "FetchGuardTimeout",
    "PinnedDnsBackend",
    "Resolver",
    "assert_allowed_scheme",
    "is_public_ip",
    "resolve_public_address",
    "safe_fetch",
    "system_resolver",
]


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Schemes :func:`safe_fetch` accepts. Anything else trips
#: :class:`FetchGuardBlocked`. Per-feature wrappers may narrow this
#: further (the iCal validator restricts to ``https://`` only).
PUBLIC_FETCH_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: Default streaming-body cap. 1 MiB is small enough to bound a
#: single-call cost, large enough for typical feed / verification
#: payloads. Callers override this only *downward* in production —
#: every feature has its own per-call budget on top.
DEFAULT_MAX_BODY_BYTES: Final[int] = 1 * 1024 * 1024

#: Default total timeout (connect + read). Captures the slow-loris
#: vector: a host that opens TCP fast then trickles bytes is killed at
#: this deadline rather than letting the worker hang.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0

#: Env var that, in combination with ``allow_private_hosts=True``,
#: opens the dev / e2e carve-out. Both must be set; one alone is not
#: enough. Production leaves this unset.
ENV_ALLOW_PRIVATE: Final[str] = "CREWDAY_NET_GUARD_ALLOW_PRIVATE"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FetchGuardError(Exception):
    """Base class for every fetch-guard rejection.

    Callers can ``except FetchGuardError`` to catch any guard-level
    failure without knowing the specific subclass; per-feature
    wrappers translate this into their own exception vocabulary.
    """


class FetchGuardBlocked(FetchGuardError):
    """The request never opened a socket — gate rejected it.

    Raised for: non-allowed scheme, empty DNS result, host that
    resolved to a private / loopback / link-local / multicast /
    reserved / unspecified address, missing host, DNS lookup error.

    ``reason`` is a stable enum string so per-feature wrappers can
    map a single guard exception to their own error vocabulary
    without parsing the message text. The set is intentionally small
    and additive — new reasons go here rather than inside callers.
    """

    BlockedReason = Literal[
        "bad_scheme",
        "no_host",
        "empty_dns",
        "dns_error",
        "private_address",
    ]

    def __init__(self, message: str, *, reason: BlockedReason) -> None:
        super().__init__(message)
        self.reason: FetchGuardBlocked.BlockedReason = reason


class FetchGuardSizeLimit(FetchGuardError):
    """Streaming body exceeded the configured ``max_body_bytes``.

    The connection is aborted before the offending byte is delivered
    to the caller — a 1-byte over-cap response and a 1-GiB over-cap
    response both raise the same error after roughly the same amount
    of work.
    """


class FetchGuardTimeout(FetchGuardError):
    """Connect or read deadline elapsed.

    Translates :class:`httpx.TimeoutException` (any subclass) into
    the guard's own vocabulary so callers don't have to import httpx.
    """


# ---------------------------------------------------------------------------
# Public-IP classifier
# ---------------------------------------------------------------------------


# CGNAT (RFC 6598) — not classified as ``is_private`` by stdlib's
# :mod:`ipaddress` module; we add an explicit CIDR check so an
# attacker's DNS that returns ``100.64.0.1`` is still rejected.
# We use :class:`IPv4Network` directly (not :func:`ip_network`) because
# the latter returns a v4 / v6 union that mypy --strict refuses to
# narrow to v4 even with a literal v4 string.
_CGNAT_V4: Final[ipaddress.IPv4Network] = ipaddress.IPv4Network("100.64.0.0/10")


def is_public_ip(ip_str: str) -> bool:
    """Return ``True`` iff ``ip_str`` is a routable public unicast address.

    Rejects every range an SSRF probe might target:

    * Loopback: ``127.0.0.0/8``, ``::1``.
    * RFC 1918 private: ``10/8``, ``172.16/12``, ``192.168/16``.
    * RFC 4193 unique-local IPv6: ``fc00::/7``.
    * Link-local: ``169.254.0.0/16``, ``fe80::/10`` (cloud-metadata
      service at ``169.254.169.254`` is the textbook target).
    * Multicast: ``224.0.0.0/4``, ``ff00::/8``.
    * Unspecified: ``0.0.0.0``, ``::``.
    * Reserved: ``240.0.0.0/4``, IPv6 reserved ranges.
    * CGNAT: ``100.64.0.0/10`` (RFC 6598 — stdlib does not flag this).
    * IPv4-mapped IPv6 (``::ffff:127.0.0.1``) — stdlib's
      ``is_loopback`` already catches mapped loopback / RFC 1918 via
      :meth:`IPv6Address.ipv4_mapped`, but the standard
      ``is_private`` heuristic does too; we rely on both.

    A garbage input string returns ``False`` rather than raising —
    callers treat unparseable as "definitely not a public IP".
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    # IPv4-mapped IPv6: classify against the embedded v4 address so
    # ``::ffff:127.0.0.1`` is rejected for the same reasons
    # ``127.0.0.1`` is. ``ipaddress.IPv6Address.ipv4_mapped`` returns
    # the inner v4 or ``None``.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return is_public_ip(str(addr.ipv4_mapped))
    if (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_private
    ):
        return False
    return not (isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT_V4)


# ---------------------------------------------------------------------------
# Scheme gate
# ---------------------------------------------------------------------------


def assert_allowed_scheme(
    scheme: str,
    *,
    allowed: Iterable[str] = PUBLIC_FETCH_SCHEMES,
) -> None:
    """Raise :class:`FetchGuardBlocked` unless ``scheme`` is in ``allowed``.

    The check is case-insensitive (``HTTPS`` is equivalent to
    ``https``). Empty / ``None``-shaped schemes are rejected.

    Per-feature callers narrow ``allowed`` — the iCal validator passes
    ``("https",)`` to lock out ``http`` even though the default helper
    permits both.
    """
    normalised = (scheme or "").lower()
    allowed_set = {s.lower() for s in allowed}
    if normalised not in allowed_set:
        raise FetchGuardBlocked(
            f"scheme {scheme!r} not allowed; expected one of {sorted(allowed_set)!r}",
            reason="bad_scheme",
        )


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


# A resolver maps ``(host, port)`` to an iterable of address strings.
# Production: :func:`system_resolver` (delegates to
# :func:`socket.getaddrinfo`). Tests: deterministic stubs.
Resolver = Callable[[str, int], Iterable[str]]


def system_resolver(host: str, port: int) -> Iterable[str]:
    """Default resolver — :func:`socket.getaddrinfo` (TCP-stream sockets).

    Returns every distinct address string :func:`socket.getaddrinfo`
    yields, preserving order. Order matters: a split-horizon DNS that
    serves one public and one private A record cannot mask the
    private leg by ordering it second — :func:`resolve_public_address`
    inspects the full list.

    Raises :class:`FetchGuardBlocked` on :class:`socket.gaierror` so
    callers see a uniform exception type for "we couldn't reach DNS"
    versus "DNS gave us a forbidden address".
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchGuardBlocked(
            f"DNS resolution failed for {host!r}: {exc}",
            reason="dns_error",
        ) from exc
    seen: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not isinstance(sockaddr, tuple) or len(sockaddr) < 2:
            continue
        ip = sockaddr[0]
        if isinstance(ip, str) and ip not in seen:
            seen.append(ip)
    return seen


def resolve_public_address(
    host: str,
    port: int,
    *,
    resolver: Resolver = system_resolver,
    allow_private_hosts: bool = False,
) -> str:
    """Resolve ``host`` and return one pinned, public IP — or raise.

    The full result set is inspected, not just the first entry: a
    split-horizon DNS that returns one public and one private record
    is rejected outright on the private leg, because a *re-resolve*
    later in the request lifetime could easily flip to it. Callers
    that pin the returned IP into the connection (the entire point of
    this module) defeat the rebind even if the resolver would lie a
    second time.

    ``allow_private_hosts`` is the dev / e2e escape hatch. It is
    accepted by callers that need to point a feed / webhook at an
    in-cluster service during local testing, but the actual gate
    requires the env var :data:`ENV_ALLOW_PRIVATE` *also* set to a
    truthy value. A single flip is not enough — defence in depth
    against accidentally hard-coding the kwarg in production code.
    """
    addresses = list(resolver(host, port))
    if not addresses:
        raise FetchGuardBlocked(
            f"DNS returned no addresses for {host!r}",
            reason="empty_dns",
        )
    if allow_private_hosts and _env_allow_private():
        return addresses[0]
    for ip in addresses:
        if not is_public_ip(ip):
            raise FetchGuardBlocked(
                f"host {host!r} resolved to non-public address {ip!r}",
                reason="private_address",
            )
    return addresses[0]


def _env_allow_private() -> bool:
    """Return ``True`` iff :data:`ENV_ALLOW_PRIVATE` is set truthy.

    Truthy = ``1``, ``true``, ``yes`` (case-insensitive). Anything
    else, including unset, is ``False``.
    """
    raw = os.environ.get(ENV_ALLOW_PRIVATE, "").strip().lower()
    return raw in {"1", "true", "yes"}


# ---------------------------------------------------------------------------
# DNS-pinned httpcore backend
# ---------------------------------------------------------------------------


class PinnedDnsBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that ignores ``host`` for TCP connect.

    Wraps an upstream :class:`httpcore.AsyncNetworkBackend`. Every
    :meth:`connect_tcp` call is rewritten to dial the pre-resolved IP
    instead of whatever hostname httpcore extracted from the URL.
    The upstream server still sees the original hostname via the
    ``Host:`` header (httpcore writes it itself) and via TLS SNI
    (the SSL context is configured by the wrapping
    :class:`httpx.AsyncClient` and uses the URL's host, not the IP).

    The defence: even if the kernel re-resolves the hostname mid-
    request — DNS rebinding's classic TOCTOU — our connect target is
    fixed at the validated IP. Re-resolution to ``127.0.0.1`` cannot
    redirect the open connection.
    """

    def __init__(
        self,
        *,
        resolved_ip: str,
        upstream: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolved_ip = resolved_ip
        self._upstream: httpcore.AsyncNetworkBackend = (
            upstream if upstream is not None else _default_async_backend()
        )

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # Discard the caller-supplied host; pin to the validated IP.
        # The upstream backend opens the TCP socket; httpx's TLS
        # wrapper above this layer still sees the original hostname
        # (passed via the request URL) for SNI + cert verification.
        return await self._upstream.connect_tcp(
            host=self._resolved_ip,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # Unix sockets bypass DNS entirely — there is no SSRF surface
        # here. We still reject them by default because :func:`safe_fetch`
        # only constructs HTTP URLs, but if a future caller wires this
        # backend into a UDS-aware client we let the upstream handle it.
        return await self._upstream.connect_unix_socket(
            path=path,
            timeout=timeout,
            socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        await self._upstream.sleep(seconds)


def _default_async_backend() -> httpcore.AsyncNetworkBackend:
    """Return httpcore's own auto-detected default network backend.

    Mirrors what :class:`httpcore.AsyncConnectionPool` would pick if
    we passed ``network_backend=None`` — :class:`AutoBackend` resolves
    at runtime to ``AnyIOBackend`` on asyncio (the crew.day default)
    and to ``TrioBackend`` if the caller is running under trio.
    Hard-coding :class:`AnyIOBackend` here would silently break a
    future trio-based caller; deferring to httpcore's own auto-pick
    keeps the wrapper agnostic.
    """
    # ``httpcore._backends.auto`` is a private import path, exposed
    # here because httpcore does not publish a stable name for "the
    # default async backend factory". If httpcore ever publishes one
    # we should switch to it.
    from httpcore._backends.auto import AutoBackend

    return AutoBackend()


# ---------------------------------------------------------------------------
# Async fetch helper
# ---------------------------------------------------------------------------


HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


async def safe_fetch(
    url: str,
    *,
    method: HttpMethod = "GET",
    headers: Mapping[str, str] | None = None,
    content: bytes | str | None = None,
    json: object | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    allowed_schemes: Iterable[str] = PUBLIC_FETCH_SCHEMES,
    allow_private_hosts: bool = False,
    resolver: Resolver = system_resolver,
    ssl_context: ssl.SSLContext | None = None,
) -> httpx.Response:
    """Issue one HTTP request through the SSRF guard.

    Pipeline:

    1. Parse ``url``. Reject the scheme if not in ``allowed_schemes``.
    2. Resolve the host via ``resolver`` (off-loop via
       :func:`anyio.to_thread.run_sync` so the event loop is not
       blocked on a synchronous DNS call).
    3. Reject the request if any address is non-public — unless the
       caller passed ``allow_private_hosts=True`` AND the env var
       :data:`ENV_ALLOW_PRIVATE` is set truthy.
    4. Open an :class:`httpx.AsyncClient` whose pool's network
       backend is :class:`PinnedDnsBackend`, so the TCP connect goes
       to the validated IP. SNI + cert-verify still see the original
       hostname.
    5. Stream the response body, aborting if it exceeds
       ``max_body_bytes``. The returned :class:`httpx.Response` has
       its body buffered into ``response.content`` for caller
       convenience.
    6. Translate :class:`httpx.TimeoutException` to
       :class:`FetchGuardTimeout`.

    Redirects are **not** followed (``follow_redirects=False``). A
    redirect target's host would need to be re-resolved and re-pinned
    before connect, which the per-feature wrappers do explicitly
    (see :class:`app.adapters.ical.validator.HttpxIcalValidator`'s
    redirect loop). Letting httpx auto-follow would silently bypass
    that re-pin and re-validate. Callers that need redirect handling
    must call :func:`safe_fetch` recursively against the ``Location``
    header themselves.

    The opened :class:`httpx.AsyncClient` is per-call. New callers
    that issue many requests should build their own long-lived
    client around the same :class:`PinnedDnsBackend`; that pattern
    is out of scope for the current bring-up but trivial to layer
    on top.
    """
    parsed = httpx.URL(url)
    assert_allowed_scheme(parsed.scheme, allowed=allowed_schemes)

    host = parsed.host
    if not host:
        raise FetchGuardBlocked(f"URL has no host: {url!r}", reason="no_host")
    port = parsed.port if parsed.port is not None else _default_port(parsed.scheme)

    # Resolve off the event loop — :func:`socket.getaddrinfo` is
    # synchronous and can take seconds on a misbehaving DNS.
    resolved_ip = await anyio.to_thread.run_sync(
        lambda: resolve_public_address(
            host,
            port,
            resolver=resolver,
            allow_private_hosts=allow_private_hosts,
        )
    )

    # Build the DNS-pinned transport. ``ssl_context`` defaults to
    # httpx's hardened default (verify=True, check_hostname=True).
    transport = _build_pinned_transport(
        resolved_ip=resolved_ip,
        ssl_context=ssl_context,
    )

    timeout = httpx.Timeout(timeout_seconds)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            return await _stream_with_cap(
                client=client,
                method=method,
                url=url,
                headers=dict(headers) if headers else None,
                content=content,
                json=json,
                max_body_bytes=max_body_bytes,
            )
    except httpx.TimeoutException as exc:
        raise FetchGuardTimeout(f"timeout fetching {url!r}: {exc}") from exc


def _default_port(scheme: str) -> int:
    """Map ``scheme`` to its default port."""
    if scheme.lower() == "https":
        return 443
    return 80


def _build_pinned_transport(
    *,
    resolved_ip: str,
    ssl_context: ssl.SSLContext | None,
) -> httpx.AsyncHTTPTransport:
    """Construct an :class:`httpx.AsyncHTTPTransport` with a pinned-DNS pool.

    Replaces the transport's internal ``_pool`` with an
    :class:`httpcore.AsyncConnectionPool` backed by
    :class:`PinnedDnsBackend`. Touching ``_pool`` is private-API
    territory, but httpx exposes no public hook for "swap the network
    backend after construction"; doing it this way keeps the rest of
    the transport's behaviour (HTTP/1.1, connection limits, retries)
    intact.
    """
    transport = httpx.AsyncHTTPTransport(
        verify=ssl_context if ssl_context is not None else True,
    )
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=ssl_context,
        network_backend=PinnedDnsBackend(resolved_ip=resolved_ip),
    )
    return transport


async def _stream_with_cap(
    *,
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: Mapping[str, str] | None,
    content: bytes | str | None,
    json: object | None,
    max_body_bytes: int,
) -> httpx.Response:
    """Issue the request as a stream and abort if the body exceeds the cap.

    A streaming read is the only way to bound *malicious* upstream
    behaviour: a cooperative server respects ``Content-Length``, but
    an attacker controls neither header nor body shape. We read in
    64 KiB chunks and tear down the connection the moment cumulative
    bytes exceed ``max_body_bytes``.
    """
    chunks: list[bytes] = []
    total = 0
    chunk_size = 64 * 1024
    request = client.build_request(
        method=method,
        url=url,
        headers=headers,
        content=content,
        json=json,
    )
    response = await client.send(request, stream=True)
    try:
        async for chunk in response.aiter_bytes(chunk_size=chunk_size):
            total += len(chunk)
            if total > max_body_bytes:
                raise FetchGuardSizeLimit(
                    f"response body exceeded cap of {max_body_bytes} bytes"
                )
            chunks.append(chunk)
    finally:
        await response.aclose()
    # ``response`` is consumed; reattach the buffered body so the
    # caller can use ``response.content`` / ``response.text`` as if
    # it were a normal non-streaming response.
    response._content = b"".join(chunks)
    return response
