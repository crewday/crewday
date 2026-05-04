"""Unit tests for the worker-side staff chat assistant facade."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.llm.ports import Tool
from app.domain.agent.runtime import GateDecision, ToolResult
from app.domain.agent.staff_chat import (
    STAFF_CHAT_AGENT_LABEL,
    STAFF_CHAT_CAPABILITY,
    STAFF_CHAT_CHANNEL,
    STAFF_CHAT_HISTORY_CAP,
    STAFF_CHAT_SCOPE,
    is_staff_chat_tool,
    is_voice_input_enabled,
    run_staff_chat_turn,
    staff_chat_tool_names,
    suggest_staff_chat_tool,
)
from app.events.bus import EventBus
from app.events.types import AgentTurnFinished, AgentTurnStarted
from app.tenancy import WorkspaceContext
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


def _bind_and_seed(db_session: Session) -> tuple[WorkspaceContext, str]:
    workspace = seed_workspace(db_session)
    user_id = seed_user(db_session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=user_id)
    set_current(ctx)
    channel_id = seed_channel(
        db_session,
        workspace_id=workspace.id,
        kind="staff",
        external_ref=f"agent:employee:{user_id}",
    )
    seed_budget_ledger(db_session, workspace_id=workspace.id, cap_cents=10_000)
    seed_assignment(
        db_session,
        workspace_id=workspace.id,
        capability=STAFF_CHAT_CAPABILITY,
    )
    return ctx, channel_id


def test_staff_chat_catalog_is_worker_scoped() -> None:
    assert staff_chat_tool_names() == {
        "get_tasks_today",
        "mark_task_done",
        "report_issue",
        "get_instruction",
        "get_my_bookings",
        "amend_booking",
        "decline_booking",
        "propose_booking",
        "get_inventory_low",
    }
    assert is_staff_chat_tool("get_tasks_today")
    assert not is_staff_chat_tool("payroll.issue")
    assert not is_staff_chat_tool("expenses.write")
    assert not is_staff_chat_tool("chat_message.create")


def test_obvious_staff_chat_intents_map_to_expected_tool() -> None:
    tasks = suggest_staff_chat_tool("What's on my plate today?")
    assert tasks is not None
    assert tasks.name == "get_tasks_today"
    assert tasks.input == {}

    done = suggest_staff_chat_tool("Mark the kitchen task done")
    assert done is not None
    assert done.name == "mark_task_done"
    assert done.input == {"query": "the kitchen task"}


def test_voice_input_requires_workspace_setting_and_assignment() -> None:
    assert is_voice_input_enabled(
        {"voice.enabled": True},
        {"voice.transcribe", STAFF_CHAT_CAPABILITY},
    )
    assert not is_voice_input_enabled(
        {"voice.enabled": False},
        {"voice.transcribe", STAFF_CHAT_CAPABILITY},
    )
    assert not is_voice_input_enabled(
        {"voice.enabled": True},
        {STAFF_CHAT_CAPABILITY},
    )


def test_staff_chat_turn_uses_employee_capability_and_worker_channel(
    db_session: Session,
    bus: EventBus,
    captured_events: CapturedEvents,
    clock: FrozenClock,
) -> None:
    ctx, channel_id = _bind_and_seed(db_session)
    token_factory = FakeTokenFactory()
    llm = ScriptedLLMClient(
        replies=[
            make_tool_call_response("get_tasks_today", {}),
            make_text_response("Kitchen, laundry."),
        ]
    )
    dispatcher = FakeToolDispatcher(
        responses={
            "get_tasks_today": [
                ToolResult(
                    call_id="placeholder",
                    status_code=200,
                    body={"tasks": ["Kitchen", "Laundry"]},
                    mutated=False,
                )
            ]
        }
    )

    outcome = run_staff_chat_turn(
        ctx,
        session=db_session,
        thread_id=channel_id,
        user_message="What's on my plate today?",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=token_factory,
        event_bus=bus,
        clock=clock,
    )

    assert outcome.outcome == "replied"
    assert outcome.tool_calls_made == 1
    assert dispatcher.captured
    captured = dispatcher.captured[0]
    assert captured.call.name == "get_tasks_today"
    assert captured.headers["X-Agent-Channel"] == STAFF_CHAT_CHANNEL
    assert captured.headers["X-Agent-Conversation-Ref"].startswith("agent:employee:")
    assert token_factory.last_call is not None
    assert token_factory.last_call[0] == STAFF_CHAT_AGENT_LABEL

    assert captured_events.names() == [
        "agent.turn.started",
        "agent.message.appended",
        "agent.turn.finished",
    ]
    started = captured_events.events[0]
    finished = captured_events.events[-1]
    assert isinstance(started, AgentTurnStarted)
    assert isinstance(finished, AgentTurnFinished)
    assert started.scope == STAFF_CHAT_SCOPE
    assert finished.scope == STAFF_CHAT_SCOPE
    assert finished.outcome == "replied"


def test_staff_chat_turn_only_advertises_worker_tools(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
) -> None:
    ctx, channel_id = _bind_and_seed(db_session)
    tools: tuple[Tool, ...] = (
        {
            "name": "get_tasks_today",
            "description": "List today's tasks.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "payroll.issue",
            "description": "Issue payroll.",
            "input_schema": {"type": "object", "properties": {}},
        },
    )
    llm = ScriptedLLMClient(replies=[make_text_response("No tasks.")])

    run_staff_chat_turn(
        ctx,
        session=db_session,
        thread_id=channel_id,
        user_message="What's on my plate today?",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=FakeToolDispatcher(tools=tools),
        token_factory=FakeTokenFactory(),
        event_bus=bus,
        clock=clock,
    )

    assert llm.last_tools is not None
    assert [tool["name"] for tool in llm.last_tools] == ["get_tasks_today"]


def test_mark_task_done_keeps_existing_policy_gating(
    db_session: Session,
    bus: EventBus,
    captured_events: CapturedEvents,
    clock: FrozenClock,
) -> None:
    ctx, channel_id = _bind_and_seed(db_session)
    llm = ScriptedLLMClient(
        replies=[make_tool_call_response("mark_task_done", {"task_id": "task_123"})]
    )
    dispatcher = FakeToolDispatcher(
        gates={
            "mark_task_done": GateDecision(
                gated=True,
                card_summary="Mark kitchen task done?",
                card_risk="low",
                pre_approval_source="workspace_policy",
            )
        }
    )

    outcome = run_staff_chat_turn(
        ctx,
        session=db_session,
        thread_id=channel_id,
        user_message="Mark the kitchen task done",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=FakeTokenFactory(),
        event_bus=bus,
        clock=clock,
    )

    assert outcome.outcome == "action"
    assert outcome.approval_request_id is not None
    assert dispatcher.is_gated_calls[0].name == "mark_task_done"
    assert dispatcher.captured == []
    finished = captured_events.events[-1]
    assert isinstance(finished, AgentTurnFinished)
    assert finished.outcome == "action"
    assert finished.scope == STAFF_CHAT_SCOPE


def test_allowed_staff_chat_mutation_writes_worker_audit_attribution(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
) -> None:
    ctx, channel_id = _bind_and_seed(db_session)
    token_factory = FakeTokenFactory()
    llm = ScriptedLLMClient(
        replies=[
            make_tool_call_response("mark_task_done", {"task_id": "task_123"}),
            make_text_response("Marked it done."),
        ]
    )
    dispatcher = FakeToolDispatcher(
        responses={
            "mark_task_done": [
                ToolResult(
                    call_id="placeholder",
                    status_code=200,
                    body={"id": "task_123", "state": "completed"},
                    mutated=True,
                )
            ]
        }
    )

    outcome = run_staff_chat_turn(
        ctx,
        session=db_session,
        thread_id=channel_id,
        user_message="Mark the kitchen task done",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=token_factory,
        event_bus=bus,
        clock=clock,
    )

    assert outcome.outcome == "replied"
    audit_rows = list(db_session.scalars(select(AuditLog)).all())
    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row.actor_kind == "user"
    assert row.actor_id == ctx.actor_id
    assert row.entity_kind == "agent_tool_call"
    assert row.action == "agent.tool.mark_task_done"
    diff = row.diff
    assert isinstance(diff, dict)
    assert diff["tool_name"] == "mark_task_done"
    assert diff["token_id"] == token_factory.token_id
    assert diff["agent_label"] == STAFF_CHAT_AGENT_LABEL
    assert diff["agent_correlation_id"] == outcome.correlation_id
    assert diff["status_code"] == 200


def test_out_of_catalog_tool_fail_closes_before_dispatch(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
) -> None:
    ctx, channel_id = _bind_and_seed(db_session)
    llm = ScriptedLLMClient(
        replies=[
            make_tool_call_response("payroll.issue", {"engagement_id": "we_123"}),
            make_text_response("I can't do payroll from worker chat."),
        ]
    )
    dispatcher = FakeToolDispatcher()

    outcome = run_staff_chat_turn(
        ctx,
        session=db_session,
        thread_id=channel_id,
        user_message="Run payroll",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=FakeTokenFactory(),
        event_bus=bus,
        clock=clock,
        max_iterations=2,
    )

    assert outcome.outcome == "replied"
    assert dispatcher.is_gated_calls == []
    assert dispatcher.captured == []
    assert llm.last_messages is not None
    rendered = "".join(message["content"] for message in llm.last_messages)
    assert "staff_chat_tool_forbidden" in rendered


def test_staff_chat_history_cap_keeps_compaction_floor() -> None:
    assert STAFF_CHAT_HISTORY_CAP >= 20
