"""Optional OpenTelemetry trace exporter (§16 "Observability / Traces").

Off by default. The exporter installs only when
``OTEL_EXPORTER_OTLP_ENDPOINT`` is set in the process environment;
otherwise :func:`setup_tracing` is a documented no-op so the hot
path pays zero overhead in self-host deployments that don't run a
collector.

When the endpoint IS set, :func:`setup_tracing`:

1. Builds a :class:`~opentelemetry.sdk.trace.TracerProvider` with a
   :class:`~opentelemetry.sdk.trace.export.BatchSpanProcessor` wrapping
   an :class:`~opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter`
   pointed at the configured endpoint.
2. Sets that provider as the global one so any code reaching for
   :func:`opentelemetry.trace.get_tracer` sees it.
3. Installs the FastAPI / SQLAlchemy / httpx auto-instrumentations
   so every HTTP handler, DB query, and outbound HTTP call emits a
   span without further wiring.

Idempotent: a second call against the same endpoint is a no-op
(the global provider is set once; the auto-instrumentations are
registered once). A second call with a different endpoint logs a
WARNING and keeps the original — re-targeting the exporter is an
ops operation that requires a process restart, not a runtime swap.

The OTLP endpoint follows the upstream OpenTelemetry env-var
contract: ``OTEL_EXPORTER_OTLP_ENDPOINT`` (full URL), with the
gRPC exporter as the default transport. Operators preferring
HTTP/protobuf set ``OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf``;
the SDK reads that itself, no crewday-side handling needed.

See ``docs/specs/16-deployment-operations.md`` §"Observability /
Traces".
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from fastapi import FastAPI
    from opentelemetry.sdk.trace import SpanProcessor

__all__ = ["OTEL_ENDPOINT_ENV", "setup_tracing"]


_log = logging.getLogger(__name__)


# Env-var name that gates the exporter. Lifted verbatim from the
# upstream OTel SDK contract so operators familiar with the
# ecosystem find the same key. Pinned as a module constant so tests
# can read it back without re-typing the literal.
OTEL_ENDPOINT_ENV: Final[str] = "OTEL_EXPORTER_OTLP_ENDPOINT"


# One-shot guard: ``setup_tracing`` may be called multiple times
# (factory invoked twice in a test suite, lifespan re-fire on a
# supervised restart) but the OTel global provider must be
# installed exactly once. We track the resolved endpoint so a
# mid-run swap surfaces as a WARNING rather than silently re-
# binding a global that downstream code may have cached.
_INSTALLED_ENDPOINT: str | None = None


def setup_tracing(app: FastAPI | None = None) -> bool:
    """Wire the OTLP exporter + auto-instrumentations.

    Returns ``True`` if the exporter was installed (or was already
    installed under the same endpoint), ``False`` if no endpoint
    was configured and the call was a no-op.

    Pass ``app`` so the FastAPI auto-instrumentation can register
    against this specific application. The SQLAlchemy + httpx
    instrumentations are global (they hook every engine / client
    in the process) and don't need an app reference.
    """
    global _INSTALLED_ENDPOINT

    endpoint = os.environ.get(OTEL_ENDPOINT_ENV)
    if not endpoint:
        # No-op fast path. Logged at DEBUG so operators tracing a
        # boot that should have wired the exporter can grep for the
        # absence; INFO would be too chatty on every test run.
        _log.debug(
            "OpenTelemetry exporter disabled; %s is unset",
            OTEL_ENDPOINT_ENV,
            extra={"event": "tracing.disabled", "env": OTEL_ENDPOINT_ENV},
        )
        return False

    if _INSTALLED_ENDPOINT is not None:
        if endpoint != _INSTALLED_ENDPOINT:
            _log.warning(
                "OpenTelemetry endpoint already installed; ignoring re-binding to %s",
                endpoint,
                extra={
                    "event": "tracing.rebind_ignored",
                    "current": _INSTALLED_ENDPOINT,
                    "requested": endpoint,
                },
            )
        # Re-attach the FastAPI hook to this app instance even when
        # the global provider is already set — a second `create_app`
        # call (test suite, factory smoke) needs its own app
        # instrumented or every test using the new app gets no
        # spans.
        if app is not None:
            _instrument_app(app)
        return True

    _install_provider(endpoint)
    _instrument_globals()
    if app is not None:
        _instrument_app(app)

    _INSTALLED_ENDPOINT = endpoint
    _log.info(
        "OpenTelemetry exporter installed",
        extra={"event": "tracing.installed", "endpoint": endpoint},
    )
    return True


def _install_provider(endpoint: str) -> None:
    """Set the global :class:`TracerProvider` with an OTLP batch exporter.

    Imported lazily so :mod:`app.observability.tracing` can be
    imported in environments where the OTel SDK is missing
    (extremely lean test runners) without crashing — we only pay
    the import cost when the operator actually configured an
    endpoint.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider

    resource = Resource.create({SERVICE_NAME: "crewday"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(_build_span_processor(endpoint))
    trace.set_tracer_provider(provider)


def _build_span_processor(endpoint: str) -> SpanProcessor:
    """Build the production span processor.

    Tests monkeypatch this seam to avoid starting an OTLP exporter
    thread that retries against ``localhost:4317`` after pytest has
    already closed its captured streams.
    """
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    exporter = OTLPSpanExporter(endpoint=endpoint)
    return BatchSpanProcessor(exporter)


def _instrument_globals() -> None:
    """Register the SQLAlchemy + httpx auto-instrumentations.

    Lazy import for the same reason as :func:`_install_provider`.
    Both instrumentations are idempotent under the OTel API
    contract — the second call is a no-op.
    """
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()


def _instrument_app(app: FastAPI) -> None:
    """Attach the FastAPI auto-instrumentation to ``app``.

    Lazy import for the same reason as :func:`_install_provider`.
    Each FastAPI app gets instrumented exactly once; the underlying
    library guards against double-wrap.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)


def _reset_for_tests() -> None:
    """Drop the install-once guard so a test fixture can re-wire.

    Production code MUST NOT call this; it exists purely so the
    test suite can swap endpoints between cases without spinning
    up a fresh process. Resetting the OTel global provider itself
    is OTel-internal and not exposed; tests that need a clean
    provider should treat it as a one-shot per process.
    """
    global _INSTALLED_ENDPOINT
    _INSTALLED_ENDPOINT = None
