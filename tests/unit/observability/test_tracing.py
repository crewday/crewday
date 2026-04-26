"""Unit tests for :mod:`app.observability.tracing` (cd-24tp).

Two contracts:

* When ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset, :func:`setup_tracing`
  is a documented no-op (returns ``False``, does not touch the OTel
  global provider).
* When the env var IS set, :func:`setup_tracing` installs an
  :class:`~opentelemetry.sdk.trace.TracerProvider` and the FastAPI /
  SQLAlchemy / httpx auto-instrumentations.

The exporter's actual gRPC dispatch is NOT tested here — that
requires a live collector. Manual verification via Jaeger is
documented in ``docs/specs/16-deployment-operations.md``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI

from app.observability import tracing as tracing_mod
from app.observability.tracing import OTEL_ENDPOINT_ENV, setup_tracing


@pytest.fixture(autouse=True)
def _reset_tracing_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Drop the install-once guard and clear the env var between tests."""
    tracing_mod._reset_for_tests()
    monkeypatch.delenv(OTEL_ENDPOINT_ENV, raising=False)
    yield
    tracing_mod._reset_for_tests()


class TestEndpointUnset:
    """``setup_tracing`` with no env var configured is a no-op."""

    def test_returns_false_when_env_var_missing(self) -> None:
        assert setup_tracing() is False

    def test_global_provider_is_unchanged(self) -> None:
        """A no-op call must not touch the OTel global provider.

        We snapshot ``trace.get_tracer_provider()`` before + after
        and assert identity.
        """
        from opentelemetry import trace

        before = trace.get_tracer_provider()
        setup_tracing()
        after = trace.get_tracer_provider()
        assert before is after


class TestEndpointSet:
    """``setup_tracing`` with the env var present installs the exporter."""

    def test_returns_true_when_env_var_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(OTEL_ENDPOINT_ENV, "http://localhost:4317")
        assert setup_tracing() is True

    def test_global_provider_is_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After install, the global provider is the SDK class
        we installed (not the default :class:`ProxyTracerProvider`)."""
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        monkeypatch.setenv(OTEL_ENDPOINT_ENV, "http://localhost:4317")
        setup_tracing()
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)

    def test_double_install_is_idempotent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calling twice with the same endpoint is a no-op."""
        monkeypatch.setenv(OTEL_ENDPOINT_ENV, "http://localhost:4317")
        first = setup_tracing()
        second = setup_tracing()
        assert first is True
        assert second is True

    def test_app_instrumentation_runs_without_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Passing a FastAPI app installs the FastAPI instrumentation."""
        monkeypatch.setenv(OTEL_ENDPOINT_ENV, "http://localhost:4317")
        app = FastAPI()
        # The contract is "doesn't raise" — the instrumentation
        # itself does not expose a "is this app instrumented" flag.
        assert setup_tracing(app) is True

    def test_re_install_with_different_endpoint_warns_and_keeps_original(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        """Re-binding emits a WARNING; the original endpoint is kept."""
        from typing import cast

        cast(object, allow_propagated_log_capture)("app.observability.tracing")

        monkeypatch.setenv(OTEL_ENDPOINT_ENV, "http://localhost:4317")
        setup_tracing()

        monkeypatch.setenv(OTEL_ENDPOINT_ENV, "http://elsewhere:4317")
        with caplog.at_level("WARNING", logger="app.observability.tracing"):
            setup_tracing()
        assert any(
            "endpoint already installed" in record.message for record in caplog.records
        )
