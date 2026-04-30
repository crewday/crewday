"""Tests for :mod:`app.domain.agent.compaction` (cd-cn7v)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import LlmUsage
from app.adapters.db.messaging.models import ChatMessage
from app.domain.agent.compaction import (
    COMPACT_AGENT_LABEL,
    compact_thread,
    search_chat_archive,
)
from app.domain.agent.preferences import PreferenceUpdate, save_preference
from app.domain.agent.runtime import run_turn
from app.tenancy import WorkspaceContext
from app.tenancy.current import set_current
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.domain.agent.conftest import (
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

_COMPACT_CAPABILITY = "chat.compact"
_MANAGER_CAPABILITY = "chat.manager"
_AGENT_LABEL = "manager-chat-agent"
_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


def _bind(ctx: WorkspaceContext) -> None:
    set_current(ctx)


def _setup_compactor(
    session: Session, *, clock: FrozenClock
) -> tuple[WorkspaceContext, str]:
    workspace = seed_workspace(session)
    user_id = seed_user(session)
    ctx = build_context(workspace.id, slug=workspace.slug, actor_id=user_id)
    _bind(ctx)
    channel_id = seed_channel(session, workspace_id=workspace.id)
    seed_budget_ledger(session, workspace_id=workspace.id, cap_cents=10_000)
    seed_assignment(
        session,
        workspace_id=workspace.id,
        capability=_COMPACT_CAPABILITY,
    )
    _save_compaction_overrides(
        session,
        ctx,
        clock=clock,
        recent_floor_hours=1_000,
        recent_floor_turns=1,
        trigger_turns=1,
        trigger_days=999,
    )
    return ctx, channel_id


def _save_compaction_overrides(
    session: Session,
    ctx: WorkspaceContext,
    *,
    clock: FrozenClock,
    recent_floor_hours: int,
    recent_floor_turns: int,
    trigger_turns: int,
    trigger_days: int,
) -> None:
    save_preference(
        session,
        ctx,
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
        update=PreferenceUpdate(
            body_md=(
                f"chat.compact.recent_floor_hours: {recent_floor_hours}\n"
                f"chat.compact.recent_floor_turns: {recent_floor_turns}\n"
                f"chat.compact.trigger_turns: {trigger_turns}\n"
                f"chat.compact.trigger_days: {trigger_days}\n"
            )
        ),
        actor_user_id=ctx.actor_id,
        clock=clock,
    )


def _add_message(
    session: Session,
    ctx: WorkspaceContext,
    *,
    channel_id: str,
    body: str,
    created_delta: timedelta,
    author_label: str = "manager",
    kind: str = "message",
    compacted_into_id: str | None = None,
) -> ChatMessage:
    at = _PINNED + created_delta
    row = ChatMessage(
        id=new_ulid(clock=FrozenClock(at)),
        workspace_id=ctx.workspace_id,
        channel_id=channel_id,
        author_user_id=ctx.actor_id if author_label != COMPACT_AGENT_LABEL else None,
        author_label=author_label,
        body_md=body,
        kind=kind,
        attachments_json=[],
        source="app",
        provider_message_id=None,
        gateway_binding_id=None,
        dispatched_to_agent_at=None,
        summary_range_from_id=None,
        summary_range_to_id=None,
        compacted_into_id=compacted_into_id,
        created_at=at,
    )
    session.add(row)
    session.flush()
    return row


def test_compact_thread_writes_one_live_summary_and_links_originals(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    ctx, channel_id = _setup_compactor(db_session, clock=clock)
    rows = [
        _add_message(
            db_session,
            ctx,
            channel_id=channel_id,
            body=f"old topic {idx}",
            created_delta=timedelta(days=-3, minutes=idx),
        )
        for idx in range(3)
    ]

    llm = ScriptedLLMClient(replies=[make_text_response("Summary v1")])
    result = compact_thread(
        db_session,
        ctx,
        channel_id=channel_id,
        llm_client=llm,
        clock=clock,
    )

    assert result.compacted is True
    assert result.eligible_count == 2
    summary = db_session.get(ChatMessage, result.summary_id)
    assert summary is not None
    assert summary.kind == "summary"
    assert summary.body_md == "Summary v1"
    assert summary.summary_range_from_id == rows[0].id
    assert summary.summary_range_to_id == rows[1].id
    assert rows[0].compacted_into_id == summary.id
    assert rows[1].compacted_into_id == summary.id
    assert rows[2].compacted_into_id is None

    live_summary_count = db_session.scalar(
        select(func.count())
        .select_from(ChatMessage)
        .where(
            ChatMessage.channel_id == channel_id,
            ChatMessage.kind == "summary",
            ChatMessage.compacted_into_id.is_(None),
        )
    )
    assert live_summary_count == 1
    usage = db_session.scalar(
        select(LlmUsage).where(LlmUsage.capability == _COMPACT_CAPABILITY)
    )
    assert usage is not None
    assert usage.agent_label == COMPACT_AGENT_LABEL
    assert usage.actor_user_id is None
    assert usage.token_id is None


def test_recompaction_supersedes_prior_summary_without_a_second_live_summary(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    ctx, channel_id = _setup_compactor(db_session, clock=clock)
    rows = [
        _add_message(
            db_session,
            ctx,
            channel_id=channel_id,
            body=f"first batch {idx}",
            created_delta=timedelta(days=-3, minutes=idx),
        )
        for idx in range(3)
    ]
    llm = ScriptedLLMClient(
        replies=[make_text_response("Summary v1"), make_text_response("Summary v2")]
    )
    first = compact_thread(
        db_session,
        ctx,
        channel_id=channel_id,
        llm_client=llm,
        clock=clock,
    )
    added = [
        _add_message(
            db_session,
            ctx,
            channel_id=channel_id,
            body=f"second batch {idx}",
            created_delta=timedelta(days=-2, minutes=idx),
        )
        for idx in range(2)
    ]

    second = compact_thread(
        db_session,
        ctx,
        channel_id=channel_id,
        llm_client=llm,
        clock=clock,
    )

    old_summary = db_session.get(ChatMessage, first.summary_id)
    new_summary = db_session.get(ChatMessage, second.summary_id)
    assert old_summary is not None
    assert new_summary is not None
    assert old_summary.compacted_into_id == new_summary.id
    assert new_summary.compacted_into_id is None
    assert new_summary.summary_range_from_id == rows[0].id
    assert new_summary.summary_range_to_id == added[0].id
    assert rows[0].compacted_into_id == new_summary.id
    assert rows[1].compacted_into_id == new_summary.id
    assert rows[2].compacted_into_id == new_summary.id
    assert added[0].compacted_into_id == new_summary.id
    assert added[1].compacted_into_id is None
    live_summary_count = db_session.scalar(
        select(func.count())
        .select_from(ChatMessage)
        .where(
            ChatMessage.channel_id == channel_id,
            ChatMessage.kind == "summary",
            ChatMessage.compacted_into_id.is_(None),
        )
    )
    assert live_summary_count == 1


def test_recent_turn_floor_override_keeps_uncompacted_tail(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    ctx, channel_id = _setup_compactor(db_session, clock=clock)
    _save_compaction_overrides(
        db_session,
        ctx,
        clock=clock,
        recent_floor_hours=1_000,
        recent_floor_turns=2,
        trigger_turns=1,
        trigger_days=999,
    )
    rows = [
        _add_message(
            db_session,
            ctx,
            channel_id=channel_id,
            body=f"floor test {idx}",
            created_delta=timedelta(days=-3, minutes=idx),
        )
        for idx in range(4)
    ]

    result = compact_thread(
        db_session,
        ctx,
        channel_id=channel_id,
        llm_client=ScriptedLLMClient(replies=[make_text_response("Summary")]),
        clock=clock,
    )

    assert result.compacted is True
    assert rows[0].compacted_into_id == result.summary_id
    assert rows[1].compacted_into_id == result.summary_id
    assert rows[2].compacted_into_id is None
    assert rows[3].compacted_into_id is None


def test_search_chat_archive_returns_matching_compacted_originals(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    ctx, channel_id = _setup_compactor(db_session, clock=clock)
    _add_message(
        db_session,
        ctx,
        channel_id=channel_id,
        body="Boiler vendor asked for access code.",
        created_delta=timedelta(days=-3),
    )
    _add_message(
        db_session,
        ctx,
        channel_id=channel_id,
        body="Protected tail",
        created_delta=timedelta(days=-3, minutes=1),
    )
    _add_message(
        db_session,
        ctx,
        channel_id=channel_id,
        body="Newest protected tail",
        created_delta=timedelta(days=-3, minutes=2),
    )
    compact_thread(
        db_session,
        ctx,
        channel_id=channel_id,
        llm_client=ScriptedLLMClient(replies=[make_text_response("Summary")]),
        clock=clock,
    )

    results = search_chat_archive(
        db_session,
        ctx,
        channel_id=channel_id,
        query="boiler",
    )

    assert [row.body_md for row in results] == ["Boiler vendor asked for access code."]


def test_runtime_history_uses_live_summary_and_uncompacted_messages(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    ctx, channel_id = _setup_compactor(db_session, clock=clock)
    seed_assignment(
        db_session,
        workspace_id=ctx.workspace_id,
        capability=_MANAGER_CAPABILITY,
        api_model_id="fake/manager-chat-model",
    )
    old = _add_message(
        db_session,
        ctx,
        channel_id=channel_id,
        body="already compacted original",
        created_delta=timedelta(days=-5),
    )
    summary = _add_message(
        db_session,
        ctx,
        channel_id=channel_id,
        body="Prior durable facts.",
        created_delta=timedelta(days=-4),
        author_label=COMPACT_AGENT_LABEL,
        kind="summary",
    )
    summary.summary_range_from_id = old.id
    summary.summary_range_to_id = old.id
    old.compacted_into_id = summary.id
    recent = _add_message(
        db_session,
        ctx,
        channel_id=channel_id,
        body="recent live message",
        created_delta=timedelta(minutes=-1),
    )

    llm = ScriptedLLMClient(replies=[make_text_response("Reply")])
    run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="What is current?",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=FakeToolDispatcher(),
        token_factory=FakeTokenFactory(),
        agent_label=_AGENT_LABEL,
        capability=_MANAGER_CAPABILITY,
        clock=clock,
    )

    assert llm.last_messages is not None
    rendered = "\n".join(message["content"] for message in llm.last_messages)
    assert "Conversation summary:\nPrior durable facts." in rendered
    assert recent.body_md in rendered
    assert old.body_md not in rendered
    assert llm.last_messages[1]["role"] == "system"


def test_runtime_handles_chat_archive_tool_without_dispatcher(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    ctx, channel_id = _setup_compactor(db_session, clock=clock)
    seed_assignment(
        db_session,
        workspace_id=ctx.workspace_id,
        capability=_MANAGER_CAPABILITY,
        api_model_id="fake/manager-chat-model",
    )
    old = _add_message(
        db_session,
        ctx,
        channel_id=channel_id,
        body="Gate code was 2468 during the vendor visit.",
        created_delta=timedelta(days=-5),
    )
    summary = _add_message(
        db_session,
        ctx,
        channel_id=channel_id,
        body="Vendor visit was resolved.",
        created_delta=timedelta(days=-4),
        author_label=COMPACT_AGENT_LABEL,
        kind="summary",
    )
    summary.summary_range_from_id = old.id
    summary.summary_range_to_id = old.id
    old.compacted_into_id = summary.id

    llm = ScriptedLLMClient(
        replies=[
            make_tool_call_response("search_chat_archive", {"q": "2468"}),
            make_text_response("Found it."),
        ]
    )
    dispatcher = FakeToolDispatcher()
    run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="What gate code did we give the vendor?",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=FakeTokenFactory(),
        agent_label=_AGENT_LABEL,
        capability=_MANAGER_CAPABILITY,
        clock=clock,
    )

    assert dispatcher.is_gated_calls == []
    assert dispatcher.captured == []
    assert llm.last_messages is not None
    tool_result = llm.last_messages[-1]["content"]
    assert "Gate code was 2468" in tool_result
