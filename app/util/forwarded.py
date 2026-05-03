"""Trusted-proxy aware source-IP resolution (cd-ca0u).

Behind a reverse proxy (Caddy, nginx, Traefik, Pangolin) the TCP peer
is the proxy, not the original client. Naively trusting
``X-Forwarded-For`` is a spoofing vector — any caller can set the
header, so an internal-only allowlist that matches against the XFF
value is open to the public. The crew.day fix is a bounded
trusted-proxy registry: operators declare the proxy CIDRs they
control; we honour ``X-Forwarded-For`` ONLY when the TCP peer falls
inside that registry, and otherwise treat the peer as the source IP.

Why the rightmost XFF entry, not the leftmost
---------------------------------------------

``X-Forwarded-For`` accumulates left-to-right: each hop appends. The
LEFTmost value is whatever the first proxy received from its peer —
which, on the public internet, is whatever the client claimed. The
RIGHTmost value is the IP the *last* proxy (ours) saw the connection
from, i.e. the IP we are choosing to trust by adding the proxy's
CIDR to the registry. Following OWASP's "Selecting an IP Address
from the X-Forwarded-For Header" guidance and the equivalent
RFC 7239 ``Forwarded`` reasoning, we walk RIGHT-to-LEFT until we
exit the trusted set; for the v1 single-hop deployments crew.day
targets, that simplifies to "take the rightmost entry".

This keeps the public path (no proxy) unchanged, gives operators a
spoof-safe knob for Recipe A (Caddy on host →
``CREWDAY_TRUSTED_PROXIES=127.0.0.1/32``), and covers IPv4 + IPv6
uniformly. See ``docs/specs/16-deployment-operations.md`` §"Reverse-
proxy caveat".

Single-hop assumption + caller semantics
----------------------------------------

The "rightmost entry" simplification is correct ONLY when there is
exactly one trusted-proxy hop in front of the app — the v1 deployment
shape crew.day targets. With a chained registry (e.g.
``client → CDN → Caddy → app`` where both CDN and Caddy land in
``CREWDAY_TRUSTED_PROXIES``), the rightmost XFF entry is the
closest-to-us hop, NOT the original client; an intermediate
untrusted hop could spoof everything to its left. A future
multi-hop iteration would walk RIGHT-to-LEFT while each successive
entry stays inside the registry, falling back to the first entry
the chain disagrees with.

What this means for callers:

* The ``/metrics`` CIDR gate is the spoof-safe consumer — it just
  needs an IP it's willing to allow, and "the proxy we chose to
  trust" is exactly that.
* Audit logs and rate-limit seams that want the **real client IP**
  must NOT take this helper's output verbatim under a multi-hop
  registry; either constrain the registry to a single hop, or wait
  for the multi-hop iteration to land before wiring those callers.
"""

from __future__ import annotations

import ipaddress
import logging

__all__ = ["parse_trusted_proxies", "resolve_source_ip"]

_log = logging.getLogger(__name__)

type _IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _unbracket(entry: str) -> str:
    """Strip a single ``[…]`` pair around a bare IPv6 literal.

    RFC 7239 brackets v6 addresses in the ``Forwarded`` header
    (``for="[2001:db8::1]"``); some proxies leak the same form into
    ``X-Forwarded-For``. :func:`ipaddress.ip_address` rejects the
    bracketed form, so without this defence the resolved source IP
    would be ``"[::1]"`` and the downstream allowlist check would
    silently fail to parse it. Anything that doesn't look like a
    bracketed-v6 literal passes through unchanged — we deliberately
    do NOT touch ``host:port`` or other shapes.
    """
    if len(entry) >= 2 and entry[0] == "[" and entry[-1] == "]":
        return entry[1:-1]
    return entry


def parse_trusted_proxies(raw: list[str]) -> tuple[_IPNetwork, ...]:
    """Parse a list of CIDR strings into v4/v6 network objects.

    Mirrors :func:`app.observability.endpoint._resolve_allow_cidrs`:
    invalid entries log at WARNING and are dropped so a typo in
    ``CREWDAY_TRUSTED_PROXIES`` does not crash the boot, but
    operators get a clear signal in the JSON log stream.
    """
    parsed: list[_IPNetwork] = []
    for entry in raw:
        try:
            parsed.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            _log.warning(
                "trusted-proxy CIDR entry rejected; ignoring",
                extra={"event": "trusted_proxies.invalid", "cidr": entry},
            )
    return tuple(parsed)


def _peer_in_trusted(
    peer_ip: str,
    trusted_proxies: tuple[_IPNetwork, ...],
) -> bool:
    """Return True if ``peer_ip`` falls inside any trusted-proxy CIDR.

    Cross-family lookups (v4 peer against v6-only registry, or vice
    versa) return False — :class:`ipaddress.IPv4Address` is never
    ``in`` an :class:`ipaddress.IPv6Network`, which is the correct
    semantics here: an IPv4 proxy entry does not implicitly trust an
    IPv6 peer.
    """
    try:
        addr = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    return any(addr in net for net in trusted_proxies)


def resolve_source_ip(
    peer_ip: str | None,
    forwarded_for: str | None,
    trusted_proxies: tuple[_IPNetwork, ...],
) -> str | None:
    """Return the request's source IP, honouring XFF only from trusted proxies.

    Args:
        peer_ip: TCP peer IP (``request.client.host``). ``None`` when
            the ASGI server didn't populate ``client`` (rare; treated
            as no source).
        forwarded_for: Raw ``X-Forwarded-For`` header value, or
            ``None`` when the header is absent.
        trusted_proxies: Output of :func:`parse_trusted_proxies`. An
            empty tuple disables XFF-honouring entirely (the safe
            default for a deployment with no reverse proxy).

    Returns:
        The resolved source IP as a string, or ``None`` when the
        peer itself was unknown.

    Behaviour:
        * ``peer_ip is None`` → ``None`` (no signal).
        * Peer NOT in any trusted-proxy CIDR → return ``peer_ip``;
          the XFF header is ignored entirely (spoof-safe path).
        * Peer IS trusted → take the rightmost entry of
          ``forwarded_for`` (after trimming whitespace); fall back
          to ``peer_ip`` when the header is missing or empty.
    """
    if peer_ip is None:
        return None
    if not _peer_in_trusted(peer_ip, trusted_proxies):
        return peer_ip
    if forwarded_for is None:
        return peer_ip
    entries = [item.strip() for item in forwarded_for.split(",") if item.strip()]
    if not entries:
        return peer_ip
    return _unbracket(entries[-1])
