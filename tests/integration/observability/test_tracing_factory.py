"""End-to-end tracing wiring through the factory (cd-24tp).

The unit suite in ``tests/unit/observability/test_tracing.py``
exercises :func:`app.observability.tracing.setup_tracing` directly.
This module asserts the factory wiring:

* With ``OTEL_EXPORTER_OTLP_ENDPOINT`` unset, building the app is
  a no-op for OTel — the global tracer provider stays at the
  default :class:`opentelemetry.trace.ProxyTracerProvider`.
* With the env var set, building the app installs the SDK
  :class:`~opentelemetry.sdk.trace.TracerProvider`.

Verifying actual span emission against a live collector is a
manual operator step — see ``docs/specs/16-deployment-operations.md``
for the Jaeger recipe.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Literal

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from pydantic import SecretStr

from app.api.factory import create_app
from app.config import Settings
from app.observability import tracing as tracing_mod
from app.observability.tracing import OTEL_ENDPOINT_ENV


class _NoopSpanExporter(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


def _settings(*, profile: Literal["prod", "dev"] = "prod") -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("tracing-factory-key"),
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
        metrics_enabled=False,
        metrics_allow_cidr=[],
    )


@pytest.fixture(autouse=True)
def _reset_tracing(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    tracing_mod._reset_for_tests()
    monkeypatch.delenv(OTEL_ENDPOINT_ENV, raising=False)
    monkeypatch.setattr(
        tracing_mod,
        "_build_span_processor",
        lambda _endpoint: SimpleSpanProcessor(_NoopSpanExporter()),
    )
    yield
    tracing_mod._reset_for_tests()


class TestFactoryTracingWiring:
    def test_unset_env_leaves_global_provider_alone(self) -> None:
        """No-op contract: building the app without the env var must
        not touch the OTel global tracer provider.

        We assert identity (``before is after``) rather than the
        concrete provider class because a prior test in the same
        process may already have installed the SDK provider — the
        OTel global provider is one-shot for the process lifetime
        and can't be reset between tests.
        """
        from opentelemetry import trace

        before = trace.get_tracer_provider()
        create_app(settings=_settings())
        after = trace.get_tracer_provider()
        assert after is before

    def test_set_env_installs_sdk_tracer_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the env var present the factory installs the SDK
        provider so OTel spans actually flow."""
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        monkeypatch.setenv(OTEL_ENDPOINT_ENV, "http://localhost:4317")
        create_app(settings=_settings())
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)
