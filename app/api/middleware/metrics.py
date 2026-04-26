"""Prometheus HTTP-instrumentation middleware (§16 "Observability").

Bumps :data:`~app.observability.metrics.HTTP_REQUESTS_TOTAL` and
:data:`~app.observability.metrics.HTTP_REQUEST_DURATION_SECONDS`
on every response. Labels:

* ``workspace_id`` — pulled from the active
  :class:`~app.tenancy.WorkspaceContext` if one is bound (the
  tenancy middleware sets it for ``/w/<slug>/...`` paths). Empty
  string for bare-host paths (``/healthz``, ``/api/v1/auth/...``).
  All workspace ids run through
  :func:`~app.observability.metrics.sanitize_workspace_label` to
  enforce the §15 "no PII in metrics" invariant.
* ``route`` — the FastAPI route template (``"/w/{slug}/api/v1/tasks"``)
  rather than the actual path, so cardinality stays bounded. A
  request that misses every router falls back to ``"<unmatched>"``
  so a 404 storm cannot blow up the time series. The
  ``/metrics`` endpoint itself is excluded from instrumentation
  to avoid recursive observation (Prometheus scraping at 30 s
  cadence would otherwise dominate the histogram with synthetic
  load).
* ``status`` — the integer HTTP status as a string. Stringifying
  matches the Prometheus convention; integer labels would be
  rejected by the client.

Mounted **inside** :class:`~app.tenancy.middleware.WorkspaceContextMiddleware`
so the workspace_id label reflects the bound :class:`WorkspaceContext`
after ``call_next`` returns. ``BaseHTTPMiddleware`` resets the
inner middleware's ``ContextVar`` in ``finally`` BEFORE control
returns to the outer middleware (the inner ``finally`` runs as
part of the inner ``dispatch``, not after it), so a metrics
middleware mounted ABOVE WorkspaceContext would always observe
``get_current() is None`` and emit an empty ``workspace_id`` label
(cd-24tp self-review fix).

Routing has already stamped the matched route on ``request.scope``
by the time the metrics middleware reads it (Starlette routing is
inside every middleware), so the route-template lookup works at
this layer regardless of where in the chain we sit.

See ``docs/specs/16-deployment-operations.md`` §"Observability".
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match, Route

from app.observability.metrics import (
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    sanitize_label,
    sanitize_workspace_label,
)
from app.tenancy.current import get_current

__all__ = ["HttpMetricsMiddleware"]


# Routes excluded from instrumentation. The metrics endpoint itself
# is the obvious case (recursive observation would dominate the
# histogram). Health probes are excluded because Kubernetes / Docker
# health checks fire on a sub-second cadence and would crowd out
# meaningful HTTP traffic from operator dashboards.
_EXCLUDED_PATHS: Final[frozenset[str]] = frozenset(
    {"/metrics", "/healthz", "/readyz", "/version"}
)


def _resolve_route_template(request: Request) -> str:
    """Return the FastAPI route template for ``request`` or a fallback.

    Fast path: Starlette stamps the matched :class:`Route` on
    ``request.scope["route"]`` during routing. When present we
    read its ``.path`` directly — O(1), the common case for any
    request that successfully reached a handler.

    Slow path (no ``scope["route"]``): walks
    ``request.app.router.routes`` and asks each route to
    :meth:`~starlette.routing.Route.matches` the request scope.
    Hits when an exception was raised before routing completed
    (CORS rejection, malformed request line) — rare, and the
    O(N) walk is acceptable in those cases.

    Falling back to a ``<unmatched>`` sentinel keeps cardinality
    bounded — every unmatched path collapses to one time series,
    which is exactly what an operator wants for a 404 storm.

    We explicitly do NOT use ``request.url.path`` for the label —
    that would explode cardinality on any URL containing an id
    (UUIDs, slugs). A 1000-tenant deployment would build 1000
    distinct ``route`` time-series per endpoint without the
    template substitution.
    """
    matched = request.scope.get("route")
    if isinstance(matched, Route):
        return matched.path
    for route in request.app.router.routes:
        if not isinstance(route, Route):
            # Mounted sub-apps (StaticFiles, etc.) report a Mount,
            # not a Route. We skip them — a static-file request
            # collapses to ``<unmatched>``, which is fine: we don't
            # want a per-asset time series.
            continue
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return route.path
    return "<unmatched>"


class HttpMetricsMiddleware(BaseHTTPMiddleware):
    """Bump HTTP counters + histogram on every response."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if path in _EXCLUDED_PATHS:
            return await call_next(request)

        start = time.perf_counter()
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            # An unhandled exception still produces a 500 envelope
            # downstream of this middleware (the FastAPI exception
            # handlers wrap it). For our purposes the request DID
            # happen and SHOULD be counted; the actual response
            # status is whatever the framework emits, which is
            # almost certainly 500. Re-raise so the framework sees
            # the exception unchanged.
            HTTP_REQUESTS_TOTAL.labels(
                workspace_id=_workspace_label(),
                route=sanitize_label(_resolve_route_template(request)),
                status="500",
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(
                route=sanitize_label(_resolve_route_template(request)),
            ).observe(time.perf_counter() - start)
            raise

        route = sanitize_label(_resolve_route_template(request))
        HTTP_REQUESTS_TOTAL.labels(
            workspace_id=_workspace_label(),
            route=route,
            status=str(status),
        ).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(route=route).observe(
            time.perf_counter() - start
        )
        return response


def _workspace_label() -> str:
    """Return the active workspace id (sanitised), or empty string.

    Reads :func:`app.tenancy.current.get_current` so the lookup
    works whether the tenancy middleware ran above us
    (workspace-scoped path) or skipped us (bare-host path). Always
    routes through :func:`sanitize_workspace_label` — the §15
    invariant.
    """
    ctx = get_current()
    if ctx is None:
        return sanitize_workspace_label(None)
    return sanitize_workspace_label(ctx.workspace_id)
