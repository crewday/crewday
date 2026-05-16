"""Unit tests for the worker-side staff chat assistant facade."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.llm.models import ApprovalRequest, LlmUsage
from app.adapters.llm.ports import LlmProviderError, LLMResponse, Tool
from app.adapters.llm.ports import LLMUsage as AdapterLLMUsage
from app.domain.agent.runtime import GateDecision, ToolResult
from app.domain.agent.staff_chat import (
    STAFF_CHAT_AGENT_LABEL,
    STAFF_CHAT_CAPABILITY,
    STAFF_CHAT_CHANNEL,
    STAFF_CHAT_HISTORY_CAP,
    STAFF_CHAT_SCOPE,
    VOICE_TRANSCRIPTION_PROMPT,
    VoiceTranscriptionUnavailable,
    is_staff_chat_tool,
    is_voice_input_enabled,
    run_staff_chat_turn,
    run_staff_chat_voice_turn,
    staff_chat_tool_names,
    suggest_staff_chat_tool,
    transcribe_voice_note,
)
from app.domain.llm import client as client_module
from app.domain.llm.client import LLMClient as RoutedLLMClient
from app.domain.llm.media import MediaTransformError
from app.events.bus import EventBus
from app.events.types import (
    AgentActionPending,
    AgentToolFinished,
    AgentTurnFinished,
    AgentTurnStarted,
)
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


class _StaticMimeSniffer:
    def __init__(self, content_type: str | None) -> None:
        self.content_type = content_type

    def sniff(self, payload: bytes, *, hint: str | None = None) -> str | None:
        del payload, hint
        return self.content_type


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


def test_voice_note_transcription_uses_fixed_prompt_and_audio_capability(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    ctx, _channel_id = _bind_and_seed(db_session)
    seed_assignment(
        db_session,
        workspace_id=ctx.workspace_id,
        capability="voice.transcribe",
        api_model_id="fake/audio-model",
        model_capabilities=("audio_input",),
    )
    audio_llm = ScriptedLLMClient(
        replies=[
            LLMResponse(
                text="Report the leaking sink",
                usage=AdapterLLMUsage(
                    prompt_tokens=3,
                    completion_tokens=4,
                    total_tokens=7,
                    seconds=3.5,
                ),
                model_id="fake/audio-model",
                finish_reason="stop",
            )
        ]
    )

    result = asyncio.run(
        transcribe_voice_note(
            ctx,
            session=db_session,
            audio_bytes=b"fake audio",
            declared_mime="audio/wav",
            workspace_settings={"voice.enabled": True},
            assigned_capabilities={"voice.transcribe", STAFF_CHAT_CAPABILITY},
            llm_client=RoutedLLMClient(audio_llm),
            mime_sniffer=_StaticMimeSniffer("audio/wav"),
            clock=clock,
        )
    )

    assert result.transcript == "Report the leaking sink"
    assert audio_llm.last_messages == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": VOICE_TRANSCRIPTION_PROMPT},
                {
                    "type": "input_audio",
                    "input_audio": {"data": "ZmFrZSBhdWRpbw==", "format": "wav"},
                },
            ],
        }
    ]
    rows = list(db_session.scalars(select(LlmUsage)).all())
    assert len(rows) == 1
    assert rows[0].capability == "voice.transcribe"
    assert rows[0].audio_seconds == Decimal("3.500")
    assert rows[0].fallback_attempts == 0


def test_voice_note_transcription_maps_mp4_container_to_m4a_format(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    ctx, _channel_id = _bind_and_seed(db_session)
    seed_assignment(
        db_session,
        workspace_id=ctx.workspace_id,
        capability="voice.transcribe",
        api_model_id="fake/audio-model",
        model_capabilities=("audio_input",),
    )
    audio_llm = ScriptedLLMClient(replies=[make_text_response("mp4 transcript")])

    result = asyncio.run(
        transcribe_voice_note(
            ctx,
            session=db_session,
            audio_bytes=b"fake mp4 audio",
            declared_mime="audio/mp4",
            workspace_settings={"voice.enabled": True},
            assigned_capabilities={"voice.transcribe", STAFF_CHAT_CAPABILITY},
            llm_client=RoutedLLMClient(audio_llm),
            mime_sniffer=_StaticMimeSniffer("audio/mp4"),
            filename="note.mp4",
            clock=clock,
        )
    )

    assert result.transcript == "mp4 transcript"
    assert audio_llm.last_messages is not None
    content = audio_llm.last_messages[0]["content"]
    assert isinstance(content, list)
    assert content[1]["input_audio"]["format"] == "m4a"


def test_voice_note_transcript_then_staff_chat_intent_are_separate_steps(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
) -> None:
    ctx, channel_id = _bind_and_seed(db_session)
    seed_assignment(
        db_session,
        workspace_id=ctx.workspace_id,
        capability="voice.transcribe",
        api_model_id="fake/audio-model",
        model_capabilities=("audio_input",),
    )
    audio_llm = ScriptedLLMClient(
        replies=[make_text_response("Mark the kitchen task done")]
    )
    chat_llm = ScriptedLLMClient(
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

    transcription, outcome = run_staff_chat_voice_turn(
        ctx,
        session=db_session,
        thread_id=channel_id,
        audio_bytes=b"fake audio",
        declared_mime="audio/wav",
        workspace_settings={"voice.enabled": True},
        assigned_capabilities={"voice.transcribe", STAFF_CHAT_CAPABILITY},
        routed_llm_client=RoutedLLMClient(audio_llm),
        chat_llm_client=chat_llm,
        tool_dispatcher=dispatcher,
        token_factory=FakeTokenFactory(),
        trigger="event",
        mime_sniffer=_StaticMimeSniffer("audio/wav"),
        event_bus=bus,
        clock=clock,
    )

    assert transcription.transcript == "Mark the kitchen task done"
    assert outcome.outcome == "replied"
    assert audio_llm.chat_calls == 1
    assert chat_llm.chat_calls == 2
    assert dispatcher.captured[0].call.name == "mark_task_done"
    assert chat_llm.last_messages is not None
    rendered = "".join(message["content"] for message in chat_llm.last_messages)
    assert "Mark the kitchen task done" in rendered
    assert "ZmFrZSBhdWRpbw==" not in rendered
    assert "input_audio" not in rendered
    rows = list(
        db_session.scalars(
            select(LlmUsage).order_by(LlmUsage.attempt.asc(), LlmUsage.capability.asc())
        ).all()
    )
    assert {row.capability for row in rows} == {
        "voice.transcribe",
        STAFF_CHAT_CAPABILITY,
    }


@pytest.mark.parametrize(
    ("workspace_settings", "assigned_capabilities", "sniffed_type", "error_reason"),
    [
        ({"voice.enabled": False}, {"voice.transcribe"}, "audio/wav", "voice_disabled"),
        ({"voice.enabled": True}, set(), "audio/wav", "voice_disabled"),
        (
            {"voice.enabled": True},
            {"voice.transcribe"},
            "text/plain",
            "unsupported_mime",
        ),
    ],
)
def test_voice_note_failures_do_not_enter_staff_chat(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
    workspace_settings: dict[str, object],
    assigned_capabilities: set[str],
    sniffed_type: str,
    error_reason: str,
) -> None:
    ctx, channel_id = _bind_and_seed(db_session)
    seed_assignment(
        db_session,
        workspace_id=ctx.workspace_id,
        capability="voice.transcribe",
        api_model_id="fake/audio-model",
        model_capabilities=("audio_input",),
    )
    audio_llm = ScriptedLLMClient(replies=[make_text_response("ignored")])
    chat_llm = ScriptedLLMClient(replies=[make_text_response("ignored")])
    dispatcher = FakeToolDispatcher()

    with pytest.raises(VoiceTranscriptionUnavailable) as exc_info:
        run_staff_chat_voice_turn(
            ctx,
            session=db_session,
            thread_id=channel_id,
            audio_bytes=b"fake audio",
            declared_mime="audio/wav",
            workspace_settings=workspace_settings,
            assigned_capabilities=assigned_capabilities,
            routed_llm_client=RoutedLLMClient(audio_llm),
            chat_llm_client=chat_llm,
            tool_dispatcher=dispatcher,
            token_factory=FakeTokenFactory(),
            trigger="event",
            mime_sniffer=_StaticMimeSniffer(sniffed_type),
            event_bus=bus,
            clock=clock,
        )

    assert exc_info.value.reason == error_reason
    assert chat_llm.chat_calls == 0
    assert dispatcher.captured == []


def test_voice_provider_failure_does_not_enter_staff_chat(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
) -> None:
    ctx, channel_id = _bind_and_seed(db_session)
    seed_assignment(
        db_session,
        workspace_id=ctx.workspace_id,
        capability="voice.transcribe",
        api_model_id="fake/audio-model",
        model_capabilities=("audio_input",),
    )
    audio_llm = ScriptedLLMClient(replies=[LlmProviderError("upstream rejected")])
    chat_llm = ScriptedLLMClient(replies=[make_text_response("ignored")])
    dispatcher = FakeToolDispatcher()

    with pytest.raises(VoiceTranscriptionUnavailable) as exc_info:
        run_staff_chat_voice_turn(
            ctx,
            session=db_session,
            thread_id=channel_id,
            audio_bytes=b"fake audio",
            declared_mime="audio/wav",
            workspace_settings={"voice.enabled": True},
            assigned_capabilities={"voice.transcribe"},
            routed_llm_client=RoutedLLMClient(audio_llm),
            chat_llm_client=chat_llm,
            tool_dispatcher=dispatcher,
            token_factory=FakeTokenFactory(),
            trigger="event",
            mime_sniffer=_StaticMimeSniffer("audio/wav"),
            event_bus=bus,
            clock=clock,
        )

    assert exc_info.value.reason == "provider_failed"
    assert chat_llm.chat_calls == 0
    assert dispatcher.captured == []


def test_voice_unassigned_failure_does_not_enter_staff_chat(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
) -> None:
    ctx, channel_id = _bind_and_seed(db_session)
    audio_llm = ScriptedLLMClient(replies=[make_text_response("ignored")])
    chat_llm = ScriptedLLMClient(replies=[make_text_response("ignored")])
    dispatcher = FakeToolDispatcher()

    with pytest.raises(VoiceTranscriptionUnavailable) as exc_info:
        run_staff_chat_voice_turn(
            ctx,
            session=db_session,
            thread_id=channel_id,
            audio_bytes=b"fake audio",
            declared_mime="audio/wav",
            workspace_settings={"voice.enabled": True},
            assigned_capabilities={"voice.transcribe"},
            routed_llm_client=RoutedLLMClient(audio_llm),
            chat_llm_client=chat_llm,
            tool_dispatcher=dispatcher,
            token_factory=FakeTokenFactory(),
            trigger="event",
            mime_sniffer=_StaticMimeSniffer("audio/wav"),
            event_bus=bus,
            clock=clock,
        )

    assert exc_info.value.reason == "voice_unassigned"
    assert audio_llm.chat_calls == 0
    assert chat_llm.chat_calls == 0
    assert dispatcher.captured == []


def test_voice_oversize_failure_does_not_enter_staff_chat(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
) -> None:
    ctx, channel_id = _bind_and_seed(db_session)
    seed_assignment(
        db_session,
        workspace_id=ctx.workspace_id,
        capability="voice.transcribe",
        api_model_id="fake/audio-model",
        model_capabilities=("audio_input",),
    )
    audio_llm = ScriptedLLMClient(replies=[make_text_response("ignored")])
    chat_llm = ScriptedLLMClient(replies=[make_text_response("ignored")])
    dispatcher = FakeToolDispatcher()

    with pytest.raises(VoiceTranscriptionUnavailable) as exc_info:
        run_staff_chat_voice_turn(
            ctx,
            session=db_session,
            thread_id=channel_id,
            audio_bytes=b"x" * (25 * 1024 * 1024 + 1),
            declared_mime="audio/wav",
            workspace_settings={"voice.enabled": True},
            assigned_capabilities={"voice.transcribe"},
            routed_llm_client=RoutedLLMClient(audio_llm),
            chat_llm_client=chat_llm,
            tool_dispatcher=dispatcher,
            token_factory=FakeTokenFactory(),
            trigger="event",
            mime_sniffer=_StaticMimeSniffer("audio/wav"),
            event_bus=bus,
            clock=clock,
        )

    assert exc_info.value.reason == "audio_too_large"
    assert audio_llm.chat_calls == 0
    assert chat_llm.chat_calls == 0
    assert dispatcher.captured == []


def test_voice_audio_conversion_failure_does_not_enter_staff_chat(
    db_session: Session,
    bus: EventBus,
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, channel_id = _bind_and_seed(db_session)
    seed_assignment(
        db_session,
        workspace_id=ctx.workspace_id,
        capability="voice.transcribe",
        api_model_id="fake/audio-model",
        model_capabilities=("audio_input",),
        audio_input_transform="wav_16khz_mono",
    )

    def fail_transform(
        audio_ref: dict[str, object], transform: str
    ) -> dict[str, object]:
        del audio_ref, transform
        raise MediaTransformError("conversion failed")

    monkeypatch.setattr(client_module, "normalize_audio_ref", fail_transform)
    audio_llm = ScriptedLLMClient(replies=[make_text_response("ignored")])
    chat_llm = ScriptedLLMClient(replies=[make_text_response("ignored")])
    dispatcher = FakeToolDispatcher()

    with pytest.raises(VoiceTranscriptionUnavailable) as exc_info:
        run_staff_chat_voice_turn(
            ctx,
            session=db_session,
            thread_id=channel_id,
            audio_bytes=b"fake audio",
            declared_mime="audio/wav",
            workspace_settings={"voice.enabled": True},
            assigned_capabilities={"voice.transcribe"},
            routed_llm_client=RoutedLLMClient(audio_llm),
            chat_llm_client=chat_llm,
            tool_dispatcher=dispatcher,
            token_factory=FakeTokenFactory(),
            trigger="event",
            mime_sniffer=_StaticMimeSniffer("audio/wav"),
            event_bus=bus,
            clock=clock,
        )

    assert exc_info.value.reason == "audio_conversion_failed"
    assert audio_llm.chat_calls == 0
    assert chat_llm.chat_calls == 0
    assert dispatcher.captured == []


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
        "agent.tool.started",
        "agent.tool.finished",
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
    assert outcome.chat_message_id is None
    assert dispatcher.is_gated_calls[0].name == "mark_task_done"
    assert dispatcher.captured == []
    assert list(db_session.scalars(select(AuditLog)).all()) == []
    approval = db_session.get(ApprovalRequest, outcome.approval_request_id)
    assert approval is not None
    assert approval.status == "pending"
    payload = approval.action_json
    assert isinstance(payload, dict)
    assert payload["tool_name"] == "mark_task_done"
    assert payload["card_summary"] == "Mark kitchen task done?"
    assert payload["card_risk"] == "low"
    assert payload["pre_approval_source"] == "workspace_policy"
    assert payload["agent_correlation_id"] == outcome.correlation_id
    assert approval.inline_channel == STAFF_CHAT_CHANNEL
    assert approval.for_user_id == ctx.actor_id
    assert captured_events.names() == [
        "agent.turn.started",
        "agent.tool.started",
        "agent.action.pending",
        "agent.turn.finished",
        "agent.tool.finished",
    ]
    pending = captured_events.events[-3]
    assert isinstance(pending, AgentActionPending)
    assert pending.approval_request_id == outcome.approval_request_id
    assert pending.scope == STAFF_CHAT_SCOPE
    finished = captured_events.events[-2]
    assert isinstance(finished, AgentTurnFinished)
    assert finished.outcome == "action"
    assert finished.scope == STAFF_CHAT_SCOPE
    tool_finished = captured_events.events[-1]
    assert isinstance(tool_finished, AgentToolFinished)
    assert tool_finished.status == "approval_required"
    assert tool_finished.scope == STAFF_CHAT_SCOPE


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
