"""HITL approval consumer pipeline (cd-9ghv).

The agent runtime in :mod:`app.domain.agent.runtime` mints an
``approval_request`` row with ``status='pending'`` whenever a tool
call hits a gate (workspace-policy always-gated, workspace-policy
configurable, per-user ``strict`` mode, or annotation-driven). This
module is the *consumer* that walks those rows through the §11
state machine:

* :func:`approve` — flip ``pending → approved``, replay the recorded
  tool call through the dispatcher, persist the result, audit, emit
  :class:`~app.events.types.ApprovalDecided`.
* :func:`deny` — flip ``pending → rejected``, audit, emit
  :class:`~app.events.types.ApprovalDecided`. No tool call leaves the
  client.
* :func:`expire_due` — worker-driven sweep: flip ``pending →
  timed_out`` for every row whose ``expires_at`` has passed, audit
  one summary row, emit one :class:`~app.events.types.ApprovalDecided`
  per row.
* :func:`get` / :func:`list_pending` — read-side projections the
  /approvals desk and the inline-chat card consume through
  ``GET /approvals`` and ``GET /approvals/{id}``.

Every state transition is **idempotent on the row level**: a second
:func:`approve` on an already-approved row returns the cached result
instead of re-dispatching, so a retried HTTP call never doubles the
side effect (§11 "Approval pipeline" pins this on the recorded
``idempotency_key`` carried in ``action_json``). Replay collapses to
the pre-recorded row read; the service does not invent new tool
calls.

Auth gating happens at the API seam, not here — the service trusts
its caller has already gated the request to a session or a PAT
carrying ``approvals:act`` (§11 "Approval decisions travel through
the human session, not the agent token"). The seam is responsible
for surfacing :class:`CredentialRejected` envelopes; this module
does not branch on transport.

See ``docs/specs/11-llm-and-agents.md`` §"Approval pipeline",
§"Approval decisions travel through the human session, not the
agent token", §"Inline approval UX", §"TTL".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import (
    _APPROVAL_REQUEST_STATUS_VALUES,
    ApprovalRequest,
)
from app.audit import write_audit
from app.domain.agent.runtime import (
    DelegatedToken,
    ToolCall,
    ToolDispatcher,
    ToolResult,
)
from app.domain.errors import Conflict, NotFound, Validation
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import ApprovalDecided
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock

__all__ = [
    "APPROVAL_STATUS_VALUES",
    "DEFAULT_PAGE_LIMIT",
    "EXPIRED_DECISION_NOTE",
    "MAX_PAGE_LIMIT",
    "ApprovalNotFound",
    "ApprovalNotPending",
    "ApprovalReplayDispatcher",
    "ApprovalView",
    "ApprovalsPage",
    "ExpireDueReport",
    "approve",
    "deny",
    "expire_due",
    "get",
    "list_pending",
]


# Re-exported to keep the model-side enum body and the service-layer
# vocabulary in lock-step. The domain layer reads ``status`` as the
# closed string set; the row's CHECK constraint enforces the same
# vocabulary at write time.
APPROVAL_STATUS_VALUES: Final[tuple[str, ...]] = _APPROVAL_REQUEST_STATUS_VALUES


# §11 "TTL": default page size + cap for ``GET /approvals``. The
# desk surface walks the cursor; the inline-card surface fetches a
# single row by id and never exercises the list path. v1 ships a
# generous cap so an operator-curated dashboard with weeks of
# pending rows still renders in a single sweep.
DEFAULT_PAGE_LIMIT: Final[int] = 25
MAX_PAGE_LIMIT: Final[int] = 100


# ``decision_note_md`` length cap. Pinned to 4 KiB so a malicious /
# careless reviewer cannot push a megabyte of free-text into the
# audit trail. The §11 spec does not pin a number; matches the
# ``rationale_md`` cap from cd-cm5-era notes.
_DECISION_NOTE_MAX_LEN: Final[int] = 4 * 1024


# Sentinel value the TTL worker stamps on auto-expired rows
# (§11 "TTL"). Lifted to a module constant so the worker, the
# unit test, and the audit reader all agree on the literal.
EXPIRED_DECISION_NOTE: Final[str] = "auto-expired"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ApprovalNotFound(NotFound):
    """The requested approval row does not exist (or is cross-tenant).

    Per §01 "Workspace addressing" the seam returns 404 (not 403)
    when the row exists in a different workspace — the tenant
    surface is not enumerable. This subclass narrows the
    :class:`NotFound` envelope so the seam can map to a stable
    error code without inspecting the message.
    """

    title = "Approval not found"
    type_name = "approval_not_found"


class ApprovalNotPending(Conflict):
    """The approval row is no longer in ``pending`` state. HTTP 409.

    The row already transitioned to ``approved`` / ``rejected`` /
    ``timed_out``; a fresh decision cannot land. The envelope's
    ``extra`` carries the row's current ``status`` so the SPA can
    refresh its inline card or /approvals queue without a second
    REST round-trip.
    """

    title = "Approval no longer pending"
    type_name = "approval_not_pending"

    def __init__(
        self,
        approval_request_id: str,
        *,
        status: str,
        detail: str | None = None,
    ) -> None:
        super().__init__(
            detail
            or (
                f"approval {approval_request_id!r} is in state {status!r}; "
                "a decision can only land on a pending row"
            ),
            extra={"approval_request_id": approval_request_id, "status": status},
        )
        self.approval_request_id = approval_request_id
        self.status = status


# ---------------------------------------------------------------------------
# Replay seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovalReplayDispatcher:
    """Wraps a :class:`ToolDispatcher` + the delegated token reused on replay.

    Approval replay re-dispatches the recorded tool call through the
    same dispatcher seam the agent runtime used at gate time. Since
    the original turn already minted (and recorded) a delegated
    token for the agent, we re-mint a fresh token at decision time
    rather than reuse a possibly-expired one — the dispatcher's
    contract requires both fields, and ``approve`` is the entry
    point that knows whose passkey session is paying for the
    decision (the API seam mints the token, hands the bundle to
    this service, and the service hands it to the dispatcher).

    This class is a small bundle of the two values the dispatcher
    needs; it does not own any state of its own.
    """

    dispatcher: ToolDispatcher
    token: DelegatedToken
    headers: Mapping[str, str]


# ---------------------------------------------------------------------------
# Views (read-side projections)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovalView:
    """Read-side projection of an :class:`ApprovalRequest` row.

    Mirrors the §11 ``agent_action`` shape the /approvals desk and
    the inline-chat card both consume. The cd-9ghv slice promotes
    six fields off ``action_json`` into first-class columns; the
    remaining §11 fields (``approval_id`` human-shown,
    ``correlation_id``, ``card_summary`` / ``card_risk`` /
    ``card_fields_json``, ``gate_source``, ``gate_destination``,
    ``executed_at``, ``requested_by_token_id``) are surfaced through
    :attr:`action_json` until a future cleanup promotes them too.
    """

    id: str
    workspace_id: str
    requester_actor_id: str | None
    for_user_id: str | None
    inline_channel: str | None
    resolved_user_mode: str | None
    status: Literal["pending", "approved", "rejected", "timed_out"]
    decided_by: str | None
    decided_at: datetime | None
    decision_note_md: str | None
    expires_at: datetime | None
    created_at: datetime
    action_json: Mapping[str, Any]
    result_json: Mapping[str, Any] | None

    @classmethod
    def from_row(cls, row: ApprovalRequest) -> ApprovalView:
        """Project a SA row into the immutable view shape."""
        # The CHECK constraint clamps ``status`` to the literal set;
        # narrow at the seam so callers pattern-match without a
        # cast. A row with an unknown status is a data bug — fail
        # loudly rather than degrading silently. The explicit
        # if/elif ladder narrows ``status`` to the closed Literal
        # type without a ``cast`` or ``# type: ignore`` (the
        # branches each assign a literal-typed local — mypy
        # narrows on the comparison).
        narrowed: Literal["pending", "approved", "rejected", "timed_out"]
        if row.status == "pending":
            narrowed = "pending"
        elif row.status == "approved":
            narrowed = "approved"
        elif row.status == "rejected":
            narrowed = "rejected"
        elif row.status == "timed_out":
            narrowed = "timed_out"
        else:
            raise ValueError(
                f"approval row {row.id!r} carries unknown status {row.status!r}; "
                f"expected one of {APPROVAL_STATUS_VALUES!r}"
            )
        return cls(
            id=row.id,
            workspace_id=row.workspace_id,
            requester_actor_id=row.requester_actor_id,
            for_user_id=row.for_user_id,
            inline_channel=row.inline_channel,
            resolved_user_mode=row.resolved_user_mode,
            status=narrowed,
            decided_by=row.decided_by,
            decided_at=row.decided_at,
            # ``decision_note_md`` is the spec name; fall back to the
            # legacy ``rationale_md`` so cd-cm5-era rows still render
            # on the desk surface (§02 "approval_request" comment).
            decision_note_md=row.decision_note_md or row.rationale_md,
            expires_at=row.expires_at,
            created_at=row.created_at,
            action_json=dict(row.action_json),
            result_json=dict(row.result_json) if row.result_json is not None else None,
        )


@dataclass(frozen=True, slots=True)
class ApprovalsPage:
    """Cursor-paginated projection for ``GET /approvals`` (§12 envelope)."""

    data: Sequence[ApprovalView]
    next_cursor: str | None
    has_more: bool


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get(
    ctx: WorkspaceContext,
    *,
    session: Session,
    approval_request_id: str,
) -> ApprovalView:
    """Return one approval row by id, scoped to ``ctx``'s workspace.

    Raises :class:`ApprovalNotFound` for a missing or cross-tenant
    row — the API seam maps that to 404 per §01 "Workspace
    addressing" (cross-tenant must not be enumerable, so 404 wins
    over 403 here).
    """
    row = session.get(ApprovalRequest, approval_request_id)
    if row is None or row.workspace_id != ctx.workspace_id:
        raise ApprovalNotFound(f"approval {approval_request_id!r} not found")
    return ApprovalView.from_row(row)


def list_pending(
    ctx: WorkspaceContext,
    *,
    session: Session,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
) -> ApprovalsPage:
    """Return the ``pending`` approval queue for ``ctx``'s workspace.

    The /approvals desk reads through this; the inline-chat surface
    does not (it consumes :class:`ApprovalView` rows directly via
    :func:`get`). Ordering is oldest-pending first so the queue
    drains in arrival order; the
    ``ix_approval_request_workspace_status_created`` composite
    index backs the scan.

    ``cursor`` is the **id** of the last row from the previous
    page. Pagination rides the composite ``(created_at, id)`` key so
    same-millisecond rows still order deterministically; the cursor
    row is looked up first to recover its ``created_at`` and the
    predicate is the SQL row-value comparison
    ``(created_at, id) > (cursor.created_at, cursor.id)``. A cursor
    that does not match any row in ``ctx``'s workspace produces an
    empty page — benign UX for a stale cursor.

    ``limit`` is clamped to ``MAX_PAGE_LIMIT``; a smaller value is
    honoured. ``limit <= 0`` is a 422 — pagination of "zero rows"
    is meaningless, and silently rounding to 1 would surprise the
    caller.
    """
    if limit <= 0:
        raise Validation(
            f"limit must be positive; got {limit}",
            extra={"limit": limit},
        )
    effective_limit = min(limit, MAX_PAGE_LIMIT)

    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.status == "pending")
        .order_by(ApprovalRequest.created_at, ApprovalRequest.id)
        # Fetch one extra so we can determine ``has_more`` without a
        # second COUNT — the desk surface only needs to know whether
        # to render the "next page" affordance.
        .limit(effective_limit + 1)
    )
    if cursor is not None:
        # Look up the cursor row to recover ``created_at``; the
        # tenant filter scopes the lookup to ``ctx``'s workspace, so
        # a cross-tenant cursor cannot leak rows. A missing cursor
        # row produces an empty page — benign UX for a stale cursor.
        cursor_row = session.get(ApprovalRequest, cursor)
        if cursor_row is None or cursor_row.workspace_id != ctx.workspace_id:
            return ApprovalsPage(data=(), next_cursor=None, has_more=False)
        # Composite-key strict-greater-than expressed as the
        # OR-of-AND form so both SQLite and PostgreSQL plan it on the
        # ``ix_approval_request_workspace_status_created`` composite
        # index. Equivalent to
        # ``(created_at, id) > (cursor_row.created_at, cursor_row.id)``
        # but without resorting to a row-value tuple comparison
        # (SA stub-typed bind parameters are awkward; this form
        # types cleanly under ``mypy --strict``).
        stmt = stmt.where(
            or_(
                ApprovalRequest.created_at > cursor_row.created_at,
                and_(
                    ApprovalRequest.created_at == cursor_row.created_at,
                    ApprovalRequest.id > cursor_row.id,
                ),
            )
        )

    rows = list(session.scalars(stmt).all())
    has_more = len(rows) > effective_limit
    if has_more:
        rows = rows[:effective_limit]

    next_cursor: str | None = rows[-1].id if has_more and rows else None

    return ApprovalsPage(
        data=tuple(ApprovalView.from_row(r) for r in rows),
        next_cursor=next_cursor,
        has_more=has_more,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def approve(
    ctx: WorkspaceContext,
    *,
    session: Session,
    approval_request_id: str,
    replay: ApprovalReplayDispatcher,
    decision_note_md: str | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> ApprovalView:
    """Flip ``pending → approved`` and replay the recorded tool call.

    The §11 "Approval pipeline" sequence:

    1. Load the row and verify it is ``pending`` + in ``ctx``'s
       workspace (404 cross-tenant, 409 already-decided).
    2. Re-dispatch the recorded tool call through the
       :class:`ApprovalReplayDispatcher`. The dispatcher's contract
       guarantees the call carries the original idempotency key
       (recorded in ``action_json.tool_call_id``) so a retried
       HTTP approval that re-enters this function does not double
       the side effect.
    3. Persist the dispatched result on the row
       (``result_json`` + ``status='approved'`` + ``decided_by`` +
       ``decided_at`` + ``decision_note_md``).
    4. Write one ``audit.approval.granted`` row attributed to
       ``ctx.actor_id`` (§11 "Audit-log fields").
    5. Publish :class:`ApprovalDecided` so subscribers refresh
       /approvals + drop the inline card.

    A second :func:`approve` call on the already-approved row
    raises :class:`ApprovalNotPending` (the seam maps to 409 with
    the row's current state). Approve never executes a tool call
    twice.

    Replay errors propagate. The runtime's dispatcher returns a
    :class:`~app.domain.agent.runtime.ToolResult` for any call
    that landed on the API seam — even a 404 / 422 is reported via
    the ``status_code`` field rather than raised. A genuine
    transport failure (the dispatcher itself raised) bubbles to
    the caller; the row stays ``pending`` (no partial transition).
    """
    eff_clock: Clock = clock if clock is not None else SystemClock()
    bus = event_bus if event_bus is not None else default_event_bus

    row = _load_pending(session, ctx=ctx, approval_request_id=approval_request_id)

    note = _validate_decision_note(decision_note_md)
    tool_call = _tool_call_from_action(row.action_json)

    # 2. Replay. The dispatcher returns a :class:`ToolResult` even
    #    for a 4xx — the runtime's contract is "transport failures
    #    raise, surface failures land on the result". We persist
    #    every outcome and let the operator inspect ``result_json``
    #    on the desk surface.
    result = replay.dispatcher.dispatch(
        tool_call,
        token=replay.token,
        headers=replay.headers,
    )

    decided_at = eff_clock.now()
    row.status = "approved"
    row.decided_by = ctx.actor_id
    row.decided_at = decided_at
    row.decision_note_md = note
    row.result_json = _result_to_json(result)

    write_audit(
        session,
        ctx,
        entity_kind="approval_request",
        entity_id=row.id,
        action="approval.granted",
        diff={
            "approval_request_id": row.id,
            "decision": "approved",
            "decided_by": ctx.actor_id,
            "for_user_id": row.for_user_id,
            "tool_name": tool_call.name,
            "tool_call_id": tool_call.id,
            "result_status_code": result.status_code,
            "result_mutated": result.mutated,
            "decision_note_md": note,
        },
        clock=eff_clock,
    )

    bus.publish(
        ApprovalDecided(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=decided_at,
            approval_request_id=row.id,
            decision="approved",
            for_user_id=row.for_user_id,
        )
    )

    return ApprovalView.from_row(row)


def deny(
    ctx: WorkspaceContext,
    *,
    session: Session,
    approval_request_id: str,
    decision_note_md: str | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> ApprovalView:
    """Flip ``pending → rejected``; the tool call is never dispatched.

    Mirrors :func:`approve` minus the replay step. Writes one
    ``audit.approval.denied`` row attributed to ``ctx.actor_id``.

    A second :func:`deny` (or :func:`approve`) on the already-
    rejected row raises :class:`ApprovalNotPending`.
    """
    eff_clock: Clock = clock if clock is not None else SystemClock()
    bus = event_bus if event_bus is not None else default_event_bus

    row = _load_pending(session, ctx=ctx, approval_request_id=approval_request_id)

    note = _validate_decision_note(decision_note_md)
    tool_call = _tool_call_from_action(row.action_json)

    decided_at = eff_clock.now()
    row.status = "rejected"
    row.decided_by = ctx.actor_id
    row.decided_at = decided_at
    row.decision_note_md = note

    write_audit(
        session,
        ctx,
        entity_kind="approval_request",
        entity_id=row.id,
        action="approval.denied",
        diff={
            "approval_request_id": row.id,
            "decision": "rejected",
            "decided_by": ctx.actor_id,
            "for_user_id": row.for_user_id,
            "tool_name": tool_call.name,
            "tool_call_id": tool_call.id,
            "decision_note_md": note,
        },
        clock=eff_clock,
    )

    bus.publish(
        ApprovalDecided(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=decided_at,
            approval_request_id=row.id,
            decision="rejected",
            for_user_id=row.for_user_id,
        )
    )

    return ApprovalView.from_row(row)


@dataclass(frozen=True, slots=True)
class ExpireDueReport:
    """Summary of one :func:`expire_due` sweep.

    The TTL worker logs this on every tick so /admin/usage's
    "Agent Activity" view (cd-wjpl) can render a sparkline of
    auto-expired approvals over time. Carrying the count + the
    list of ids (rather than only the count) lets the worker
    re-emit a webhook for each row even if the worker is later
    extended to fan out to a second sink — the row id is enough
    to re-derive everything else.
    """

    expired_count: int
    expired_ids: tuple[str, ...]


def expire_due(
    *,
    session: Session,
    now: datetime,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> ExpireDueReport:
    """Flip every ``pending`` row past its ``expires_at`` to ``timed_out``.

    Worker-driven sweep. Cross-tenant by design: the TTL worker
    runs per-deployment, scans every workspace's pending queue,
    and re-emits one :class:`ApprovalDecided` event per expired
    row (the SSE transport's per-workspace filter routes it to
    the right subscribers).

    The sweep:

    1. Selects every ``pending`` row whose ``expires_at`` is non-
       null and not greater than ``now`` (rows with ``NULL``
       ``expires_at`` are pre-cd-9ghv legacy and never expire —
       see the migration's rationale). The select runs under
       :func:`tenant_agnostic` because the worker has no
       :class:`WorkspaceContext`; the ORM tenant filter would
       drop every row otherwise.
    2. For each row: stamp ``status='timed_out'``,
       ``decided_at=now``, ``decision_note_md='auto-expired'``.
       ``decided_by`` stays ``NULL`` — the auto-expiry has no
       human reviewer.
    3. Publishes one :class:`ApprovalDecided` event per row.

    No audit row is written here (deliberate). The worker's
    structured log (``event=approval.ttl.sweep``, see
    :mod:`app.worker.tasks.approval_ttl`) carries the per-tick
    summary; the per-row state flip + event payload carry the
    detail. Adding an audit row per expired row would dominate
    the audit table on a busy fleet for no reader gain (the
    decision is automatic, attributable to the system actor —
    nothing a human did).

    Returns the count + the ids of expired rows so the worker can
    log + re-emit. An empty sweep is a no-op (no audit row, no
    events). The audit + event side-effects share the caller's
    open transaction; the worker commits once at the tick
    boundary.

    The ``clock`` parameter is kept on the signature for forward
    compatibility — a future extension that writes one summary
    audit row per tick (rather than per row) needs the clock for
    its ``created_at`` stamp — but is unused on the v1 path
    because ``now`` is passed in by the worker (which already
    derived it from its own clock).
    """
    del clock  # forward-compat parameter, see docstring
    bus = event_bus if event_bus is not None else default_event_bus

    with tenant_agnostic():
        stmt = (
            select(ApprovalRequest)
            .where(
                ApprovalRequest.status == "pending",
                ApprovalRequest.expires_at.is_not(None),
                ApprovalRequest.expires_at <= now,
            )
            .order_by(ApprovalRequest.created_at, ApprovalRequest.id)
        )
        rows = list(session.scalars(stmt).all())

    expired_ids: list[str] = []
    for row in rows:
        # Defensive guard: a concurrent ``approve`` /
        # ``deny`` may have flipped the row between the SELECT and
        # the UPDATE. Skip rows whose status changed under us so
        # the worker never overwrites a fresh decision. The next
        # tick re-checks (the row is already terminal so it falls
        # out of the predicate).
        if row.status != "pending":
            continue
        row.status = "timed_out"
        row.decided_at = now
        row.decision_note_md = EXPIRED_DECISION_NOTE
        # ``decided_by`` stays NULL — the auto-expiry has no human
        # reviewer; an audit reader keys off ``decision_note_md``
        # to tell the auto path apart from a NULL-decider data
        # corruption.
        expired_ids.append(row.id)

        bus.publish(
            ApprovalDecided(
                workspace_id=row.workspace_id,
                # The expiry has no actor — we attribute to the
                # delegating user for SSE filter parity (the
                # inline-chat tab refreshes off this event), but
                # the audit row carries the system actor through
                # the worker's deployment-scope writer.
                actor_id=row.for_user_id or row.requester_actor_id or "system",
                correlation_id=row.id,
                occurred_at=now,
                approval_request_id=row.id,
                decision="expired",
                for_user_id=row.for_user_id,
            )
        )

    return ExpireDueReport(
        expired_count=len(expired_ids),
        expired_ids=tuple(expired_ids),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_pending(
    session: Session,
    *,
    ctx: WorkspaceContext,
    approval_request_id: str,
) -> ApprovalRequest:
    """Load one approval row, asserting tenancy + ``pending`` state.

    Returns the SA row so callers can mutate it in place; the
    session's open transaction owns the flush. Raises
    :class:`ApprovalNotFound` for a missing or cross-tenant row,
    :class:`ApprovalNotPending` for a row already past ``pending``.
    """
    row = session.get(ApprovalRequest, approval_request_id)
    if row is None or row.workspace_id != ctx.workspace_id:
        raise ApprovalNotFound(f"approval {approval_request_id!r} not found")
    if row.status != "pending":
        raise ApprovalNotPending(
            approval_request_id=row.id,
            status=row.status,
        )
    return row


def _validate_decision_note(decision_note_md: str | None) -> str | None:
    """Normalise + cap the reviewer's free-form note.

    Empty / whitespace-only collapses to ``None`` so a "no comment"
    decision does not write a misleading empty-string row. A note
    longer than :data:`_DECISION_NOTE_MAX_LEN` raises
    :class:`Validation` rather than truncating silently — a 4 KiB
    cap is comfortably above any legitimate reviewer note.
    """
    if decision_note_md is None:
        return None
    stripped = decision_note_md.strip()
    if not stripped:
        return None
    if len(stripped) > _DECISION_NOTE_MAX_LEN:
        raise Validation(
            f"decision_note_md exceeds {_DECISION_NOTE_MAX_LEN}-char cap "
            f"(got {len(stripped)})",
            extra={"max_length": _DECISION_NOTE_MAX_LEN},
        )
    return stripped


def _tool_call_from_action(action_json: Mapping[str, Any]) -> ToolCall:
    """Reconstruct a :class:`ToolCall` from the row's ``action_json``.

    The runtime persists the recorded ``tool_name`` / ``tool_call_id``
    / ``tool_input`` keys at gate time
    (:func:`app.domain.agent.runtime._write_approval_request`); the
    consumer reads them back to drive the dispatcher. A row missing
    any of the three is a data bug — fail loudly so the operator
    sees the corruption rather than silently dispatching an empty
    call.
    """
    try:
        name = action_json["tool_name"]
        call_id = action_json["tool_call_id"]
        tool_input = action_json["tool_input"]
    except KeyError as exc:
        raise ValueError(
            "approval row's action_json is missing required key "
            f"{exc.args[0]!r}; cannot replay tool call"
        ) from exc
    if not isinstance(name, str) or not isinstance(call_id, str):
        raise ValueError(
            "approval row's tool_name / tool_call_id must be strings; "
            f"got {type(name).__name__} / {type(call_id).__name__}"
        )
    if not isinstance(tool_input, Mapping):
        raise ValueError(
            "approval row's tool_input must be a JSON object; "
            f"got {type(tool_input).__name__}"
        )
    return ToolCall(id=call_id, name=name, input=dict(tool_input))


def _result_to_json(result: ToolResult) -> dict[str, Any]:
    """Project a :class:`ToolResult` into the JSON blob persisted on the row.

    The /approvals desk renders ``status_code`` + ``body`` so the
    operator can confirm the action landed; ``mutated`` carries
    forward for telemetry. The blob is deliberately schema-light —
    the spec's full ``executed_at`` / ``result_json`` shape lands
    when the cleanup migration promotes the rest of the §11
    columns. A nested ``body`` whose JSON encoding is exotic
    (datetime / Decimal / UUID) is still safe — the dispatcher's
    contract requires JSON-serialisable bodies.
    """
    return {
        "status_code": result.status_code,
        "mutated": result.mutated,
        "body": result.body,
    }
