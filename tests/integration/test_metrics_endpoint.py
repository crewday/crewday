"""End-to-end ``/metrics`` endpoint test through the full factory (cd-24tp).

Drives :func:`app.api.factory.create_app` with `metrics_enabled=True`
+ a loopback CIDR allowlist, then asserts:

* ``GET /metrics`` returns 200 with Prometheus text format.
* The §16 metric families all appear in the body.
* The HTTP middleware bumps the request counter on a real call.
"""

from __future__ import annotations

from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.factory import create_app
from app.config import Settings
from app.observability.metrics import HTTP_REQUESTS_TOTAL, LLM_CALLS_TOTAL


def _settings(
    *,
    profile: Literal["prod", "dev"] = "prod",
    metrics_enabled: bool = True,
    metrics_allow_cidr: list[str] | None = None,
) -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("metrics-endpoint-integration-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        smtp_host=None,
        smtp_port=587,
        smtp_from=None,
        smtp_use_tls=False,
        log_level="INFO",
        cors_allow_origins=[],
        profile=profile,
        vite_dev_url="http://127.0.0.1:5173",
        metrics_enabled=metrics_enabled,
        metrics_allow_cidr=metrics_allow_cidr or [],
    )


def _client(app: FastAPI) -> TestClient:
    """TestClient pinned to a loopback source IP.

    The default ``("testclient", 50000)`` falls outside the metrics
    CIDR allowlist, so the gating tests need an explicit loopback
    pin to exercise the happy path.
    """
    return TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 12345))


def _external_client(app: FastAPI, *, ip: str = "203.0.113.42") -> TestClient:
    return TestClient(app, raise_server_exceptions=False, client=(ip, 12345))


@pytest.fixture(autouse=True)
def _reset_counters() -> None:
    HTTP_REQUESTS_TOTAL.clear()
    LLM_CALLS_TOTAL.clear()


# ---------------------------------------------------------------------------
# Endpoint shape — through the actual factory
# ---------------------------------------------------------------------------


class TestMetricsEndpointThroughFactory:
    def test_enabled_loopback_returns_200_with_prom_text(self) -> None:
        client = _client(create_app(settings=_settings()))
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        # The required §16 metric families.
        assert "crewday_http_requests_total" in resp.text
        assert "crewday_http_request_duration_seconds" in resp.text
        assert "crewday_llm_calls_total" in resp.text
        assert "crewday_llm_cost_usd_total" in resp.text
        assert "crewday_worker_jobs_total" in resp.text
        assert "crewday_worker_job_duration_seconds" in resp.text

    def test_disabled_returns_404(self) -> None:
        client = _client(create_app(settings=_settings(metrics_enabled=False)))
        resp = client.get("/metrics")
        assert resp.status_code == 404

    def test_external_ip_rejected_with_403(self) -> None:
        """An IP outside the allowlist returns 403 even when enabled."""
        client = _external_client(create_app(settings=_settings()))
        resp = client.get("/metrics")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# HTTP middleware end-to-end — a real call bumps the counter
# ---------------------------------------------------------------------------


class TestHttpMiddlewareUnderLoad:
    """A request through the factory bumps the §16 HTTP counter."""

    def test_health_probes_are_excluded_from_instrumentation(self) -> None:
        """``/healthz`` is on the excluded path list so a Kubernetes
        liveness probe does not crowd out real-traffic time series.
        """
        client = _client(create_app(settings=_settings()))
        client.get("/healthz")
        resp = client.get("/metrics")
        # No HTTP-counter line for ``/healthz`` — the path is excluded.
        # Look for the route label literal in the body.
        assert 'route="/healthz"' not in resp.text

    def test_workspace_id_label_carries_bound_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for the cd-24tp self-review middleware-ordering bug.

        ``HttpMetricsMiddleware`` runs INSIDE
        ``WorkspaceContextMiddleware`` so the workspace_id label
        reflects the bound context. Pre-fix ordering had the
        metrics middleware OUTSIDE, which observed
        ``get_current() is None`` after ``call_next`` returned and
        emitted an empty workspace label on every counter line.

        We use the Phase-0 stub (cd-iwsv) to bind a known
        workspace_id from a request header without setting up
        users / sessions / role grants — the stub is the
        documented escape hatch for tenancy-bound integration
        tests. The tenancy middleware reads from the
        :func:`~app.config.get_settings` lru_cached singleton, so
        the stub gate is set via env var rather than through the
        :class:`Settings` instance handed to :func:`create_app`.
        """
        from app.config import get_settings

        ws_id = "01KQ3HF5QR6SX6PDC4XPGGDFC1"
        # Phase-0 stub gate is read from get_settings() — flush its
        # cache and set the env var so the singleton picks it up.
        monkeypatch.setenv("CREWDAY_PHASE0_STUB_ENABLED", "1")
        get_settings.cache_clear()
        try:
            client = _client(create_app(settings=_settings()))
            # The Phase-0 stub resolves the workspace from a header,
            # but the request must still hit a path that runs the
            # tenancy middleware (i.e. NOT a SKIP_PATHS entry). Any
            # ``/w/<slug>/api/v1/...`` path qualifies; the response
            # status doesn't matter — we only need the metric bumped.
            client.get(
                "/w/ordering-test/api/v1/properties",
                headers={
                    "X-Test-Workspace-Id": ws_id,
                    "X-Test-Actor-Id": "01KQ3HF5QR6SX6PDC4XPGGDFCA",
                },
            )
            resp = client.get("/metrics")
        finally:
            # Clear so subsequent tests in the same process don't
            # inherit a stub-enabled singleton.
            get_settings.cache_clear()
        body = resp.text
        # The bound workspace_id MUST appear on the HTTP counter.
        # Pre-fix bug: ``workspace_id=""`` would appear instead.
        assert f'workspace_id="{ws_id}"' in body, (
            "Metrics middleware lost the bound workspace_id — likely "
            "a regression of the cd-24tp ordering fix (HttpMetrics "
            "must sit INSIDE WorkspaceContextMiddleware)."
        )

    def test_unknown_path_collapses_to_spa_catchall_template(self) -> None:
        """In the prod factory the SPA catch-all (``/{full_path:path}``)
        matches every non-API request, so an unknown path collapses
        to that route template — bounded cardinality without our
        fallback firing.

        The ``<unmatched>`` fallback is exercised by the unit tests
        in :mod:`tests.unit.observability.test_metrics_endpoint`
        which mount only the metrics router.
        """
        client = _client(create_app(settings=_settings()))
        client.get("/totally-not-a-real-route")
        resp = client.get("/metrics")
        body = resp.text
        # The SPA catch-all template OR the unmatched fallback —
        # both are bounded-cardinality outcomes; the spec invariant
        # is "no per-request URL in the label".
        assert 'route="/{full_path:path}"' in body or 'route="<unmatched>"' in body
