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

**Reverse-proxy caveat (cd-24tp self-review):** The CIDR check
runs against ``request.client.host`` — the TCP peer. In any
deployment that fronts the app with a reverse proxy
(Caddy / nginx / Traefik / Pangolin) the peer is the proxy, not
the original client. Recipe A (Caddy on host →
``reverse_proxy 127.0.0.1:8000``) makes EVERY request appear to
come from loopback, so the default ``127.0.0.0/8`` allowlist
would let an external scraper hitting the proxy reach
``/metrics``. Operators behind a reverse proxy MUST either:

* Keep ``CREWDAY_METRICS_ENABLED=false`` (the default) and scrape
  Prometheus from a sidecar that talks to the app directly
  (bypassing the proxy), or
* Configure the reverse proxy to refuse external traffic to
  ``/metrics`` (nginx ``location = /metrics { allow ...; deny all; }``,
  Caddy ``@metrics path /metrics`` with an IP matcher), or
* Set ``CREWDAY_METRICS_ALLOW_CIDR`` to a tight, proxy-aware
  range that excludes loopback (e.g. just the scraper's
  internal Docker bridge CIDR).

We deliberately do NOT trust ``X-Forwarded-For`` here — without
a trusted-proxy registry an attacker can spoof the header. The
crewday spec carries no trusted-proxy seam today; adding one is
tracked as a Beads follow-up.

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

__all__ = ["build_metrics_router"]

_log = logging.getLogger(__name__)


# Default allow-list when the operator hasn't set one explicitly.
# Loopback covers the same-host scrape Recipe A documents; the
# Tailscale CGNAT range covers Recipe B / D where the scraper sits
# on a private mesh. Pinned as a module constant so the resolution
# logic in :func:`_resolve_allow_cidrs` has a single source of truth.
_DEFAULT_ALLOW_CIDRS: Final[tuple[str, ...]] = (
    "127.0.0.0/8",
    "100.64.0.0/10",
)


def _resolve_allow_cidrs(settings: Settings) -> tuple[ipaddress.IPv4Network, ...]:
    """Return the parsed CIDR allow-list as :class:`IPv4Network` tuples.

    The settings value is a list of strings (parsed from the
    comma-separated ``CREWDAY_METRICS_ALLOW_CIDR`` env var); empty
    falls back to :data:`_DEFAULT_ALLOW_CIDRS`. Invalid entries log
    at WARNING and are dropped — a typo in the env var should not
    crash the boot, but operators need a clear signal in the JSON
    log stream.
    """
    raw = list(settings.metrics_allow_cidr) or list(_DEFAULT_ALLOW_CIDRS)
    parsed: list[ipaddress.IPv4Network] = []
    for entry in raw:
        try:
            parsed.append(ipaddress.IPv4Network(entry, strict=False))
        except ValueError:
            _log.warning(
                "metrics allow-cidr entry rejected; ignoring",
                extra={"event": "metrics.allow_cidr.invalid", "cidr": entry},
            )
    return tuple(parsed)


def _client_ip(request: Request) -> str | None:
    """Return the request's source IP (no proxy header trust).

    The §15 spec deliberately does NOT trust ``X-Forwarded-For`` —
    a client behind no proxy can spoof it freely. The metrics CIDR
    gate runs against the actual TCP peer
    (``request.client.host``). Operators fronting the app with a
    reverse proxy MUST scrape from the proxy's network and add
    that CIDR to the allowlist; the alternative is letting any
    public client claim a private source.
    """
    if request.client is None:
        return None
    return request.client.host


def _is_allowed(
    client_ip: str | None,
    cidrs: tuple[ipaddress.IPv4Network, ...],
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
        addr = ipaddress.IPv4Address(client_ip)
    except ValueError:
        # IPv6 callers fall through this branch — for v1 the spec
        # ships an IPv4-only allowlist (loopback + Tailscale CGNAT
        # are both v4). A future IPv6 deployment widens both the
        # default and this branch in lockstep.
        return False
    return any(addr in cidr for cidr in cidrs)


def build_metrics_router(*, settings: Settings) -> APIRouter:
    """Return the ``GET /metrics`` router with both gates applied.

    Built per-app so :class:`Settings` is captured by closure rather
    than read from a global on every request — matches the rest of
    the factory's settings-injection pattern (every router that
    needs settings takes them at construction time).
    """
    router = APIRouter()
    allow_cidrs = _resolve_allow_cidrs(settings)

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
