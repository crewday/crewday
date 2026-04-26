"""Regression: HttpMetricsMiddleware reads the bound workspace_id.

The middleware-ordering bug fixed during cd-24tp self-review:
``HttpMetricsMiddleware`` was mounted OUTSIDE
``WorkspaceContextMiddleware`` in the factory chain, but
``BaseHTTPMiddleware`` resets the inner middleware's ContextVar
binding in its ``finally`` block BEFORE control returns to the
outer middleware (the inner ``finally`` runs as part of the inner
``dispatch``, not after it). As a result ``_workspace_label()`` —
called by ``HttpMetricsMiddleware`` AFTER ``await call_next``
returns — observed ``get_current() is None`` and emitted an empty
``workspace_id`` label on every HTTP counter increment.

The fix moves the metrics middleware INSIDE the workspace-context
middleware so the binding is still live when the metric is bumped.

This module pins the ordering as an invariant so a future refactor
can't silently regress.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.api.middleware.metrics import HttpMetricsMiddleware
from app.observability.metrics import HTTP_REQUESTS_TOTAL, METRICS_REGISTRY
from app.tenancy import WorkspaceContext
from app.tenancy.current import reset_current, set_current

# Realistic high-entropy ULID — avoid contrived all-zero bodies
# because the central PII redactor's PAN regex (Luhn over 13-19
# contiguous digits) matches a long zero tail; a hostile fixture
# would mask the bug under test rather than the bug being audited.
_WORKSPACE_ID = "01KQ3HF5QR6SX6PDC4XPGGDFC1"


class _SetWorkspaceCtxMiddleware(BaseHTTPMiddleware):
    """Test-only middleware that binds a known WorkspaceContext.

    Mirrors :class:`app.tenancy.middleware.WorkspaceContextMiddleware`'s
    set + reset lifecycle so the ordering question this test asks
    matches the production layout exactly.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        ctx = WorkspaceContext(
            workspace_id=_WORKSPACE_ID,
            workspace_slug="ordering-test",
            actor_id="01KQ3HF5QR6SX6PDC4XPGGDFCA",
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id="01KQ3HF5QR6SX6PDC4XPGGDFCC",
        )
        token = set_current(ctx)
        try:
            return await call_next(request)
        finally:
            reset_current(token)


@pytest.fixture(autouse=True)
def _reset_counter() -> Iterator[None]:
    HTTP_REQUESTS_TOTAL.clear()
    yield
    HTTP_REQUESTS_TOTAL.clear()


def _build_app(*, metrics_inside: bool) -> FastAPI:
    """Build a minimal app with the two middlewares in either order.

    ``metrics_inside=True`` matches the post-fix factory layout:
    HttpMetrics is added FIRST (becomes innermost), so at request
    time it sits INSIDE the workspace-context binding and observes
    the bound workspace_id when reading after ``call_next``.

    ``metrics_inside=False`` is the pre-fix bug shape kept here so
    the regression test can ALSO assert the failure mode: when
    HttpMetrics sits OUTSIDE WorkspaceContext, the inner
    middleware's ``finally`` runs before control returns and the
    label comes back empty.
    """
    app = FastAPI()
    if metrics_inside:
        # Innermost first. HttpMetrics added first → innermost at
        # request time → INSIDE WorkspaceCtx → reads bound ctx.
        app.add_middleware(HttpMetricsMiddleware)
        app.add_middleware(_SetWorkspaceCtxMiddleware)
    else:
        # The pre-fix arrangement. WorkspaceCtx added first →
        # innermost → resets ctx in finally before HttpMetrics
        # observes it.
        app.add_middleware(_SetWorkspaceCtxMiddleware)
        app.add_middleware(HttpMetricsMiddleware)

    @app.get("/echo")
    def echo() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def _scrape_workspace_label_for(route: str) -> str | None:
    """Return the workspace_id label observed for ``route`` in the
    current registry, or ``None`` if no counter line matches."""
    from prometheus_client import generate_latest

    body = generate_latest(METRICS_REGISTRY).decode("utf-8")
    for line in body.splitlines():
        if not line.startswith("crewday_http_requests_total{"):
            continue
        if f'route="{route}"' not in line:
            continue
        # Parse the workspace_id label out of the line.
        marker = 'workspace_id="'
        start = line.find(marker)
        if start < 0:
            continue
        start += len(marker)
        end = line.find('"', start)
        return line[start:end]
    return None


class TestMetricsMiddlewareReadsBoundWorkspace:
    """The post-fix ordering DOES surface the bound workspace_id."""

    def test_metrics_inside_workspace_ctx_reads_bound_label(self) -> None:
        app = _build_app(metrics_inside=True)
        client = TestClient(app)
        resp = client.get("/echo")
        assert resp.status_code == 200
        observed = _scrape_workspace_label_for("/echo")
        assert observed == _WORKSPACE_ID, (
            "HttpMetricsMiddleware mounted INSIDE WorkspaceContext must "
            "observe the bound workspace_id after call_next returns. "
            f"Got: {observed!r}"
        )

    def test_metrics_outside_workspace_ctx_loses_label(self) -> None:
        """The pre-fix shape — kept as a guard against silent regressions.

        If a future refactor flips the middleware order back, this
        test passes (the label is empty) but
        :meth:`test_metrics_inside_workspace_ctx_reads_bound_label`
        fails — making the regression visible.
        """
        app = _build_app(metrics_inside=False)
        client = TestClient(app)
        resp = client.get("/echo")
        assert resp.status_code == 200
        observed = _scrape_workspace_label_for("/echo")
        assert observed == "", (
            "Reference assertion documenting the pre-fix bug: when "
            "HttpMetricsMiddleware sits OUTSIDE WorkspaceContext the "
            "inner middleware's finally resets the ContextVar before "
            f"the outer reads it. Got: {observed!r}"
        )
