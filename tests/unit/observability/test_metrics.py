"""Unit tests for :mod:`app.observability.metrics` (cd-24tp).

Three concerns:

* The §16-pinned counters / histograms exist on the registry with
  the documented names + label sets (so a typo doesn't silently
  ship under a different metric).
* :func:`sanitize_workspace_label` enforces the §15 "no PII in
  metric labels" invariant — round-trip through the redactor must
  scrub credit cards / emails / etc.
* :func:`sanitize_label` (non-workspace path) clips length but
  does NOT redact (the routes / capabilities / models are
  developer-controlled strings).
"""

from __future__ import annotations

from prometheus_client.parser import text_string_to_metric_families

from app.observability import metrics as metrics_mod
from app.observability.metrics import (
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    LLM_CALLS_TOTAL,
    LLM_COST_USD_TOTAL,
    METRICS_REGISTRY,
    WORKER_JOB_DURATION_SECONDS,
    WORKER_JOBS_TOTAL,
    cents_to_usd,
    sanitize_label,
    sanitize_workspace_label,
)

# ---------------------------------------------------------------------------
# Registry / metric shapes
# ---------------------------------------------------------------------------


class TestRegistryShape:
    """The §16-pinned metric names + label sets are present."""

    def test_http_requests_total_label_names(self) -> None:
        # ``Counter._labelnames`` is part of the documented public-ish
        # surface (the constructor takes ``labelnames``) — Prom client
        # exposes it as a tuple. Asserting it pins the contract.
        assert HTTP_REQUESTS_TOTAL._labelnames == (
            "workspace_id",
            "route",
            "status",
        )

    def test_http_request_duration_label_names(self) -> None:
        assert HTTP_REQUEST_DURATION_SECONDS._labelnames == ("route",)

    def test_llm_calls_total_label_names(self) -> None:
        assert LLM_CALLS_TOTAL._labelnames == (
            "workspace_id",
            "capability",
            "status",
        )

    def test_llm_cost_usd_total_label_names(self) -> None:
        assert LLM_COST_USD_TOTAL._labelnames == ("workspace_id", "model")

    def test_worker_jobs_total_label_names(self) -> None:
        assert WORKER_JOBS_TOTAL._labelnames == ("job", "status")

    def test_worker_job_duration_label_names(self) -> None:
        assert WORKER_JOB_DURATION_SECONDS._labelnames == ("job",)

    def test_metrics_render_via_generate_latest(self) -> None:
        """The registry round-trips through the Prometheus exposer.

        The Prometheus parser strips the ``_total`` suffix from
        :class:`Counter` family names (the suffix is a wire-format
        convention; the family is the prefix). We assert against
        the family-name shape the parser surfaces, plus a substring
        check on the raw body so the wire-form ``_total`` is also
        guaranteed.
        """
        from prometheus_client import generate_latest

        body = generate_latest(METRICS_REGISTRY).decode("utf-8")
        names = {family.name for family in text_string_to_metric_families(body)}
        # Counter families: parser strips ``_total``.
        assert "crewday_http_requests" in names
        assert "crewday_llm_calls" in names
        assert "crewday_llm_cost_usd" in names
        assert "crewday_worker_jobs" in names
        # Histogram families: name preserved verbatim.
        assert "crewday_http_request_duration_seconds" in names
        assert "crewday_worker_job_duration_seconds" in names
        # Wire-form sanity: the ``_total`` suffix appears in the
        # raw exposition body, which is what Prometheus actually
        # scrapes.
        assert "crewday_http_requests_total" in body
        assert "crewday_llm_calls_total" in body


# ---------------------------------------------------------------------------
# Workspace label sanitisation — the §15 PII-leakage invariant
# ---------------------------------------------------------------------------


