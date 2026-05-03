"""``GET /metrics`` Prometheus scrape endpoint (§16 "Observability").

Two gates per the task / spec contract:

1. :attr:`Settings.metrics_enabled` — deployment-wide kill switch.
   When ``False``, the route returns 404 (not 403) so a scanner sees
   the same shape as a missing endpoint. Default **``False``**
   (fail-closed); SaaS recipes (§16 Recipe D) set
   ``CREWDAY_METRICS_ENABLED=true`` explicitly.
2. :attr:`Settings.metrics_allow_cidr` — CIDR allowlist matched
   against the request's source IP. Default
   ``["127.0.0.0/8", "100.64.0.0/10"]`` (loopback + Tailscale CGNAT)
   so a scrape from outside the trust boundary returns 403. Operators
   running Prometheus inside the same VPC populate this with the
   scraper's CIDR.

**Source-IP resolution (cd-ca0u):** the gate matches against the
output of :func:`app.util.forwarded.resolve_source_ip`. When the TCP
peer falls inside :attr:`Settings.trusted_proxies` we honour the
rightmost ``X-Forwarded-For`` entry; otherwise we fall back to
``request.client.host`` and ignore the header entirely. Operators
behind a reverse proxy (Caddy / nginx / Traefik / Pangolin) set
``CREWDAY_TRUSTED_PROXIES`` to the proxy's CIDR (Recipe A:
``127.0.0.1/32``); deployments with no proxy leave it empty and keep
the historical "TCP peer is source" behaviour. See
``docs/specs/16-deployment-operations.md`` §"Reverse-proxy caveat"
for the operator-side mitigations that compose with this seam.

The endpoint is unversioned — bare ``/metrics`` — because it is an
ops surface, not part of the §12 REST API. Prometheus's default
scrape path is ``/metrics`` and we follow that convention.

The response body is the standard Prometheus text-format exposition
of the per-process :data:`~app.observability.metrics.METRICS_REGISTRY`.

See ``docs/specs/16-deployment-operations.md`` §"Observability /
Metrics".
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Final

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import Settings
from app.observability.metrics import METRICS_REGISTRY
from app.util.forwarded import parse_trusted_proxies, resolve_source_ip

__all__ = ["build_metrics_router"]

_log = logging.getLogger(__name__)

type _IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


# Default allow-list when the operator hasn't set one explicitly.
# Loopback covers the same-host scrape Recipe A documents; the
# Tailscale CGNAT range covers Recipe B / D where the scraper sits
# on a private mesh. Pinned as a module constant so the resolution
# logic in :func:`_resolve_allow_cidrs` has a single source of truth.
_DEFAULT_ALLOW_CIDRS: Final[tuple[str, ...]] = (
    "127.0.0.0/8",
    "100.64.0.0/10",
)


def _resolve_allow_cidrs(settings: Settings) -> tuple[_IPNetwork, ...]:
    """Return the parsed CIDR allow-list as v4/v6 network tuples.

    The settings value is a list of strings (parsed from the
    comma-separated ``CREWDAY_METRICS_ALLOW_CIDR`` env var); empty
    falls back to :data:`_DEFAULT_ALLOW_CIDRS`. Invalid entries log
    at WARNING and are dropped — a typo in the env var should not
    crash the boot, but operators need a clear signal in the JSON
    log stream. v4 + v6 entries mix freely; the default stays v4-only
    because the documented mesh ranges (loopback + Tailscale CGNAT)
    are both v4.
    """
    raw = list(settings.metrics_allow_cidr) or list(_DEFAULT_ALLOW_CIDRS)
    parsed: list[_IPNetwork] = []
    for entry in raw:
        try:
            parsed.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            _log.warning(
                "metrics allow-cidr entry rejected; ignoring",
                extra={"event": "metrics.allow_cidr.invalid", "cidr": entry},
            )
    return tuple(parsed)


def _is_allowed(
    client_ip: str | None,
    cidrs: tuple[_IPNetwork, ...],
) -> bool:
    """Return True if ``client_ip`` falls inside any allowed CIDR.

    A missing client (test client without a TCP peer, ASGI server
    that doesn't populate ``request.client``) is rejected. The
    metrics endpoint is too sensitive to fall open on an
    introspection failure — operators who actually need the
    Test Client path can set up a known-loopback override.
    """
    if client_ip is None:
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(addr in cidr for cidr in cidrs)


def build_metrics_router(*, settings: Settings) -> APIRouter:
    """Return the ``GET /metrics`` router with both gates applied.

    Built per-app so :class:`Settings` is captured by closure rather
    than read from a global on every request — matches the rest of
    the factory's settings-injection pattern (every router that
    needs settings takes them at construction time). Both the CIDR
    allowlist and the trusted-proxy registry are parsed once here
    and reused per request.
    """
    router = APIRouter()
    allow_cidrs = _resolve_allow_cidrs(settings)
    trusted_proxies = parse_trusted_proxies(list(settings.trusted_proxies))

    def _client_ip(request: Request) -> str | None:
        peer = request.client.host if request.client is not None else None
        # Multi-value handling: HTTP allows the same header to appear
        # more than once (some proxies append a fresh ``X-Forwarded-For``
        # rather than extending the upstream one). ``Headers.get`` only
        # returns the first; ``getlist`` returns all. Comma-join so the
        # rightmost-pick in :func:`resolve_source_ip` sees the full
        # chain — for the /metrics gate this is mostly belt-and-braces,
        # but it's the correct shape for any future audit / rate-limit
        # consumer.
        forwarded_values = request.headers.getlist("X-Forwarded-For")
        forwarded_for = ", ".join(forwarded_values) if forwarded_values else None
        return resolve_source_ip(peer, forwarded_for, trusted_proxies)

    @router.get(
        "/metrics",
        # Hidden from the public OpenAPI surface — ops endpoint, not
        # part of the §12 REST API contract.
        include_in_schema=False,
    )
    def metrics(request: Request) -> Response:
        if not settings.metrics_enabled:
            # 404 (not 403) when the feature is off — a scanner
            # should not be able to distinguish "metrics disabled"
            # from "this image has no metrics endpoint".
            return Response(status_code=404)

        client_ip = _client_ip(request)
        if not _is_allowed(client_ip, allow_cidrs):
            _log.info(
                "metrics scrape rejected; source IP not in allow_cidr",
                extra={
                    "event": "metrics.scrape.forbidden",
                    "source_ip": client_ip,
                },
            )
            return Response(status_code=403)

        body = generate_latest(METRICS_REGISTRY)
        return Response(content=body, media_type=CONTENT_TYPE_LATEST)

    return router
