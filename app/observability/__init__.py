"""Observability seams — metrics + tracing.

Three independent surfaces, each gated by deployment configuration:

* :mod:`app.observability.metrics` — Prometheus collector registry
  with the §16 counters / histograms (HTTP, LLM, worker).
* :mod:`app.observability.tracing` — Optional OpenTelemetry trace
  exporter, controlled by the ``OTEL_EXPORTER_OTLP_ENDPOINT`` env
  var. Off by default; turning it on installs the FastAPI / SQLAlchemy
  / httpx auto-instrumentations.
* :mod:`app.observability.endpoint` — ``GET /metrics`` Prometheus
  scrape endpoint with two gates (settings flag + source-IP CIDR
  allowlist).

See ``docs/specs/16-deployment-operations.md`` §"Observability".
"""

from __future__ import annotations

from app.observability.endpoint import build_metrics_router
from app.observability.metrics import (
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    LLM_CALLS_TOTAL,
    LLM_COST_USD_TOTAL,
    METRICS_REGISTRY,
    WORKER_JOB_DURATION_SECONDS,
    WORKER_JOBS_TOTAL,
    sanitize_workspace_label,
)
from app.observability.tracing import setup_tracing

__all__ = [
    "HTTP_REQUESTS_TOTAL",
    "HTTP_REQUEST_DURATION_SECONDS",
    "LLM_CALLS_TOTAL",
    "LLM_COST_USD_TOTAL",
    "METRICS_REGISTRY",
    "WORKER_JOBS_TOTAL",
    "WORKER_JOB_DURATION_SECONDS",
    "build_metrics_router",
    "sanitize_workspace_label",
    "setup_tracing",
]
