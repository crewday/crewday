"""Unit tests for the ``GET /metrics`` endpoint gating (cd-24tp).

Two gates per the spec:

1. :attr:`Settings.metrics_enabled` — when ``False``, return 404
   so a scanner cannot distinguish "metrics off" from "no such
   image" (the §15 enumeration-shield posture).
2. :attr:`Settings.metrics_allow_cidr` — request source IP MUST
   match an entry in the allowlist; non-match → 403. Defaults
   cover loopback + Tailscale CGNAT.

The endpoint also asserts the Prometheus exposition body actually
contains the §16 metrics under simulated load (a single counter
bump suffices to prove the path).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.observability.endpoint import build_metrics_router
from app.observability.metrics import (
    HTTP_REQUESTS_TOTAL,
    LLM_CALLS_TOTAL,
    METRICS_REGISTRY,
)


def _settings(
    *,
    metrics_enabled: bool = True,
    metrics_allow_cidr: list[str] | None = None,
    trusted_proxies: list[str] | None = None,
) -> Settings:
    """Build a minimal :class:`Settings` for the gating tests."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        metrics_enabled=metrics_enabled,
        metrics_allow_cidr=metrics_allow_cidr or [],
        trusted_proxies=trusted_proxies or [],
    )


def _build_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.include_router(build_metrics_router(settings=settings))
    return app


def _loopback_client(app: FastAPI) -> TestClient:
    """Build a :class:`TestClient` whose ASGI ``client`` tuple is loopback.

    The default :class:`starlette.testclient.TestClient` reports
    ``("testclient", 50000)`` as the client, which falls outside
    every CIDR allowlist. Pinning ``("127.0.0.1", ...)`` matches
    the production loopback path and makes the gating tests
    realistic.
    """
    return TestClient(app, client=("127.0.0.1", 12345))


def _external_client(app: FastAPI, *, ip: str = "203.0.113.42") -> TestClient:
    """Build a :class:`TestClient` whose source IP is outside the
    default allowlist (loopback / Tailscale CGNAT).

    ``203.0.113.0/24`` is the IETF-reserved TEST-NET-3 block per
    RFC 5737 — guaranteed not to be in any private-network CIDR
    a real deployment would allow.
    """
    return TestClient(app, client=(ip, 12345))


@pytest.fixture(autouse=True)
def _reset_default_counters() -> Iterator[None]:
    """Reset counter values between tests so assertions are local.

    Prometheus counters are append-only; tests that scrape and
    assert on a specific value need a clean baseline. Resetting
    individual counters by clearing their internal label dict is
    the documented escape hatch for test isolation.
    """
    HTTP_REQUESTS_TOTAL.clear()
    LLM_CALLS_TOTAL.clear()
    yield
    HTTP_REQUESTS_TOTAL.clear()
    LLM_CALLS_TOTAL.clear()


# ---------------------------------------------------------------------------
# Gate 1: settings.metrics_enabled
# ---------------------------------------------------------------------------


class TestMetricsEnabledGate:
    """``settings.metrics_enabled = False`` → 404 (not 403)."""

    def test_disabled_returns_404(self) -> None:
        app = _build_app(_settings(metrics_enabled=False))
        resp = _loopback_client(app).get("/metrics")
        assert resp.status_code == 404

    def test_disabled_returns_404_even_from_loopback(self) -> None:
        """The kill switch wins — even a loopback caller sees 404."""
        app = _build_app(
            _settings(
                metrics_enabled=False,
                metrics_allow_cidr=["127.0.0.0/8"],
            )
        )
        resp = _loopback_client(app).get("/metrics")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Gate 2: source-IP CIDR allowlist
# ---------------------------------------------------------------------------


