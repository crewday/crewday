"""Prometheus counter + histogram registry (§16 "Observability / Metrics").

Single module-level :class:`prometheus_client.CollectorRegistry`
exposing the §16-pinned metrics:

* ``crewday_http_requests_total{workspace_id, route, status}`` —
  Counter bumped by :class:`~app.api.middleware.metrics.HttpMetricsMiddleware`
  on every response.
* ``crewday_http_request_duration_seconds{route}`` — Histogram of
  HTTP-handler latency (seconds), observed by the same middleware.
* ``crewday_llm_calls_total{workspace_id, capability, status}`` —
  Counter bumped by :func:`~app.domain.llm.usage_recorder.record`
  for every recorded LLM dispatch.
* ``crewday_llm_cost_usd_total{workspace_id, model}`` — Counter
  incremented by the same recorder seam, summing the
  provider-reported cost in USD.
* ``crewday_worker_jobs_total{job, status}`` — Counter bumped per
  scheduler tick (status ``ok``/``error``).
* ``crewday_worker_job_duration_seconds{job}`` — Histogram of per-tick
  durations (seconds).

The registry is **per-process** — one in the API container, another
in the worker container under Recipe D (§16). Prometheus scrapes
each independently; aggregation across replicas happens at the
metrics backend, not in-app.

## Label hygiene

Workspace labels MUST pass through :func:`sanitize_workspace_label`
before reaching :meth:`Counter.labels`. The §15 "Logging and
redaction" contract bans per-user PII from any log-shape surface;
metric labels are a log-shape surface (the Prometheus text format
is grep-able from outside the app), so the redaction seam runs here
too. Workspace **slugs** are public identifiers (they appear in
URLs); workspace **ids** are ULIDs (no PII by construction). The
sanitiser:

1. Coerces ``None`` → ``""`` (Prometheus accepts the empty label
   value but the Python client refuses ``None``).
2. Hard-caps the length at :data:`_LABEL_MAX_LENGTH` chars. A
   pathological value (say, an attacker-supplied workspace slug
   that the slug validator should have rejected) cannot blow up
   metric storage.
3. Routes the value through the central
   :func:`app.util.redact.redact` redactor in ``"log"`` scope so a
   future caller that mistakenly hands an email or token to the
   workspace label gets the same masking the JSON-log filter
   applies. The double-belt-and-braces here is deliberate — the
   spec invariant is "no PII in metric labels", not "the resolver
   is correct"; the redactor is the second line of defence.

Non-workspace labels (``route``, ``status``, ``capability``,
``model``, ``job``) carry no PII by construction and pass through
the simpler :func:`_truncate` only.

See ``docs/specs/16-deployment-operations.md`` §"Observability /
Metrics" and ``docs/specs/15-security-privacy.md`` §"Logging and
redaction".
"""

from __future__ import annotations

from typing import Final

from prometheus_client import CollectorRegistry, Counter, Histogram

from app.util.redact import ConsentSet, redact

__all__ = [
    "HTTP_REQUESTS_TOTAL",
    "HTTP_REQUEST_DURATION_SECONDS",
    "LLM_CALLS_TOTAL",
    "LLM_COST_USD_TOTAL",
    "METRICS_REGISTRY",
    "WORKER_JOBS_TOTAL",
    "WORKER_JOB_DURATION_SECONDS",
    "sanitize_workspace_label",
]


# Labels are clipped to this length on emit. 64 chars covers any
# legitimate slug, route template, capability, or model id; an
# attacker-supplied longer string is silently truncated.
# Prometheus itself happily accepts longer values, but storage and
# scrape latency degrade with cardinality + label width.
_LABEL_MAX_LENGTH: Final[int] = 64


# Histogram buckets in seconds — defaults chosen for HTTP handler
# + worker-job latency. Covers the p50 / p99 envelope of every
# legitimate request shape: sub-millisecond healthcheck,
# sub-second SQL-bound API call, multi-second LLM dispatch, slow
# scheduler tick. Aligned with the Prometheus client's documented
# defaults (``DEFAULT_BUCKETS``) plus a 30 s ceiling for the slow
# LLM tail.
_DURATION_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)


