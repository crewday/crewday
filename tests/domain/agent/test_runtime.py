"""Unit tests for :mod:`app.domain.agent.runtime` (cd-nyvm).

Covers each acceptance criterion on the Beads ticket:

* No-tool turn → exactly one chat-message reply, started + finished
  (replied), no audit row.
* One-read tool turn → no audit row for the read; LLM called twice
  (first → tool call, second → reply).
* One-write tool turn → one audit row carrying ``token_id`` +
  ``agent_label``, attributed to the delegating user.
* Gated write tool → :class:`ApprovalRequest` row, outcome=action.
* Scheduled trigger → no thread, no chat reply written, audit fires
  on a write.
* Iteration cap → outcome=timeout + friendly fallback reply.
* Wall-clock cap → outcome=timeout + friendly fallback reply.
* Forbidden read (403) → result threaded back into the prompt; no
  audit row.
* Headers propagated to dispatch.
* Budget exceeded → outcome=error, no LLM call leaves the client,
  no ``llm_usage`` row written.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.llm.models import ApprovalRequest, LlmUsage
from app.adapters.db.messaging.models import ChatMessage
from app.domain.agent.preferences import PreferenceUpdate, save_preference
from app.domain.agent.runtime import (
    APPROVAL_REQUEST_TTL,
    GateDecision,
    ToolResult,
    run_turn,
)
from app.events.types import AgentActionPending, AgentTurnFinished, AgentTurnStarted
from app.tenancy.current import set_current
from app.util.clock import FrozenClock
from tests.domain.agent.conftest import (
    CapturedEvents,
    FakeTokenFactory,
    FakeToolDispatcher,
    ScriptedLLMClient,
    build_context,
    make_text_response,
    make_tool_call_response,
    seed_assignment,
    seed_budget_ledger,
    seed_channel,
    seed_user,
    seed_workspace,
)

_CAPABILITY = "chat.manager"
_AGENT_LABEL = "manager-chat-agent"


def _bind(ctx) -> None:  # type: ignore[no-untyped-def]
    set_current(ctx)


def _bind_and_seed(db_session: Session, *, capability: str = _CAPABILITY):  # type: ignore[no-untyped-def]
    """Common setup: workspace, user, ctx, channel, ledger, capability."""
    workspace = seed_workspace(db_session)
    user_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=user_id)
    _bind(ctx)
    channel_id = seed_channel(db_session, workspace_id=workspace.id)
    seed_budget_ledger(db_session, workspace_id=workspace.id, cap_cents=10_000)
    seed_assignment(db_session, workspace_id=workspace.id, capability=capability)
    return workspace, ctx, channel_id


# ---------------------------------------------------------------------------
# Acceptance: no-tool turn
# ---------------------------------------------------------------------------


def test_no_tool_turn_emits_started_finished_and_one_chat_message(
    db_session: Session,
    bus,  # type: ignore[no-untyped-def]
    captured_events: CapturedEvents,
    clock: FrozenClock,
) -> None:
    _ws, ctx, channel_id = _bind_and_seed(db_session)

    llm = ScriptedLLMClient(replies=[make_text_response("Hello back!")])
    outcome = run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="Hi",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=FakeToolDispatcher(),
        token_factory=FakeTokenFactory(),
        agent_label=_AGENT_LABEL,
        capability=_CAPABILITY,
        event_bus=bus,
        clock=clock,
    )

    assert outcome.outcome == "replied"
    assert outcome.chat_message_id is not None
    assert outcome.tool_calls_made == 0
    assert outcome.llm_calls_made == 1

    # Started + finished, in order, both with the same correlation id.
    assert captured_events.names() == [
        "agent.turn.started",
        "agent.turn.finished",
    ]
    started = captured_events.events[0]
    finished = captured_events.events[1]
    assert isinstance(started, AgentTurnStarted)
    assert isinstance(finished, AgentTurnFinished)
    assert started.correlation_id == finished.correlation_id == outcome.correlation_id
    assert finished.outcome == "replied"

    # Exactly one chat row (the reply); no audit row.
    chat_row = db_session.get(ChatMessage, outcome.chat_message_id)
    assert chat_row is not None
    assert chat_row.body_md == "Hello back!"
    assert chat_row.author_label == "agent"
    assert chat_row.author_user_id == ctx.actor_id

    audit_count = db_session.scalar(select(func.count()).select_from(AuditLog))
    assert audit_count == 0


# ---------------------------------------------------------------------------
# Acceptance: one read tool, no audit
# ---------------------------------------------------------------------------


def test_one_read_tool_no_audit(
    db_session: Session,
    bus,  # type: ignore[no-untyped-def]
    captured_events: CapturedEvents,
    clock: FrozenClock,
) -> None:
    _ws, ctx, channel_id = _bind_and_seed(db_session)

    llm = ScriptedLLMClient(
        replies=[
            make_tool_call_response("tasks.list", {"property_id": "p1"}),
            make_text_response("There are 3 open tasks."),
        ]
    )
    dispatcher = FakeToolDispatcher(
        responses={
            "tasks.list": [
                ToolResult(
                    call_id="placeholder",
                    status_code=200,
                    body={"tasks": [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}]},
                    mutated=False,
                ),
            ]
        }
    )
    outcome = run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="How many open tasks?",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=FakeTokenFactory(),
        agent_label=_AGENT_LABEL,
        capability=_CAPABILITY,
        event_bus=bus,
        clock=clock,
    )

    assert outcome.outcome == "replied"
    assert outcome.tool_calls_made == 1
    assert outcome.llm_calls_made == 2
    assert dispatcher.captured  # the read fired
    assert not dispatcher.captured[0].call.name.startswith("agent.")  # raw tool name

    # Two LLM calls minimum (first → tool, second → reply); zero audit rows
    # because the tool is a read.
    audit_count = db_session.scalar(select(func.count()).select_from(AuditLog))
    assert audit_count == 0

    # Exactly one llm_usage row per LLM call.
    usage_count = db_session.scalar(select(func.count()).select_from(LlmUsage))
    assert usage_count == 2


def test_workspace_preferences_are_injected_into_system_prompt(
    db_session: Session,
    bus,  # type: ignore[no-untyped-def]
    clock: FrozenClock,
) -> None:
    _ws, ctx, channel_id = _bind_and_seed(db_session)
    save_preference(
        db_session,
        ctx,
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
        update=PreferenceUpdate(body_md="Use concise formal replies."),
        actor_user_id=ctx.actor_id,
        clock=clock,
    )

    llm = ScriptedLLMClient(replies=[make_text_response("Acknowledged.")])
    run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="Hi",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=FakeToolDispatcher(),
        token_factory=FakeTokenFactory(),
        agent_label=_AGENT_LABEL,
        capability=_CAPABILITY,
        event_bus=bus,
        clock=clock,
    )

    assert llm.last_messages is not None
    system = llm.last_messages[0]["content"]
    assert "## Workspace preferences --" in system
    assert "Use concise formal replies." in system


def test_blocked_action_returns_403_without_dispatch_or_audit(
    db_session: Session,
    bus,  # type: ignore[no-untyped-def]
    clock: FrozenClock,
) -> None:
    _ws, ctx, channel_id = _bind_and_seed(db_session)
    save_preference(
        db_session,
        ctx,
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
        update=PreferenceUpdate(
            body_md="",
            blocked_actions=("tasks.create",),
        ),
        actor_user_id=ctx.actor_id,
        clock=clock,
    )

    llm = ScriptedLLMClient(
        replies=[
            make_tool_call_response("tasks.create", {"title": "Blocked"}),
            make_text_response("I can't run that action."),
        ]
    )
    dispatcher = FakeToolDispatcher()
    outcome = run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="Create a task",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=FakeTokenFactory(),
        agent_label=_AGENT_LABEL,
        capability=_CAPABILITY,
        event_bus=bus,
        clock=clock,
    )

    assert outcome.outcome == "replied"
    assert outcome.tool_calls_made == 1
    assert dispatcher.is_gated_calls == []
    assert dispatcher.captured == []
    audit_rows = list(db_session.scalars(select(AuditLog)).all())
    assert [row.action for row in audit_rows] == ["agent_preference.updated"]
    assert llm.last_messages is not None
    tool_result = llm.last_messages[-1]["content"]
    assert "action_blocked_by_preferences" in tool_result


# ---------------------------------------------------------------------------
# Acceptance: one write tool with audit attribution
# ---------------------------------------------------------------------------


def test_one_write_tool_writes_audit_with_token_and_label(
    db_session: Session,
    bus,  # type: ignore[no-untyped-def]
    captured_events: CapturedEvents,
    clock: FrozenClock,
) -> None:
    _ws, ctx, channel_id = _bind_and_seed(db_session)

    llm = ScriptedLLMClient(
        replies=[
            make_tool_call_response(
                "tasks.create",
                {"title": "Restock kitchen", "property_id": "p1"},
            ),
            make_text_response("Created the task."),
        ]
    )
    dispatcher = FakeToolDispatcher(
        responses={
            "tasks.create": [
                ToolResult(
                    call_id="placeholder",
                    status_code=201,
                    body={"id": "task_001"},
                    mutated=True,
                ),
            ]
        }
    )
    factory = FakeTokenFactory()
    outcome = run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="Create a task: restock kitchen",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=factory,
        agent_label=_AGENT_LABEL,
        capability=_CAPABILITY,
        event_bus=bus,
        clock=clock,
    )

    assert outcome.outcome == "replied"
    assert outcome.tool_calls_made == 1

    audit_rows = list(db_session.scalars(select(AuditLog)).all())
    assert len(audit_rows) == 1
    row = audit_rows[0]
    # AC: actor_kind="user", actor_id = delegating user.
    assert row.actor_kind == "user"
    assert row.actor_id == ctx.actor_id
    assert row.entity_kind == "agent_tool_call"
    assert row.action == "agent.tool.tasks.create"
    diff = row.diff
    assert isinstance(diff, dict)
    assert diff["tool_name"] == "tasks.create"
    assert diff["token_id"] == factory.token_id
    assert diff["agent_label"] == _AGENT_LABEL
    assert diff["agent_correlation_id"] == outcome.correlation_id
    # ``status_code`` from the dispatch is denormalised on the diff.
    assert diff["status_code"] == 201


# ---------------------------------------------------------------------------
# Acceptance: gated write tool creates approval row and pauses
# ---------------------------------------------------------------------------


def test_gated_write_tool_creates_approval_request_and_pauses(
    db_session: Session,
    bus,  # type: ignore[no-untyped-def]
    captured_events: CapturedEvents,
    clock: FrozenClock,
) -> None:
    _ws, ctx, channel_id = _bind_and_seed(db_session)

    llm = ScriptedLLMClient(
        replies=[
            make_tool_call_response("payroll.issue", {"engagement_id": "we_001"}),
        ]
    )
    dispatcher = FakeToolDispatcher(
        gates={
            "payroll.issue": GateDecision(
                gated=True,
                card_summary="Issue payroll for we_001?",
                card_risk="high",
                pre_approval_source="workspace_always",
            )
        }
    )
    outcome = run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="Issue payroll",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=FakeTokenFactory(),
        agent_label=_AGENT_LABEL,
        capability=_CAPABILITY,
        event_bus=bus,
        clock=clock,
    )

    assert outcome.outcome == "action"
    assert outcome.approval_request_id is not None
    assert outcome.chat_message_id is None
    # Dispatcher.dispatch should NOT have been called — gating
    # halts the call before the side-effect fires.
    assert dispatcher.captured == []
    # No audit row because nothing executed.
    audit_count = db_session.scalar(select(func.count()).select_from(AuditLog))
    assert audit_count == 0

    approval = db_session.get(ApprovalRequest, outcome.approval_request_id)
    assert approval is not None
    assert approval.status == "pending"
    payload = approval.action_json
    assert isinstance(payload, dict)
    assert payload["tool_name"] == "payroll.issue"
    assert payload["card_summary"] == "Issue payroll for we_001?"
    assert payload["card_risk"] == "high"
    assert payload["pre_approval_source"] == "workspace_always"
    assert payload["agent_correlation_id"] == outcome.correlation_id

    # cd-9ghv column-promoted fields land on the row at insert time.
    # ``expires_at`` rides ``APPROVAL_REQUEST_TTL`` from the row's
    # ``created_at`` (the same instant the runtime stamped on the
    # SSE events). ``inline_channel`` follows the manager scope's
    # X-Agent-Channel value. ``for_user_id`` snapshots the
    # delegating user; ``resolved_user_mode`` is None until the
    # per-user mode column lands on :class:`User`.
    assert approval.created_at is not None
    assert approval.expires_at is not None
    assert approval.expires_at - approval.created_at == APPROVAL_REQUEST_TTL
    assert approval.inline_channel == "web_owner_sidebar"
    assert approval.for_user_id == ctx.actor_id
    assert approval.resolved_user_mode is None
    assert approval.decision_note_md is None
    assert approval.result_json is None

    # Inline-card SSE event for the delegating user, then the turn
    # lifecycle finished(action) event.
    pending_events = [
        e for e in captured_events.events if isinstance(e, AgentActionPending)
    ]
    assert len(pending_events) == 1
    pending = pending_events[0]
    assert pending.approval_request_id == outcome.approval_request_id
    assert pending.actor_user_id == ctx.actor_id
    assert pending.scope == "manager"
    assert pending.thread_id == channel_id

    finished = captured_events.events[-1]
    assert isinstance(finished, AgentTurnFinished)
    assert finished.outcome == "action"


# ---------------------------------------------------------------------------
# Acceptance: scheduled trigger
# ---------------------------------------------------------------------------


def test_scheduled_trigger_records_turn_and_audits(
    db_session: Session,
    bus,  # type: ignore[no-untyped-def]
    captured_events: CapturedEvents,
    clock: FrozenClock,
) -> None:
    _ws, ctx, _ = _bind_and_seed(db_session, capability="chat.compact")

    llm = ScriptedLLMClient(
        replies=[
            make_tool_call_response("digest.send", {"period": "daily"}),
            make_text_response("Daily digest dispatched."),
        ]
    )
    dispatcher = FakeToolDispatcher(
        responses={
            "digest.send": [
                ToolResult(
                    call_id="placeholder",
                    status_code=202,
                    body={"queued": True},
                    mutated=True,
                ),
            ]
        }
    )
    outcome = run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=None,  # scheduled — no chat thread
        user_message="Daily digest",
        trigger="schedule",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=FakeTokenFactory(),
        agent_label="digest-worker",
        capability="chat.compact",
        event_bus=bus,
        clock=clock,
    )

    assert outcome.outcome == "replied"
    assert outcome.chat_message_id is None  # no thread to land the reply on
    assert outcome.tool_calls_made == 1

    # Audit row landed for the write.
    audit_rows = list(db_session.scalars(select(AuditLog)).all())
    assert len(audit_rows) == 1
    assert audit_rows[0].action == "agent.tool.digest.send"

    # Started/finished still emit (uniform shape across triggers).
    assert captured_events.names() == [
        "agent.turn.started",
        "agent.turn.finished",
    ]
    started = captured_events.events[0]
    assert isinstance(started, AgentTurnStarted)
    assert started.trigger == "schedule"
    assert started.thread_id is None


# ---------------------------------------------------------------------------
# Acceptance: iteration cap → timeout
# ---------------------------------------------------------------------------


def test_iteration_cap_emits_timeout(
    db_session: Session,
    bus,  # type: ignore[no-untyped-def]
    captured_events: CapturedEvents,
    clock: FrozenClock,
) -> None:
    _ws, ctx, channel_id = _bind_and_seed(db_session)

    # Every LLM reply is a tool call, so the loop never exits via the
    # text-reply branch; the iteration cap fires at the for-loop's
    # natural exit. The dispatcher returns a generic 200 OK read for
    # each call; no audit rows land.
    llm = ScriptedLLMClient(
        replies=[make_tool_call_response("tasks.list", {"i": i}) for i in range(20)]
    )
    outcome = run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="Loop forever",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=FakeToolDispatcher(),
        token_factory=FakeTokenFactory(),
        agent_label=_AGENT_LABEL,
        capability=_CAPABILITY,
        event_bus=bus,
        clock=clock,
        max_iterations=3,  # cap dialed down for the test
    )

    assert outcome.outcome == "timeout"
    assert outcome.tool_calls_made == 3
    assert outcome.llm_calls_made == 3
    # Friendly fallback reply landed.
    chat_row = db_session.get(ChatMessage, outcome.chat_message_id)
    assert chat_row is not None
    assert "couldn't finish" in chat_row.body_md.lower()

    finished = captured_events.events[-1]
    assert isinstance(finished, AgentTurnFinished)
    assert finished.outcome == "timeout"


# ---------------------------------------------------------------------------
# Acceptance: wall-clock cap → timeout
# ---------------------------------------------------------------------------


def test_wall_clock_timeout_emits_timeout(
    db_session: Session,
    bus,  # type: ignore[no-untyped-def]
    captured_events: CapturedEvents,
    clock: FrozenClock,
) -> None:
    _ws, ctx, channel_id = _bind_and_seed(db_session)

    # Each LLM call advances the clock past the wall-clock cap. The
    # script supplies enough replies for the iteration cap to be
    # impossible — the wall-clock check must trip first.
    llm = ScriptedLLMClient(
        replies=[make_tool_call_response("tasks.list", {"i": i}) for i in range(10)]
    )

    class _AdvancingDispatcher(FakeToolDispatcher):
        def dispatch(self, call, *, token, headers):  # type: ignore[no-untyped-def]
            # Every dispatch advances the test clock by 25s; the
            # second iteration's pre-call wall-clock check trips
            # after 50s with the 30s cap below.
            clock.advance(timedelta(seconds=25))
            return super().dispatch(call, token=token, headers=headers)

    outcome = run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="Slow loop",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=_AdvancingDispatcher(),
        token_factory=FakeTokenFactory(),
        agent_label=_AGENT_LABEL,
        capability=_CAPABILITY,
        event_bus=bus,
        clock=clock,
        max_iterations=8,
        wall_clock_timeout_s=30,
    )

    assert outcome.outcome == "timeout"
    assert outcome.tool_calls_made <= 8  # iteration cap not the trigger
    finished = captured_events.events[-1]
    assert isinstance(finished, AgentTurnFinished)
    assert finished.outcome == "timeout"


# ---------------------------------------------------------------------------
# Acceptance: forbidden read returns empty / 403, runtime carries it back
# ---------------------------------------------------------------------------


def test_forbidden_read_returns_empty_or_403(
    db_session: Session,
    bus,  # type: ignore[no-untyped-def]
    captured_events: CapturedEvents,
    clock: FrozenClock,
) -> None:
    _ws, ctx, channel_id = _bind_and_seed(db_session)

    llm = ScriptedLLMClient(
        replies=[
            make_tool_call_response("payroll.read", {"engagement_id": "we_001"}),
            # The runtime appends the 403 result to history; the model
            # sees the 403 and replies plainly. We assert on the
            # surfaces (no audit, the tool result body is in the
            # replies' last message).
            make_text_response("I can't see payroll details for that engagement."),
        ]
    )
    dispatcher = FakeToolDispatcher(
        responses={
            "payroll.read": [
                ToolResult(
                    call_id="placeholder",
                    status_code=403,
                    body={"error": "forbidden"},
                    mutated=False,
                ),
            ]
        }
    )
    outcome = run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="Show payroll",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=FakeTokenFactory(),
        agent_label=_AGENT_LABEL,
        capability=_CAPABILITY,
        event_bus=bus,
        clock=clock,
    )

    assert outcome.outcome == "replied"
    # No audit row — the call was a read (mutated=False).
    audit_count = db_session.scalar(select(func.count()).select_from(AuditLog))
    assert audit_count == 0
    # The runtime threaded the 403 result back to the LLM; the second
    # LLM call's history therefore contains the JSON-rendered result.
    last_messages = llm.last_messages
    assert last_messages is not None
    rendered = "".join(m["content"] for m in last_messages)
    assert "403" in rendered
    assert "forbidden" in rendered


# ---------------------------------------------------------------------------
# Acceptance: headers propagated to tool calls
# ---------------------------------------------------------------------------


def test_headers_propagated_to_tool_calls(
    db_session: Session,
    bus,  # type: ignore[no-untyped-def]
    captured_events: CapturedEvents,
    clock: FrozenClock,
) -> None:
    workspace = seed_workspace(db_session)
    user_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=user_id)
    _bind(ctx)
    # Channel carries an external_ref so the runtime threads it onto
    # ``X-Agent-Conversation-Ref``.
    channel_id = seed_channel(
        db_session, workspace_id=workspace.id, external_ref="extern_ref_123"
    )
    seed_budget_ledger(db_session, workspace_id=workspace.id, cap_cents=10_000)
    seed_assignment(db_session, workspace_id=workspace.id, capability=_CAPABILITY)

    llm = ScriptedLLMClient(
        replies=[
            make_tool_call_response("tasks.list", {}),
            make_text_response("done"),
        ]
    )
    dispatcher = FakeToolDispatcher()
    outcome = run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="list tasks",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=FakeTokenFactory(),
        agent_label=_AGENT_LABEL,
        capability=_CAPABILITY,
        event_bus=bus,
        clock=clock,
    )

    assert outcome.outcome == "replied"
    assert dispatcher.captured
    captured = dispatcher.captured[0]
    assert captured.headers["X-Agent-Channel"] == "web_owner_sidebar"
    assert captured.headers["X-Agent-Conversation-Ref"] == "extern_ref_123"
    assert captured.headers["X-Agent-Reason"].startswith("agent.turn:")
    assert captured.token.plaintext.startswith("mip_")


# ---------------------------------------------------------------------------
# Acceptance: budget exceeded → outcome=error, no llm_usage row
# ---------------------------------------------------------------------------


def test_budget_exceeded_returns_error_no_llm_call_row(
    db_session: Session,
    bus,  # type: ignore[no-untyped-def]
    captured_events: CapturedEvents,
    clock: FrozenClock,
) -> None:
    workspace = seed_workspace(db_session)
    user_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=user_id)
    _bind(ctx)
    channel_id = seed_channel(db_session, workspace_id=workspace.id)
    # Cap pinned at 0 cents, spent already at 0 — but pricing for
    # the model returns >0 cents per call so the projection
    # overshoots immediately.
    seed_budget_ledger(db_session, workspace_id=workspace.id, cap_cents=0)
    seed_assignment(
        db_session,
        workspace_id=workspace.id,
        capability=_CAPABILITY,
        api_model_id="paid/model",
    )

    llm = ScriptedLLMClient(replies=[make_text_response("This should never run")])
    pricing = {"paid/model": (1_000_000, 1_000_000)}  # 1 cent per token

    outcome = run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="Hi",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=FakeToolDispatcher(),
        token_factory=FakeTokenFactory(),
        agent_label=_AGENT_LABEL,
        capability=_CAPABILITY,
        pricing=pricing,
        event_bus=bus,
        clock=clock,
    )

    assert outcome.outcome == "error"
    assert outcome.error_code == "budget_exceeded"
    # No LLM call left the client.
    assert llm.chat_calls == 0
    # No llm_usage row written.
    usage_count = db_session.scalar(select(func.count()).select_from(LlmUsage))
    assert usage_count == 0
    # No chat reply (the API layer surfaces the error to the user;
    # the runtime stays silent on the chat surface to avoid a
    # double-toast pattern).
    chat_count = db_session.scalar(select(func.count()).select_from(ChatMessage))
    assert chat_count == 0

    # Started + finished(error) on the wire.
    assert captured_events.names() == [
        "agent.turn.started",
        "agent.turn.finished",
    ]
    finished = captured_events.events[-1]
    assert isinstance(finished, AgentTurnFinished)
    assert finished.outcome == "error"


# ---------------------------------------------------------------------------
# Defensive: trigger='event' without thread_id is a misuse → error
# ---------------------------------------------------------------------------


def test_event_trigger_without_thread_id_raises(
    db_session: Session,
    bus,  # type: ignore[no-untyped-def]
    clock: FrozenClock,
) -> None:
    from app.domain.agent.runtime import AgentTurnError

    workspace = seed_workspace(db_session)

    user_id = seed_user(db_session)

    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=user_id)

    _bind(ctx)

    import pytest

    with pytest.raises(AgentTurnError):
        run_turn(
            ctx,
            session=db_session,
            scope="manager",
            thread_id=None,
            user_message="oops",
            trigger="event",
            llm_client=ScriptedLLMClient(),
            tool_dispatcher=FakeToolDispatcher(),
            token_factory=FakeTokenFactory(),
            agent_label=_AGENT_LABEL,
            capability=_CAPABILITY,
            event_bus=bus,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# Parser robustness — malformed XML, multiple calls, mixed text+call
# ---------------------------------------------------------------------------


def test_parse_tool_call_returns_none_on_malformed_xml() -> None:
    """A truncated / unclosed ``<tool_call>`` block reads as plain text."""
    from app.domain.agent.runtime import _parse_tool_call

    # Missing closing ``/>`` — the regex never matches; the runtime
    # treats the response as a reply.
    assert _parse_tool_call('<tool_call name="x" input="{}"') is None
    # Missing required attribute (no ``input=``).
    assert _parse_tool_call('<tool_call name="x"/>') is None
    # ``input`` payload is not JSON-decodable.
    assert _parse_tool_call('<tool_call name="x" input="{not json}"/>') is None
    # ``input`` decodes to a JSON array, not an object.
    assert _parse_tool_call('<tool_call name="x" input="[1,2,3]"/>') is None
    # Empty / None inputs short-circuit cleanly.
    assert _parse_tool_call("") is None


def test_parse_tool_call_returns_none_when_response_has_multiple_blocks() -> None:
    """Two ``<tool_call/>`` blocks → fall back to plain text."""
    from app.domain.agent.runtime import _parse_tool_call

    text = (
        '<tool_call name="tasks.list" input="{}"/>'
        " and then "
        '<tool_call name="tasks.create" input=\'{"title":"x"}\'/>'
    )
    # The runtime refuses to invent an order across multiple calls;
    # the surrounding chat sees the raw text as the agent's reply.
    assert _parse_tool_call(text) is None


def test_parse_tool_call_dispatches_when_block_wrapped_in_prose() -> None:
    """Single tool block with surrounding text still dispatches."""
    from app.domain.agent.runtime import _parse_tool_call

    text = (
        "Sure, let me check that for you. "
        '<tool_call name="tasks.list" input=\'{"property_id":"p1"}\'/>'
        " I'll let you know what I find."
    )
    parsed = _parse_tool_call(text)
    assert parsed is not None
    assert parsed.name == "tasks.list"
    assert parsed.input == {"property_id": "p1"}


def test_runtime_treats_multiple_tool_blocks_as_plain_reply(
    db_session: Session,
    bus,  # type: ignore[no-untyped-def]
    captured_events: CapturedEvents,
    clock: FrozenClock,
) -> None:
    """Multi-block model output lands as a chat reply, no dispatch."""
    from app.adapters.llm.ports import LLMResponse, LLMUsage

    _ws, ctx, channel_id = _bind_and_seed(db_session)

    multi = (
        '<tool_call name="tasks.list" input="{}"/>'
        ' <tool_call name="tasks.create" input=\'{"title":"x"}\'/>'
    )
    llm = ScriptedLLMClient(
        replies=[
            LLMResponse(
                text=multi,
                usage=LLMUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
                model_id="fake/model",
                finish_reason="stop",
            )
        ]
    )
    dispatcher = FakeToolDispatcher()
    outcome = run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="Hi",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=FakeTokenFactory(),
        agent_label=_AGENT_LABEL,
        capability=_CAPABILITY,
        event_bus=bus,
        clock=clock,
    )

    # No dispatch fired; the multi-block payload landed as the
    # agent's reply verbatim, the turn closed cleanly.
    assert outcome.outcome == "replied"
    assert outcome.tool_calls_made == 0
    assert dispatcher.captured == []
    chat_row = db_session.get(ChatMessage, outcome.chat_message_id)
    assert chat_row is not None
    assert chat_row.body_md == multi


def test_runtime_treats_malformed_block_as_plain_reply(
    db_session: Session,
    bus,  # type: ignore[no-untyped-def]
    captured_events: CapturedEvents,
    clock: FrozenClock,
) -> None:
    """Malformed ``<tool_call>`` lands as a chat reply, no dispatch."""
    from app.adapters.llm.ports import LLMResponse, LLMUsage

    _ws, ctx, channel_id = _bind_and_seed(db_session)

    # Unparseable JSON inside ``input``; the runtime falls through
    # to "treat as plain text" and the user sees the raw response.
    malformed = '<tool_call name="tasks.list" input="{not json}"/>'
    llm = ScriptedLLMClient(
        replies=[
            LLMResponse(
                text=malformed,
                usage=LLMUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
                model_id="fake/model",
                finish_reason="stop",
            )
        ]
    )
    dispatcher = FakeToolDispatcher()
    outcome = run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="Hi",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=FakeTokenFactory(),
        agent_label=_AGENT_LABEL,
        capability=_CAPABILITY,
        event_bus=bus,
        clock=clock,
    )

    assert outcome.outcome == "replied"
    assert outcome.tool_calls_made == 0
    assert dispatcher.captured == []
    chat_row = db_session.get(ChatMessage, outcome.chat_message_id)
    assert chat_row is not None
    assert chat_row.body_md == malformed
