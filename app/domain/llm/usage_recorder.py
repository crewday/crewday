"""Post-flight LLM-call recorder (cd-wjpl).

Thin orchestration seam between an LLM-client call site and the
:mod:`app.domain.llm.budget` module. For every call that left the
client, the caller:

1. Pre-flights via :func:`~app.domain.llm.budget.check_budget` (not
   owned by this module — the recorder is strictly post-flight).
2. Dispatches the call through its own adapter and waits for a
   response or terminal failure.
3. Calls :func:`record` with the provider's reported metrics, the
   §11 "LLMResult" telemetry (``fallback_attempts``,
   ``finish_reason``), and the §11 "Agent audit trail" attribution
   (delegating user, token id, agent label).

:func:`record` is pure orchestration — it assembles a domain
:class:`~app.domain.llm.budget.LlmUsage` from the structured inputs
the caller already has, delegates to
:func:`~app.domain.llm.budget.record_usage`, and returns a
:class:`RecordedCall` carrying the correlation id back to the caller
for the ``X-Correlation-Id-Echo`` header round-trip (§11 "Client
abstraction").

What this module is NOT:

* NOT a client wrapper — it doesn't own the HTTP round-trip, retry
  loop, or fallback-chain walk. Those live in the LLM adapter
  (:mod:`app.adapters.llm.openrouter` and siblings) and the
  eventual client abstraction (§11 "Client abstraction").
* NOT a budget pre-flight — callers call
  :func:`~app.domain.llm.budget.check_budget` directly before
  dispatch and log ``llm.budget_exceeded`` themselves on refusal.
  A refused call MUST NOT reach :func:`record`; the defensive
  ``status="refused"`` branch in
  :func:`~app.domain.llm.budget.record_usage` catches the bypass
  but the recorder treats it as an invariant violation worth
  flagging (see ``test_refused_status_is_defensive`` in the test
  module).
* NOT an audit writer — the caller that triggered the LLM call
  (agent action middleware, digest worker) writes the single
  :class:`~app.adapters.db.audit.models.AuditLog` row per §11
  "Agent audit trail". Writing one here would double-count.

Terminal-error semantic:

* ``status="ok"`` — successful dispatch. The row writes with cost,
  tokens, latency, ``finish_reason``; the ledger bumps by
  ``cost_cents``.
* ``status="error"`` / ``status="timeout"`` — terminal failure
  after the chain exhausted. The row still writes (for the
  /admin/usage feed's "calls that were attempted"). Callers MUST
  pass ``cost_cents=0`` — the invariant is enforced by
  :class:`~app.domain.llm.budget.LlmUsage`'s ``__post_init__``
  (raises :class:`ValueError` on construction if a non-zero cost
  is paired with a non-``"ok"`` status). §11 "Cost tracking" never
  bills a provider failure against the meter; the ledger would
  otherwise inflate silently because
  :func:`~app.domain.llm.budget.record_usage` bumps unconditionally
  on non-refused statuses.
* ``status="refused"`` — MUST NOT reach this function. Callers
  catch :class:`BudgetExceeded` out of
  :func:`~app.domain.llm.budget.check_budget` and log
  ``llm.budget_exceeded`` themselves (§11 "At-cap behaviour"). The
  defensive branch in ``record_usage`` short-circuits so a bypass
  doesn't destabilise the session. A refusal row built with a
  non-zero cost would ALSO trip the ``LlmUsage`` invariant — the
  refusal bypass is a no-op either way.

See ``docs/specs/11-llm-and-agents.md`` §"Client abstraction",
§"Cost tracking", §"Cost tracking — extended", §"Agent audit trail",
§"Failure modes"; ``docs/specs/02-domain-model.md`` §"LLM"
§"llm_usage" / §"llm_call".
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.domain.llm.budget import LlmUsage, UsageStatus, record_usage
from app.domain.llm.router import ModelPick
from app.observability.metrics import (
    LLM_CALLS_TOTAL,
    LLM_COST_USD_TOTAL,
    cents_to_usd,
    sanitize_label,
    sanitize_workspace_label,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock

__all__ = [
    "AgentAttribution",
    "RecordedCall",
    "record",
]


@dataclass(frozen=True, slots=True)
class AgentAttribution:
    """§11 "Agent audit trail" fields the recorder persists verbatim.

    Populated by the API layer from the authenticated request (token
    kind, delegating user, ``X-Agent-Conversation-Ref``) and passed
    opaquely through the recorder. Every field is optional — a
    passkey-session call fills only ``actor_user_id``, a
    service-initiated call (digest worker, health check) fills
    nothing, a delegated-token call fills all four.

    Kept as a frozen dataclass (not a TypedDict) so the type checker
    catches a caller that omits a field rather than silently landing
    ``None`` for a required actor. Slotted so the value can be
    stashed on a request-local record without the dict overhead.

    ``agent_conversation_ref`` is accepted for surface parity with
    §11 "Agent audit trail" but is NOT written to the ``llm_usage``
    row — it lands on the paired ``audit_log`` row the action caller
    writes. The field is carried here so a future extension that
    denormalises the ref onto ``llm_usage`` (for
    correlation-agnostic /admin/usage lookups) has a seat in the
    attribution surface already.

    Empty-string / ``None`` convention: every string field is
    normalised to ``None`` at construction if the caller passes an
    empty string. The DB contract (§02 ``audit_log`` /
    ``llm_usage``) uses ``NULL`` as the "absent" sentinel; an empty
    string that sneaked through would show up as ``""`` on a
    /admin/usage readout and an operator would see a "call with
    empty label" that is just a caller-side typo. Mirrors the
    resolver-bypass coercion on :class:`~app.domain.llm.budget.
    LlmUsage.assignment_id` — symmetry across the audit-trail
    surface.
    """

    actor_user_id: str | None
    token_id: str | None
    agent_label: str | None
    agent_conversation_ref: str | None = None

    def __post_init__(self) -> None:
        """Coerce empty strings to ``None`` on every attribution field.

        The API layer reads these from headers / token metadata; an
        empty / missing header lands as ``""`` in the typical
        FastAPI pattern. Coercing here (rather than at each caller)
        centralises the rule and keeps the ``NULL`` semantics
        consistent across /admin/usage filters — same pattern as
        :func:`~app.domain.llm.budget.record_usage`'s
        ``assignment_id`` coercion.
        """
        # ``object.__setattr__`` — the dataclass is frozen, so
        # assignment would raise. The frozen guarantee still holds
        # for external callers; internal normalisation during
        # construction is the documented escape hatch for
        # ``frozen=True`` dataclasses.
        for field_name in (
            "actor_user_id",
            "token_id",
            "agent_label",
            "agent_conversation_ref",
        ):
            value = getattr(self, field_name)
            if value == "":
                object.__setattr__(self, field_name, None)


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """Return value from :func:`record` — the persisted usage row.

    Callers echo the correlation id back to their own caller via the
    ``X-Correlation-Id-Echo`` header (§11 "Client abstraction") by
    reading ``usage.correlation_id``. The usage record is surfaced
    for the same reason as a flushed-ORM return: downstream code
    that wants to snapshot the persisted shape for logging doesn't
    need a re-read.

    A prior revision carried a redundant top-level ``correlation_id``
    field; it was removed under cd-z8h1 since ``usage.correlation_id``
    is the single source of truth and duplicating it on the return
    shape creates a drift hazard (two writers, one reader).
    """

    usage: LlmUsage


def record(
    session: Session,
    ctx: WorkspaceContext,
    *,
    capability: str,
    model_pick: ModelPick,
    fallback_attempts: int,
    correlation_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_cents: int,
    latency_ms: int,
    status: UsageStatus,
    finish_reason: str | None,
    attribution: AgentAttribution,
    attempt: int = 0,
    clock: Clock | None = None,
) -> RecordedCall:
    """Post-flight seam: build a domain usage row and persist it.

    Takes the structured inputs the callers already have (the
    resolved :class:`~app.domain.llm.router.ModelPick`, the
    provider's reported token counts + latency, the §11 agent-audit
    attribution) and builds a domain
    :class:`~app.domain.llm.budget.LlmUsage`. Delegates the actual
    INSERT + ledger bump to
    :func:`~app.domain.llm.budget.record_usage` so the idempotency +
    refusal + row-locked ledger-bump semantics stay in one place.

    Caller contract:

    * Call :func:`~app.domain.llm.budget.check_budget` **before** the
      provider dispatch. If it raises :class:`BudgetExceeded`, do
      NOT call :func:`record` — the pre-flight path logs
      ``llm.budget_exceeded`` itself and no usage row is written
      (§11 "At-cap behaviour").
    * On success, pass ``status="ok"`` and the provider's
      ``finish_reason``.
    * On terminal failure after the whole chain exhausted, pass
      ``status="error"`` (or ``"timeout"``) with ``cost_cents=0``.
      The /admin/usage feed still sees the attempt without bumping
      the meter. :func:`~app.domain.llm.budget.record_usage` bumps
      the ledger unconditionally on non-refused statuses, so
      :class:`~app.domain.llm.budget.LlmUsage` now **enforces**
      ``cost_cents == 0`` when ``status != "ok"`` at construction
      (raises :class:`ValueError`). The §11 "Cost tracking"
      convention is no longer a docstring hope.

    Parameters:

    * ``capability`` — the §11 capability key the caller resolved
      through.
    * ``model_pick`` — head-of-chain rung that actually dispatched.
      For a fallback-success case this is the rung that eventually
      worked; ``fallback_attempts`` carries how many rungs ahead of
      it failed.
    * ``fallback_attempts`` — matches the §11 "LLMResult"
      ``fallback_attempts`` contract. 0 = first-rung success.
    * ``correlation_id`` — tie-id shared across the retry chain and
      echoed back to the caller.
    * ``prompt_tokens`` / ``completion_tokens`` / ``cost_cents`` /
      ``latency_ms`` — provider-reported metrics. ``cost_cents`` is
      already cent-rounded; callers use
      :func:`~app.domain.llm.budget.estimate_cost_cents` on the
      actual (not estimated) token counts.
    * ``status`` — ``"ok" | "error" | "timeout"``. ``"refused"`` is
      rejected via the defensive branch in ``record_usage``; the
      /admin/usage feed never carries refusal rows.
    * ``finish_reason`` — the provider's verbatim string. NULL on
      timeout / transport error.
    * ``attribution`` — §11 "Agent audit trail" fields.
    * ``attempt`` — retry index within one ``(workspace_id,
      correlation_id)`` operation. Defaults to 0; the caller bumps
      this on each rung so the idempotency unique catches a replay.
    * ``clock`` — test seam for deterministic ``created_at``.

    Returns a :class:`RecordedCall` carrying the usage + correlation
    id. No I/O beyond the delegated ``record_usage`` write.
    """
    # Domain ``LlmUsage`` is frozen — build it in one shot; no
    # conditional mutation. The router's ``ModelPick`` already
    # exposes the provider-model ULID + api wire name; we pass
    # them straight through so the budget adapter can pick them up
    # via the existing ``api_model_id`` / ``provider_model_id``
    # contract.
    usage = LlmUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_cents=cost_cents,
        provider_model_id=model_pick.provider_model_id,
        api_model_id=model_pick.api_model_id,
        assignment_id=model_pick.assignment_id,
        capability=capability,
        correlation_id=correlation_id,
        attempt=attempt,
        status=status,
        latency_ms=latency_ms,
        fallback_attempts=fallback_attempts,
        finish_reason=finish_reason,
        actor_user_id=attribution.actor_user_id,
        token_id=attribution.token_id,
        agent_label=attribution.agent_label,
    )
    record_usage(session, ctx, usage, clock=clock)
    _record_metrics(ctx, capability=capability, usage=usage)
    return RecordedCall(usage=usage)


def _record_metrics(
    ctx: WorkspaceContext,
    *,
    capability: str,
    usage: LlmUsage,
) -> None:
    """Bump the §16 LLM metrics from a recorded usage row.

    Runs after :func:`record_usage` so a constraint violation /
    budget invariant trip never leaks a phantom counter increment.
    Workspace labels run through
    :func:`~app.observability.metrics.sanitize_workspace_label` to
    enforce the §15 "no PII in metric labels" invariant; capability
    + status / model are non-PII by construction.

    Cost only flows into ``crewday_llm_cost_usd_total`` for
    successful (``status="ok"``) calls because the §11 spec bills
    only those — a terminal error / timeout carries
    ``cost_cents=0`` (the :class:`LlmUsage` invariant). Skipping
    the bump on non-OK statuses also keeps the counter reset-free:
    a counter that observed ``0.0`` increments would Prometheus-
    correctly stay flat, but the noise on a `rate()` query against
    a sparse series is worse than not emitting at all.
    """
    workspace = sanitize_workspace_label(ctx.workspace_id)
    LLM_CALLS_TOTAL.labels(
        workspace_id=workspace,
        capability=sanitize_label(capability),
        status=sanitize_label(usage.status),
    ).inc()
    if usage.status == "ok" and usage.cost_cents > 0:
        LLM_COST_USD_TOTAL.labels(
            workspace_id=workspace,
            model=sanitize_label(usage.api_model_id),
        ).inc(cents_to_usd(usage.cost_cents))