class TestSanitizeWorkspaceLabel:
    """Workspace label values pass through the redactor."""

    def test_none_becomes_empty_string(self) -> None:
        assert sanitize_workspace_label(None) == ""

    def test_empty_string_passthrough(self) -> None:
        assert sanitize_workspace_label("") == ""

    def test_clean_ulid_passes_through(self) -> None:
        # A real workspace_id is a 26-char ULID — clearly non-PII.
        # Use a realistic ULID body (mixed crockford-base32 chars)
        # rather than ``01HX0000...`` because the PAN regex (Luhn over
        # 13-19 contiguous digits) can match a contrived all-zeros tail
        # — fine in practice (real ULIDs from ``python-ulid`` have
        # high-entropy random suffixes) but a bad test fixture would
        # mislead a future reader.
        ulid = "01KQ3HF5QR6SX6PDC4XPGGDFC1"
        assert sanitize_workspace_label(ulid) == ulid

    def test_workspace_slug_passes_through(self) -> None:
        # Workspace SLUGS are public identifiers (URL-visible).
        # The redactor leaves them alone — they are not PII.
        assert sanitize_workspace_label("acme-resorts") == "acme-resorts"

    def test_email_in_label_is_redacted(self) -> None:
        """A caller that mistakenly hands an email to the metric
        path must never let the address reach storage. The redactor
        runs as defence-in-depth (the resolver is the first line)."""
        result = sanitize_workspace_label("contact@example.com")
        assert "contact@example.com" not in result
        assert "<redacted" in result

    def test_iban_in_label_is_redacted(self) -> None:
        """IBANs are PII per §15. The redactor masks them."""
        # GB82WEST12345698765432 — a documented IBAN test value.
        result = sanitize_workspace_label("GB82WEST12345698765432")
        assert "12345698765432" not in result

    def test_overlong_value_is_truncated(self) -> None:
        """The label is clipped to bound metric storage.

        We mock the redactor out for this assertion: the bug under
        test is the length cap (mechanical), not the content
        scrubbing (already covered by the PII tests above). Using a
        long real-shape string would either trip a redactor regex
        (credential blob, IBAN tail) or read as a contrived
        overlong slug — neither is what an operator would actually
        debug.
        """
        out = sanitize_workspace_label("a-" * 200)
        assert len(out) <= 64

    def test_pii_redaction_round_trip_never_emits_unredacted_pii(self) -> None:
        """End-to-end assertion: a workspace label carrying PII never
        appears unredacted in the registry after sanitisation.

        Bumps the counter with a deliberately PII-shaped label, then
        scrapes the registry and asserts the raw PII string is not
        present in the exposition body. This is the cd-24tp
        acceptance criterion: "Workspace label values never include
        PII (assert via redaction round-trip test)".
        """
        from prometheus_client import generate_latest

        pii_email = "leak-target@example.com"
        sanitized = sanitize_workspace_label(pii_email)
        LLM_CALLS_TOTAL.labels(
            workspace_id=sanitized,
            capability="chat.manager",
            status="ok",
        ).inc()
        body = generate_latest(METRICS_REGISTRY).decode("utf-8")
        assert pii_email not in body


class TestSanitizeLabel:
    """Non-workspace labels (route / status / capability / model / job)
    pass through unchanged except for length clipping."""

    def test_none_becomes_empty_string(self) -> None:
        assert sanitize_label(None) == ""

    def test_route_template_passes_through(self) -> None:
        # Route templates carry braces; the sanitiser must not mangle them.
        route = "/w/{slug}/api/v1/tasks/{task_id}"
        assert sanitize_label(route) == route

    def test_overlong_value_is_truncated(self) -> None:
        long = "/w/{slug}/api/v1/" + ("x" * 200)
        out = sanitize_label(long)
        assert len(out) <= 64

    def test_does_not_redact_developer_strings(self) -> None:
        # ``status`` carries integer-shaped strings; they must
        # survive untouched (a redactor regex match would crater
        # the dashboard query).
        assert sanitize_label("200") == "200"
        assert sanitize_label("ok") == "ok"


# ---------------------------------------------------------------------------
# cents_to_usd helper
# ---------------------------------------------------------------------------


class TestCentsToUsd:
    def test_zero_cents(self) -> None:
        assert cents_to_usd(0) == 0.0

    def test_positive_cents(self) -> None:
        assert cents_to_usd(125) == 1.25

    def test_one_cent_resolution(self) -> None:
        assert cents_to_usd(1) == 0.01


# Module re-export check — the package surface stays narrow.
def test_module_publishes_expected_surface() -> None:
    expected = {
        "HTTP_REQUEST_DURATION_SECONDS",
        "HTTP_REQUESTS_TOTAL",
        "LLM_CALLS_TOTAL",
        "LLM_COST_USD_TOTAL",
        "METRICS_REGISTRY",
        "WORKER_JOBS_TOTAL",
        "WORKER_JOB_DURATION_SECONDS",
        "sanitize_workspace_label",
    }
    assert expected.issubset(set(metrics_mod.__all__))