class TestCidrAllowlistGate:
    """Non-allowed source IP returns 403."""

    def test_loopback_default_allowed(self) -> None:
        """The default allowlist covers 127.0.0.0/8."""
        app = _build_app(_settings())
        resp = _loopback_client(app).get("/metrics")
        assert resp.status_code == 200

    def test_tailscale_cgnat_default_allowed(self) -> None:
        """The default allowlist covers 100.64.0.0/10 (CGNAT / Tailscale)."""
        app = _build_app(_settings())
        # 100.72.198.118 is a Tailscale-shaped CGNAT address (matches
        # the §16 documented mesh range).
        resp = _external_client(app, ip="100.72.198.118").get("/metrics")
        assert resp.status_code == 200

    def test_external_ip_rejected_with_403(self) -> None:
        """An IP outside the allowlist returns 403."""
        app = _build_app(_settings())  # default loopback + Tailscale only
        resp = _external_client(app, ip="203.0.113.42").get("/metrics")
        assert resp.status_code == 403

    def test_loopback_blocked_when_allowlist_excludes_it(self) -> None:
        """Custom allowlist that excludes loopback rejects loopback."""
        app = _build_app(_settings(metrics_allow_cidr=["10.0.0.0/8"]))
        resp = _loopback_client(app).get("/metrics")
        assert resp.status_code == 403

    def test_custom_cidr_with_loopback_allows_loopback(self) -> None:
        app = _build_app(_settings(metrics_allow_cidr=["127.0.0.1/32"]))
        resp = _loopback_client(app).get("/metrics")
        assert resp.status_code == 200

    def test_invalid_cidr_in_allowlist_is_logged_and_dropped(
        self,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        """A malformed CIDR entry doesn't crash the boot."""
        # Caplog binding for the observability namespace.
        from typing import cast

        cast(object, allow_propagated_log_capture)("app.observability.endpoint")
        with caplog.at_level("WARNING", logger="app.observability.endpoint"):
            app = _build_app(
                _settings(metrics_allow_cidr=["not-a-cidr", "127.0.0.0/8"])
            )
            resp = _loopback_client(app).get("/metrics")
        assert resp.status_code == 200
        # WARNING was emitted for the bad entry.
        assert any(
            "metrics allow-cidr entry rejected" in record.message
            for record in caplog.records
        )


# ---------------------------------------------------------------------------
# Trusted-proxy seam (cd-ca0u): X-Forwarded-For honoured ONLY when peer
# falls inside ``CREWDAY_TRUSTED_PROXIES``; otherwise ignored.
# ---------------------------------------------------------------------------


class TestTrustedProxySeam:
    """``CREWDAY_TRUSTED_PROXIES`` gates ``X-Forwarded-For`` trust."""

    def test_metrics_xff_rejected_when_peer_not_trusted(self) -> None:
        """A spoofed ``X-Forwarded-For`` from an untrusted peer is ignored.

        Untrusted peer outside the allowlist + XFF claiming loopback
        (which IS in the allowlist) → 403, because the gate uses the
        peer when no proxy is trusted. This is the spoof-safe path.
        """
        app = _build_app(_settings(trusted_proxies=[]))
        resp = _external_client(app, ip="203.0.113.42").get(
            "/metrics", headers={"X-Forwarded-For": "127.0.0.1"}
        )
        assert resp.status_code == 403

    def test_metrics_xff_honoured_when_peer_trusted(self) -> None:
        """Trusted-peer XFF resolves to the rightmost entry → allowlist match."""
        app = _build_app(
            _settings(
                metrics_allow_cidr=["127.0.0.0/8"],
                trusted_proxies=["10.0.0.0/8"],
            )
        )
        resp = _external_client(app, ip="10.0.0.5").get(
            "/metrics", headers={"X-Forwarded-For": "127.0.0.1"}
        )
        assert resp.status_code == 200

    def test_metrics_xff_honoured_but_xff_ip_not_in_allow_cidrs(self) -> None:
        """Both gates compose: XFF honoured for source, allowlist still gates."""
        app = _build_app(
            _settings(
                metrics_allow_cidr=["127.0.0.0/8"],
                trusted_proxies=["10.0.0.0/8"],
            )
        )
        resp = _external_client(app, ip="10.0.0.5").get(
            "/metrics", headers={"X-Forwarded-For": "203.0.113.42"}
        )
        assert resp.status_code == 403

    def test_metrics_xff_multi_header_takes_rightmost_across_headers(self) -> None:
        """Multiple ``X-Forwarded-For`` headers are joined before rightmost-pick.

        HTTP allows the same header to appear more than once; some
        proxies append a fresh ``X-Forwarded-For`` rather than
        extending the upstream value. The endpoint must consider every
        header value, not just the first — otherwise a later hop
        would not be considered.
        """
        app = _build_app(
            _settings(
                metrics_allow_cidr=["127.0.0.0/8"],
                trusted_proxies=["10.0.0.0/8"],
            )
        )
        # ``httpx`` (and the underlying ``TestClient``) flattens a list
        # value into a multi-header send. ``203.0.113.42`` (TEST-NET-3)
        # is OUTSIDE ``metrics_allow_cidr``; ``127.0.0.1`` (the second
        # header — i.e. the rightmost across headers) is INSIDE. If
        # the endpoint only looked at the first header, the gate would
        # reject; with the multi-header join in place, it allows.
        resp = _external_client(app, ip="10.0.0.5").get(
            "/metrics",
            headers=[
                ("X-Forwarded-For", "203.0.113.42"),
                ("X-Forwarded-For", "127.0.0.1"),
            ],
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Body shape — the actual Prometheus exposition
# ---------------------------------------------------------------------------


class TestMetricsBody:
    """Under simulated load the response carries the §16 counters."""

    def test_body_carries_text_format_content_type(self) -> None:
        app = _build_app(_settings())
        resp = _loopback_client(app).get("/metrics")
        assert resp.status_code == 200
        # Prometheus text format (0.0.4) carries this token.
        assert "text/plain" in resp.headers["content-type"]

    def test_body_carries_http_request_counter_under_load(self) -> None:
        """Bump the counter, scrape, expect the time series."""
        HTTP_REQUESTS_TOTAL.labels(
            workspace_id="01KQ3HF5QR6SX6PDC4XPGGDFC1",
            route="/w/{slug}/api/v1/tasks",
            status="200",
        ).inc()

        app = _build_app(_settings())
        resp = _loopback_client(app).get("/metrics")
        body = resp.text
        assert "crewday_http_requests_total" in body
        assert 'workspace_id="01KQ3HF5QR6SX6PDC4XPGGDFC1"' in body
        assert 'route="/w/{slug}/api/v1/tasks"' in body
        assert 'status="200"' in body

    def test_body_carries_llm_call_counter_under_load(self) -> None:
        LLM_CALLS_TOTAL.labels(
            workspace_id="01KQ3HF5QR6SX6PDC4XPGGDFC1",
            capability="chat.manager",
            status="ok",
        ).inc()

        app = _build_app(_settings())
        resp = _loopback_client(app).get("/metrics")
        assert "crewday_llm_calls_total" in resp.text
        assert 'capability="chat.manager"' in resp.text


def test_registry_singleton_is_shared() -> None:
    """The endpoint exposes the SAME registry as the rest of the app
    so a counter bumped from a domain caller shows up here."""
    from app.observability import METRICS_REGISTRY as exported

    assert exported is METRICS_REGISTRY
