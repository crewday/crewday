"""Unit tests for :mod:`app.domain.agent.approval` (cd-9ghv).

Covers each acceptance criterion on the Beads ticket:

* :func:`approve` happy path replays the recorded tool call, persists
  the result, audits ``approval.granted``, emits :class:`ApprovalDecided`.
* :func:`approve` is idempotent on the row level — a second call on
  an already-approved row raises :class:`ApprovalNotPending` (mapped
  to 409 by the seam).
* :func:`approve` cross-tenant raises :class:`ApprovalNotFound` — the
  surface is not enumerable.
* :func:`deny` flips ``pending → rejected``, never dispatches, audits
  ``approval.denied``, emits :class:`ApprovalDecided`.
* :func:`expire_due` flips every pending row past its ``expires_at``
  to ``timed_out`` with the ``auto-expired`` decision note, emits one
  :class:`ApprovalDecided` per row, and skips rows whose status
  changed under it.
* :func:`get` cross-tenant raises :class:`ApprovalNotFound`.
* :func:`list_pending` cursor-paginates oldest-first with the
  ``has_more`` boundary detected via ``LIMIT N+1``.
* :func:`list_pending` rejects ``limit <= 0`` with
  :class:`Validation`.
* ``decision_note_md`` validation: empty/whitespace collapses to
  None; over-cap raises :class:`Validation`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.llm.models import ApprovalRequest
from app.domain.agent.approval import (
    EXPIRED_DECISION_NOTE,
    MAX_PAGE_LIMIT,
    ApprovalNotFound,
    ApprovalNotPending,
    ApprovalReplayDispatcher,
    ApprovalView,
    approve,
    deny,
    expire_due,
    get,
    list_pending,
)
from app.domain.agent.runtime import (
    APPROVAL_REQUEST_TTL,
    DelegatedToken,
    ToolCall,
    ToolResult,
)
from app.domain.errors import Validation
from app.events.bus import EventBus
from app.events.types import ApprovalDecided
from app.tenancy import tenant_agnostic
from app.tenancy.current import set_current
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.domain.agent.conftest import (
    build_context,
    seed_user,
    seed_workspace,
)

# ---------------------------------------------------------------------------
# Local fixtures + helpers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CapturedReplay:
    """One :meth:`_RecordingDispatcher.dispatch` invocation."""

    call: ToolCall
    headers: Mapping[str, str]
    token: DelegatedToken


@dataclass(slots=True)
class _RecordingDispatcher:
    """Test double for :class:`ToolDispatcher`'s ``dispatch`` method.

    Approval replay only exercises ``dispatch`` (the gate decision
    was made and recorded at request time). The double pops the next
    canned :class:`ToolResult` per call; the queue is keyed by tool
    name so a test can pre-load distinct outcomes for distinct
    fixtures.
    """

    responses: dict[str, list[ToolResult]] = field(default_factory=dict)
    captured: list[_CapturedReplay] = field(default_factory=list)
    raise_on_dispatch: BaseException | None = None

    def is_gated(self, call: ToolCall):  # type: ignore[no-untyped-def]
        # The approval consumer never calls ``is_gated`` — the gate
        # decision is replayed off the recorded row. Implementing it
        # to fail loudly catches a future regression that adds an
        # unintended call site.
        raise AssertionError("is_gated must not be called during replay")

    def dispatch(
        self,
        call: ToolCall,
        *,
        token: DelegatedToken,
        headers: Mapping[str, str],
    ) -> ToolResult:
        if self.raise_on_dispatch is not None:
            raise self.raise_on_dispatch
        self.captured.append(
            _CapturedReplay(call=call, headers=dict(headers), token=token)
        )
        bucket = self.responses.get(call.name)
        if not bucket:
            return ToolResult(
                call_id=call.id,
                status_code=200,
                body={"echo": dict(call.input)},
                mutated=True,
            )
        return bucket.pop(0)


@dataclass(slots=True)
class _CapturedDecided:
    events: list[ApprovalDecided] = field(default_factory=list)


@pytest.fixture
def captured_decided(bus: EventBus) -> _CapturedDecided:
    """Subscribe to :class:`ApprovalDecided` on the per-test bus."""
    capture = _CapturedDecided()

    @bus.subscribe(ApprovalDecided)
    def _on(event: ApprovalDecided) -> None:
        capture.events.append(event)

    return capture


def _seed_pending(
    session: Session,
    *,
    workspace_id: str,
    requester_actor_id: str | None,
    for_user_id: str | None,
    clock: FrozenClock,
    tool_name: str = "tasks.complete",
    tool_input: Mapping[str, object] | None = None,
    inline_channel: str = "web_owner_sidebar",
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
) -> ApprovalRequest:
    """Insert one pending :class:`ApprovalRequest` for tests.

    Mirrors the runtime's ``_write_approval_request`` shape so the
    consumer's reads land on the same key set.
    """
    row_id = new_ulid(clock=clock)
    now = created_at or clock.now()
    row = ApprovalRequest(
        id=row_id,
        workspace_id=workspace_id,
        requester_actor_id=requester_actor_id,
        action_json={
            "tool_name": tool_name,
            "tool_call_id": f"tcall-{row_id[-8:].lower()}",
            "tool_input": dict(tool_input or {"task_id": "tsk_42"}),
            "card_summary": f"call {tool_name}",
            "card_risk": "low",
            "pre_approval_source": "manual",
            "agent_correlation_id": new_ulid(),
        },
        status="pending",
        decided_by=None,
        decided_at=None,
        rationale_md=None,
        decision_note_md=None,
        result_json=None,
        expires_at=expires_at if expires_at is not None else now + APPROVAL_REQUEST_TTL,
        inline_channel=inline_channel,
        for_user_id=for_user_id,
        resolved_user_mode=None,
        created_at=now,
    )
    with tenant_agnostic():
        session.add(row)
        session.flush()
    return row


def _make_replay(
    dispatcher: _RecordingDispatcher,
    *,
    token_id: str | None = None,
) -> ApprovalReplayDispatcher:
    return ApprovalReplayDispatcher(
        dispatcher=dispatcher,
        token=DelegatedToken(
            plaintext="mip_REPLAY_FAKEKEY",
            token_id=token_id or new_ulid(),
        ),
        headers={"X-Crewday-Replay": "1"},
    )


def _audit_actions(session: Session, *, workspace_id: str) -> list[str]:
    rows = session.scalars(
        select(AuditLog)
        .where(AuditLog.workspace_id == workspace_id)
        .order_by(AuditLog.created_at, AuditLog.id)
    ).all()
    return [row.action for row in rows]


# ---------------------------------------------------------------------------
# approve — happy path
# ---------------------------------------------------------------------------


def test_approve_happy_path_replays_persists_result_audits_emits(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
    captured_decided: _CapturedDecided,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=actor_id)
    set_current(ctx)
    delegating_user = seed_user(db_session)
    pending = _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=delegating_user,
        for_user_id=delegating_user,
        clock=clock,
    )
    dispatcher = _RecordingDispatcher(
        responses={
            "tasks.complete": [
                ToolResult(
                    call_id=pending.action_json["tool_call_id"],
                    status_code=201,
                    body={"task_id": "tsk_42", "completed": True},
                    mutated=True,
                ),
            ]
        },
    )
    replay = _make_replay(dispatcher)

    view = approve(
        ctx,
        session=db_session,
        approval_request_id=pending.id,
        replay=replay,
        decision_note_md="ship it",
        clock=clock,
        event_bus=bus,
    )
    db_session.flush()

    # 1. Dispatcher saw the recorded tool call exactly once.
    assert len(dispatcher.captured) == 1
    captured = dispatcher.captured[0]
    assert captured.call.id == pending.action_json["tool_call_id"]
    assert captured.call.name == "tasks.complete"
    assert dict(captured.call.input) == {"task_id": "tsk_42"}
    assert captured.headers == {"X-Crewday-Replay": "1"}

    # 2. Row was flipped + result persisted.
    refreshed = db_session.get(ApprovalRequest, pending.id)
    assert refreshed is not None
    assert refreshed.status == "approved"
    assert refreshed.decided_by == actor_id
    assert refreshed.decided_at == clock.now()
    assert refreshed.decision_note_md == "ship it"
    assert refreshed.result_json == {
        "status_code": 201,
        "mutated": True,
        "body": {"task_id": "tsk_42", "completed": True},
    }

    # 3. View matches the row.
    assert isinstance(view, ApprovalView)
    assert view.status == "approved"
    assert view.decided_by == actor_id
    assert view.decision_note_md == "ship it"
    assert view.result_json == refreshed.result_json

    # 4. Audit row written with the granted action.
    actions = _audit_actions(db_session, workspace_id=workspace.id)
    assert actions == ["approval.granted"]
    audit_row = db_session.scalars(
        select(AuditLog).where(AuditLog.entity_id == pending.id)
    ).one()
    assert audit_row.action == "approval.granted"
    assert audit_row.entity_kind == "approval_request"
    assert audit_row.actor_id == actor_id
    assert audit_row.diff["decision"] == "approved"
    assert audit_row.diff["tool_name"] == "tasks.complete"
    assert audit_row.diff["result_status_code"] == 201
    assert audit_row.diff["result_mutated"] is True
    assert audit_row.diff["decision_note_md"] == "ship it"

    # 5. Event published.
    assert len(captured_decided.events) == 1
    decided = captured_decided.events[0]
    assert decided.workspace_id == workspace.id
    assert decided.actor_id == actor_id
    assert decided.approval_request_id == pending.id
    assert decided.decision == "approved"
    assert decided.for_user_id == delegating_user


def test_approve_idempotent_via_409_on_second_attempt(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
    captured_decided: _CapturedDecided,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=actor_id)
    set_current(ctx)
    pending = _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=actor_id,
        for_user_id=actor_id,
        clock=clock,
    )
    dispatcher = _RecordingDispatcher()
    replay = _make_replay(dispatcher)

    approve(
        ctx,
        session=db_session,
        approval_request_id=pending.id,
        replay=replay,
        clock=clock,
        event_bus=bus,
    )
    db_session.flush()
    first_call_count = len(dispatcher.captured)
    assert first_call_count == 1

    # Second approve must not re-dispatch — the row is already
    # approved; a retried HTTP call is observed as 409.
    with pytest.raises(ApprovalNotPending) as info:
        approve(
            ctx,
            session=db_session,
            approval_request_id=pending.id,
            replay=replay,
            clock=clock,
            event_bus=bus,
        )

    assert info.value.status == "approved"
    assert info.value.approval_request_id == pending.id
    # The dispatcher still has only the single call from the first
    # approve — no double side-effect.
    assert len(dispatcher.captured) == first_call_count
    # Only one ApprovalDecided was published.
    assert [e.decision for e in captured_decided.events] == ["approved"]


def test_approve_cross_tenant_raises_not_found(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
) -> None:
    # Two workspaces; the row lives in the *other* one.
    other_ws = seed_workspace(db_session, slug="ws-other")
    other_user = seed_user(db_session)
    pending = _seed_pending(
        db_session,
        workspace_id=other_ws.id,
        requester_actor_id=other_user,
        for_user_id=other_user,
        clock=clock,
    )

    self_ws = seed_workspace(db_session, slug="ws-self")
    self_user = seed_user(db_session)
    ctx = build_context(self_ws.id, slug=self_ws.slug, actor_id=self_user)
    set_current(ctx)

    dispatcher = _RecordingDispatcher()
    replay = _make_replay(dispatcher)

    with pytest.raises(ApprovalNotFound):
        approve(
            ctx,
            session=db_session,
            approval_request_id=pending.id,
            replay=replay,
            clock=clock,
            event_bus=bus,
        )

    # Dispatcher must not have been called for a cross-tenant lookup.
    assert dispatcher.captured == []


def test_approve_unknown_id_raises_not_found(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=actor_id)
    set_current(ctx)
    replay = _make_replay(_RecordingDispatcher())

    with pytest.raises(ApprovalNotFound):
        approve(
            ctx,
            session=db_session,
            approval_request_id="01HZZZZZZZZZZZZZZZZZZZZZZZ",
            replay=replay,
            clock=clock,
            event_bus=bus,
        )


def test_approve_already_rejected_raises_not_pending(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=actor_id)
    set_current(ctx)
    pending = _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=actor_id,
        for_user_id=actor_id,
        clock=clock,
    )
    deny(
        ctx,
        session=db_session,
        approval_request_id=pending.id,
        decision_note_md="no",
        clock=clock,
        event_bus=bus,
    )
    db_session.flush()

    dispatcher = _RecordingDispatcher()
    with pytest.raises(ApprovalNotPending) as info:
        approve(
            ctx,
            session=db_session,
            approval_request_id=pending.id,
            replay=_make_replay(dispatcher),
            clock=clock,
            event_bus=bus,
        )
    assert info.value.status == "rejected"
    assert dispatcher.captured == []


# ---------------------------------------------------------------------------
# deny
# ---------------------------------------------------------------------------


def test_deny_flips_to_rejected_audits_emits_no_dispatch(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
    captured_decided: _CapturedDecided,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=actor_id)
    set_current(ctx)
    delegating_user = seed_user(db_session)
    pending = _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=delegating_user,
        for_user_id=delegating_user,
        clock=clock,
    )

    view = deny(
        ctx,
        session=db_session,
        approval_request_id=pending.id,
        decision_note_md="too risky",
        clock=clock,
        event_bus=bus,
    )
    db_session.flush()

    refreshed = db_session.get(ApprovalRequest, pending.id)
    assert refreshed is not None
    assert refreshed.status == "rejected"
    assert refreshed.decided_by == actor_id
    assert refreshed.decided_at == clock.now()
    assert refreshed.decision_note_md == "too risky"
    # No replay → no result captured.
    assert refreshed.result_json is None

    assert view.status == "rejected"
    assert view.decision_note_md == "too risky"
    assert view.result_json is None

    actions = _audit_actions(db_session, workspace_id=workspace.id)
    assert actions == ["approval.denied"]
    audit_row = db_session.scalars(
        select(AuditLog).where(AuditLog.entity_id == pending.id)
    ).one()
    assert audit_row.diff["decision"] == "rejected"
    assert audit_row.diff["tool_name"] == "tasks.complete"
    assert audit_row.diff["decision_note_md"] == "too risky"

    assert [e.decision for e in captured_decided.events] == ["rejected"]
    assert captured_decided.events[0].for_user_id == delegating_user


def test_deny_already_approved_raises_not_pending(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=actor_id)
    set_current(ctx)
    pending = _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=actor_id,
        for_user_id=actor_id,
        clock=clock,
    )
    approve(
        ctx,
        session=db_session,
        approval_request_id=pending.id,
        replay=_make_replay(_RecordingDispatcher()),
        clock=clock,
        event_bus=bus,
    )
    db_session.flush()

    with pytest.raises(ApprovalNotPending) as info:
        deny(
            ctx,
            session=db_session,
            approval_request_id=pending.id,
            clock=clock,
            event_bus=bus,
        )
    assert info.value.status == "approved"


# ---------------------------------------------------------------------------
# decision_note validation
# ---------------------------------------------------------------------------


def test_approve_collapses_whitespace_only_note_to_none(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=actor_id)
    set_current(ctx)
    pending = _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=actor_id,
        for_user_id=actor_id,
        clock=clock,
    )

    view = approve(
        ctx,
        session=db_session,
        approval_request_id=pending.id,
        replay=_make_replay(_RecordingDispatcher()),
        decision_note_md="   \n\t  ",
        clock=clock,
        event_bus=bus,
    )
    assert view.decision_note_md is None
    db_session.flush()
    refreshed = db_session.get(ApprovalRequest, pending.id)
    assert refreshed is not None
    assert refreshed.decision_note_md is None


def test_deny_rejects_oversized_note(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=actor_id)
    set_current(ctx)
    pending = _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=actor_id,
        for_user_id=actor_id,
        clock=clock,
    )

    huge = "x" * (4 * 1024 + 1)
    with pytest.raises(Validation) as info:
        deny(
            ctx,
            session=db_session,
            approval_request_id=pending.id,
            decision_note_md=huge,
            clock=clock,
            event_bus=bus,
        )
    assert "decision_note_md" in str(info.value)


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_returns_view_for_own_workspace(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=actor_id)
    set_current(ctx)
    pending = _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=actor_id,
        for_user_id=actor_id,
        clock=clock,
    )

    view = get(ctx, session=db_session, approval_request_id=pending.id)
    assert view.id == pending.id
    assert view.workspace_id == workspace.id
    assert view.status == "pending"
    assert view.for_user_id == actor_id
    assert view.action_json["tool_name"] == "tasks.complete"


def test_get_cross_tenant_raises_not_found(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    other_ws = seed_workspace(db_session, slug="ws-other2")
    other_user = seed_user(db_session)
    pending = _seed_pending(
        db_session,
        workspace_id=other_ws.id,
        requester_actor_id=other_user,
        for_user_id=other_user,
        clock=clock,
    )

    self_ws = seed_workspace(db_session, slug="ws-self2")
    self_user = seed_user(db_session)
    ctx = build_context(self_ws.id, slug=self_ws.slug, actor_id=self_user)
    set_current(ctx)

    with pytest.raises(ApprovalNotFound):
        get(ctx, session=db_session, approval_request_id=pending.id)


# ---------------------------------------------------------------------------
# list_pending
# ---------------------------------------------------------------------------


def test_list_pending_returns_oldest_first_with_has_more_boundary(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=actor_id)
    set_current(ctx)

    # Three rows with strictly-ascending created_at so ordering is
    # well-defined regardless of ULID monotonicity.
    base = clock.now()
    row_a = _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=actor_id,
        for_user_id=actor_id,
        clock=clock,
        created_at=base,
    )
    row_b = _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=actor_id,
        for_user_id=actor_id,
        clock=clock,
        created_at=base + timedelta(seconds=1),
    )
    row_c = _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=actor_id,
        for_user_id=actor_id,
        clock=clock,
        created_at=base + timedelta(seconds=2),
    )

    # limit=2 → first page returns the two oldest, has_more=True.
    page = list_pending(ctx, session=db_session, limit=2)
    assert [v.id for v in page.data] == [row_a.id, row_b.id]
    assert page.has_more is True
    assert page.next_cursor == row_b.id

    # Walking the cursor returns the trailing row, has_more=False.
    page2 = list_pending(ctx, session=db_session, cursor=page.next_cursor, limit=2)
    assert [v.id for v in page2.data] == [row_c.id]
    assert page2.has_more is False
    assert page2.next_cursor is None


def test_list_pending_skips_decided_rows(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=actor_id)
    set_current(ctx)
    base = clock.now()
    pending = _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=actor_id,
        for_user_id=actor_id,
        clock=clock,
        created_at=base,
    )
    decided = _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=actor_id,
        for_user_id=actor_id,
        clock=clock,
        created_at=base + timedelta(seconds=1),
    )
    deny(
        ctx,
        session=db_session,
        approval_request_id=decided.id,
        clock=clock,
        event_bus=bus,
    )
    db_session.flush()

    page = list_pending(ctx, session=db_session)
    assert [v.id for v in page.data] == [pending.id]
    assert page.has_more is False


def test_list_pending_validates_limit(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=actor_id)
    set_current(ctx)

    with pytest.raises(Validation):
        list_pending(ctx, session=db_session, limit=0)
    with pytest.raises(Validation):
        list_pending(ctx, session=db_session, limit=-3)


def test_list_pending_clamps_limit_to_max(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=actor_id)
    set_current(ctx)
    # Single row — the cap is enforced regardless of the queue size.
    _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=actor_id,
        for_user_id=actor_id,
        clock=clock,
    )

    # Asking for above the cap is honoured up to MAX_PAGE_LIMIT; the
    # service does not raise. The returned page is the queue itself.
    page = list_pending(ctx, session=db_session, limit=MAX_PAGE_LIMIT * 4)
    assert len(page.data) == 1
    assert page.has_more is False


# ---------------------------------------------------------------------------
# expire_due
# ---------------------------------------------------------------------------


def test_expire_due_empty_sweep_is_a_noop(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
    captured_decided: _CapturedDecided,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    # Pending row whose expires_at is well in the future.
    _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=actor_id,
        for_user_id=actor_id,
        clock=clock,
        expires_at=clock.now() + timedelta(days=30),
    )

    report = expire_due(
        session=db_session,
        now=clock.now(),
        clock=clock,
        event_bus=bus,
    )
    assert report.expired_count == 0
    assert report.expired_ids == ()
    assert captured_decided.events == []


def test_expire_due_flips_pending_rows_and_emits_events(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
    captured_decided: _CapturedDecided,
) -> None:
    # Two workspaces, both with one expired + one fresh row.
    ws_a = seed_workspace(db_session, slug="ws-a")
    user_a = seed_user(db_session)
    ws_b = seed_workspace(db_session, slug="ws-b")
    user_b = seed_user(db_session)

    base = clock.now()

    expired_a = _seed_pending(
        db_session,
        workspace_id=ws_a.id,
        requester_actor_id=user_a,
        for_user_id=user_a,
        clock=clock,
        expires_at=base - timedelta(seconds=1),
    )
    fresh_a = _seed_pending(
        db_session,
        workspace_id=ws_a.id,
        requester_actor_id=user_a,
        for_user_id=user_a,
        clock=clock,
        expires_at=base + timedelta(days=1),
    )
    expired_b = _seed_pending(
        db_session,
        workspace_id=ws_b.id,
        requester_actor_id=user_b,
        for_user_id=None,  # desk-only row
        clock=clock,
        expires_at=base - timedelta(hours=1),
    )

    report = expire_due(
        session=db_session,
        now=base,
        clock=clock,
        event_bus=bus,
    )
    db_session.flush()

    assert report.expired_count == 2
    assert set(report.expired_ids) == {expired_a.id, expired_b.id}

    # Expired rows transitioned.
    refreshed_a = db_session.get(ApprovalRequest, expired_a.id)
    assert refreshed_a is not None
    assert refreshed_a.status == "timed_out"
    assert refreshed_a.decided_at == base
    assert refreshed_a.decision_note_md == EXPIRED_DECISION_NOTE
    assert refreshed_a.decided_by is None

    refreshed_b = db_session.get(ApprovalRequest, expired_b.id)
    assert refreshed_b is not None
    assert refreshed_b.status == "timed_out"
    assert refreshed_b.decision_note_md == EXPIRED_DECISION_NOTE

    # Fresh row was left alone.
    refreshed_fresh = db_session.get(ApprovalRequest, fresh_a.id)
    assert refreshed_fresh is not None
    assert refreshed_fresh.status == "pending"

    # One ApprovalDecided per expired row, with workspace + decision
    # threaded through correctly.
    decisions = [
        (e.workspace_id, e.approval_request_id, e.decision, e.for_user_id)
        for e in captured_decided.events
    ]
    assert sorted(decisions) == sorted(
        [
            (ws_a.id, expired_a.id, "expired", user_a),
            (ws_b.id, expired_b.id, "expired", None),
        ]
    )


def test_expire_due_skips_rows_with_null_expires_at(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
    captured_decided: _CapturedDecided,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    # Pre-cd-9ghv legacy: ``expires_at`` is NULL. The sweep must
    # leave it alone — those rows live forever (or until a manual
    # decision lands).
    legacy = _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=actor_id,
        for_user_id=actor_id,
        clock=clock,
        expires_at=None,
    )
    # _seed_pending set expires_at via the default branch; force it
    # back to NULL here.
    legacy.expires_at = None
    db_session.flush()

    report = expire_due(
        session=db_session,
        now=clock.now() + timedelta(days=365),
        clock=clock,
        event_bus=bus,
    )
    assert report.expired_count == 0
    refreshed = db_session.get(ApprovalRequest, legacy.id)
    assert refreshed is not None
    assert refreshed.status == "pending"


# ---------------------------------------------------------------------------
# ApprovalView.from_row legacy fallback
# ---------------------------------------------------------------------------


def test_from_row_falls_back_to_rationale_md_for_legacy_rows(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    workspace = seed_workspace(db_session)
    actor_id = seed_user(db_session)
    pending = _seed_pending(
        db_session,
        workspace_id=workspace.id,
        requester_actor_id=actor_id,
        for_user_id=actor_id,
        clock=clock,
    )
    # Simulate a cd-cm5-era row: only ``rationale_md`` is set,
    # ``decision_note_md`` stays NULL.
    pending.rationale_md = "legacy reviewer note"
    pending.decision_note_md = None
    db_session.flush()

    view = ApprovalView.from_row(pending)
    assert view.decision_note_md == "legacy reviewer note"