# Single per-process registry. Constructing our own (rather than
# using :data:`prometheus_client.REGISTRY`) keeps tests hermetic —
# they instantiate a fresh registry without inheriting collectors
# from a previous suite — and lets the ``/metrics`` endpoint expose
# only crewday-owned counters without leaking the Python runtime's
# default ``process_*`` / ``python_*`` series. Operators who want
# those can scrape them via the standard library exposer; the
# crewday surface deliberately stays narrow.
METRICS_REGISTRY: Final[CollectorRegistry] = CollectorRegistry()


# --- HTTP -------------------------------------------------------------------

HTTP_REQUESTS_TOTAL: Final[Counter] = Counter(
    "crewday_http_requests_total",
    "Total HTTP requests handled by the API.",
    labelnames=("workspace_id", "route", "status"),
    registry=METRICS_REGISTRY,
)

HTTP_REQUEST_DURATION_SECONDS: Final[Histogram] = Histogram(
    "crewday_http_request_duration_seconds",
    "HTTP request handler duration (seconds).",
    labelnames=("route",),
    buckets=_DURATION_BUCKETS,
    registry=METRICS_REGISTRY,
)


# --- LLM -------------------------------------------------------------------

LLM_CALLS_TOTAL: Final[Counter] = Counter(
    "crewday_llm_calls_total",
    "Total LLM dispatches recorded by the post-flight seam.",
    labelnames=("workspace_id", "capability", "status"),
    registry=METRICS_REGISTRY,
)

LLM_COST_USD_TOTAL: Final[Counter] = Counter(
    "crewday_llm_cost_usd_total",
    "Cumulative LLM spend (USD), summed from provider-reported costs.",
    labelnames=("workspace_id", "model"),
    registry=METRICS_REGISTRY,
)


# --- Worker -----------------------------------------------------------------

WORKER_JOBS_TOTAL: Final[Counter] = Counter(
    "crewday_worker_jobs_total",
    "Total scheduler ticks executed (status ok | error).",
    labelnames=("job", "status"),
    registry=METRICS_REGISTRY,
)

WORKER_JOB_DURATION_SECONDS: Final[Histogram] = Histogram(
    "crewday_worker_job_duration_seconds",
    "Scheduler tick duration (seconds).",
    labelnames=("job",),
    buckets=_DURATION_BUCKETS,
    registry=METRICS_REGISTRY,
)


def _truncate(value: str) -> str:
    """Clip ``value`` to :data:`_LABEL_MAX_LENGTH` characters."""
    if len(value) <= _LABEL_MAX_LENGTH:
        return value
    return value[:_LABEL_MAX_LENGTH]


def sanitize_workspace_label(value: str | None) -> str:
    """Return a label-safe workspace identifier.

    Coerces ``None`` → ``""``, runs the value through
    :func:`app.util.redact.redact` so a stray PII string never
    reaches metric storage, and clips the result to
    :data:`_LABEL_MAX_LENGTH`.

    Workspace IDs (ULIDs) are public-by-construction and pass
    through untouched; the redaction step is defence-in-depth
    against a future caller that hands an email / token to this
    seam by mistake (see module docstring for the rationale).
    """
    if value is None:
        return ""
    redacted = redact(value, scope="log", consents=ConsentSet.none())
    if not isinstance(redacted, str):
        # ``redact`` preserves str → str by contract; the narrowing
        # is for the type checker. A non-str fallback is treated as
        # an empty label rather than crashing the metric path.
        return ""
    return _truncate(redacted)


def sanitize_label(value: str | None) -> str:
    """Return a label-safe non-workspace identifier (no PII redaction).

    Routes ``None`` to ``""`` and clips to :data:`_LABEL_MAX_LENGTH`.
    Unlike :func:`sanitize_workspace_label`, no PII redaction step:
    these labels (``route``, ``status``, ``capability``, ``model``,
    ``job``) carry no user input by construction.
    """
    if value is None:
        return ""
    return _truncate(value)


def cents_to_usd(cents: int) -> float:
    """Convert integer cents to USD float for the cost counter.

    The §11 "Cost tracking" model stores spend in integer cents to
    keep the ledger arithmetic exact; the metric label exposes USD
    so dashboards plot a familiar unit.
    """
    return cents / 100.0
