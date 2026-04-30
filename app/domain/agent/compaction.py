"""Conversation compaction worker for agent chat threads."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import AgentPreference
from app.adapters.db.messaging.models import ChatChannel, ChatMessage
from app.adapters.llm.ports import ChatMessage as LlmChatMessage
from app.adapters.llm.ports import LLMClient, LLMResponse
from app.domain.llm.budget import (
    PricingTable,
    check_budget,
    default_pricing_table,
    estimate_cost_cents,
)
from app.domain.llm.router import ModelPick, resolve_primary
from app.domain.llm.usage_recorder import AgentAttribution, record
from app.services.llm import get_active_prompt
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "COMPACT_AGENT_LABEL",
    "CompactionConfig",
    "CompactionResult",
    "compact_due_threads",
    "compact_thread",
    "search_chat_archive",
]

COMPACT_CAPABILITY: Final[str] = "chat.compact"
COMPACT_AGENT_LABEL: Final[str] = "compact-worker"
DEFAULT_RECENT_FLOOR_HOURS: Final[int] = 24
DEFAULT_RECENT_FLOOR_TURNS: Final[int] = 20
DEFAULT_TRIGGER_TURNS: Final[int] = 200
DEFAULT_TRIGGER_DAYS: Final[int] = 30
_PROJECTED_PROMPT_TOKENS: Final[int] = 2_000
_PROJECTED_COMPLETION_TOKENS: Final[int] = 800
_OVERRIDE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?m)^\s*(chat\.compact\.(?:recent_floor_hours|recent_floor_turns|trigger_turns|trigger_days))\s*[:=]\s*(\d+)\s*$"
)
_NUMERIC_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\d+(?:[.,:/-]\d+)*")


@dataclass(frozen=True, slots=True)
class CompactionConfig:
    recent_floor_hours: int = DEFAULT_RECENT_FLOOR_HOURS
    recent_floor_turns: int = DEFAULT_RECENT_FLOOR_TURNS
    trigger_turns: int = DEFAULT_TRIGGER_TURNS
    trigger_days: int = DEFAULT_TRIGGER_DAYS


@dataclass(frozen=True, slots=True)
class CompactionResult:
    compacted: bool
    summary_id: str | None = None
    eligible_count: int = 0


def compact_due_threads(
    session: Session,
    ctx: WorkspaceContext,
    *,
    llm_client: LLMClient,
    limit: int = 50,
    pricing: PricingTable | None = None,
    clock: Clock | None = None,
) -> list[CompactionResult]:
    """Run compaction for due agent channels in this workspace."""
    eff_clock = clock if clock is not None else SystemClock()
    channel_ids = session.scalars(
        select(ChatChannel.id)
        .where(ChatChannel.workspace_id == ctx.workspace_id)
        .order_by(ChatChannel.created_at.asc(), ChatChannel.id.asc())
        .limit(limit)
    ).all()
    results: list[CompactionResult] = []
    for channel_id in channel_ids:
        result = compact_thread(
            session,
            ctx,
            channel_id=channel_id,
            llm_client=llm_client,
            pricing=pricing,
            clock=eff_clock,
        )
        if result.compacted:
            results.append(result)
    return results


def compact_thread(
    session: Session,
    ctx: WorkspaceContext,
    *,
    channel_id: str,
    llm_client: LLMClient,
    pricing: PricingTable | None = None,
    clock: Clock | None = None,
) -> CompactionResult:
    """Compact one thread when it exceeds the §11 trigger bounds."""
    eff_clock = clock if clock is not None else SystemClock()
    now = eff_clock.now()
    config = _resolve_config(session, ctx)
    originals = _live_originals(session, ctx, channel_id)
    eligible = _eligible_for_compaction(originals, config=config, now=now)
    if not eligible or not _triggered(eligible, config=config, now=now):
        return CompactionResult(compacted=False, eligible_count=len(eligible))

    model_pick = resolve_primary(session, ctx, COMPACT_CAPABILITY, clock=eff_clock)
    pricing_table = pricing if pricing is not None else default_pricing_table()
    projected_cost = estimate_cost_cents(
        prompt_tokens=_PROJECTED_PROMPT_TOKENS,
        max_output_tokens=_PROJECTED_COMPLETION_TOKENS,
        api_model_id=model_pick.api_model_id,
        pricing=pricing_table,
        workspace_id=ctx.workspace_id,
    )
    check_budget(
        session,
        ctx,
        capability=COMPACT_CAPABILITY,
        projected_cost_cents=projected_cost,
        clock=eff_clock,
    )

    live_summary = _live_summary(session, ctx, channel_id)
    prompt = get_active_prompt(
        session,
        COMPACT_CAPABILITY,
        default=_default_compaction_prompt(),
        clock=eff_clock.now,
    )
    messages = _compaction_messages(
        system_prompt=prompt,
        live_summary=live_summary,
        eligible=eligible,
    )
    correlation_id = new_ulid(clock=eff_clock)
    response = _call_compactor(
        session,
        ctx,
        llm_client=llm_client,
        model_pick=model_pick,
        messages=messages,
        correlation_id=correlation_id,
        pricing=pricing_table,
        clock=eff_clock,
    )

    first = (
        live_summary.summary_range_from_id or eligible[0].id
        if live_summary is not None
        else eligible[0].id
    )
    last = eligible[-1].id
    summary_id = new_ulid(clock=eff_clock)
    verified_text = _verify_numeric_claims(
        response.text.strip(),
        live_summary=live_summary,
        eligible=eligible,
    )
    summary = ChatMessage(
        id=summary_id,
        workspace_id=ctx.workspace_id,
        channel_id=channel_id,
        author_user_id=None,
        author_label=COMPACT_AGENT_LABEL,
        body_md=verified_text,
        kind="summary",
        attachments_json=[],
        source="app",
        provider_message_id=None,
        gateway_binding_id=None,
        dispatched_to_agent_at=None,
        summary_range_from_id=first,
        summary_range_to_id=last,
        compacted_into_id=live_summary.id if live_summary is not None else None,
        created_at=now,
    )
    session.add(summary)
    session.flush()
    if live_summary is not None:
        live_summary.compacted_into_id = summary_id
        session.flush()
        summary.compacted_into_id = None
        session.flush()
    for row in _covered_originals(
        session, ctx, channel_id, first_id=first, last_id=last
    ):
        row.compacted_into_id = summary_id
    session.flush()
    return CompactionResult(
        compacted=True,
        summary_id=summary_id,
        eligible_count=len(eligible),
    )


def search_chat_archive(
    session: Session,
    ctx: WorkspaceContext,
    *,
    channel_id: str,
    query: str,
    limit: int = 10,
) -> list[ChatMessage]:
    """Return compacted originals matching ``query`` for the archive tool."""
    q = query.strip().lower()
    if not q:
        return []
    stmt = (
        select(ChatMessage)
        .where(
            ChatMessage.workspace_id == ctx.workspace_id,
            ChatMessage.channel_id == channel_id,
            ChatMessage.kind != "summary",
            ChatMessage.compacted_into_id.is_not(None),
            func.lower(ChatMessage.body_md).contains(q),
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(max(1, limit))
    )
    return list(session.scalars(stmt).all())


def _resolve_config(session: Session, ctx: WorkspaceContext) -> CompactionConfig:
    row = session.scalar(
        select(AgentPreference).where(
            AgentPreference.workspace_id == ctx.workspace_id,
            AgentPreference.scope_kind == "workspace",
            AgentPreference.scope_id == ctx.workspace_id,
            AgentPreference.archived_at.is_(None),
        )
    )
    values = {
        "recent_floor_hours": DEFAULT_RECENT_FLOOR_HOURS,
        "recent_floor_turns": DEFAULT_RECENT_FLOOR_TURNS,
        "trigger_turns": DEFAULT_TRIGGER_TURNS,
        "trigger_days": DEFAULT_TRIGGER_DAYS,
    }
    if row is not None:
        for key, raw in _OVERRIDE_RE.findall(row.body_md):
            value = max(1, int(raw))
            values[key.removeprefix("chat.compact.")] = value
    return CompactionConfig(**values)


def _live_originals(
    session: Session, ctx: WorkspaceContext, channel_id: str
) -> list[ChatMessage]:
    return list(
        session.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.workspace_id == ctx.workspace_id,
                ChatMessage.channel_id == channel_id,
                ChatMessage.kind != "summary",
                ChatMessage.compacted_into_id.is_(None),
            )
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        ).all()
    )


def _live_summary(
    session: Session, ctx: WorkspaceContext, channel_id: str
) -> ChatMessage | None:
    return session.scalar(
        select(ChatMessage).where(
            ChatMessage.workspace_id == ctx.workspace_id,
            ChatMessage.channel_id == channel_id,
            ChatMessage.kind == "summary",
            ChatMessage.compacted_into_id.is_(None),
        )
    )


def _eligible_for_compaction(
    originals: list[ChatMessage],
    *,
    config: CompactionConfig,
    now: datetime,
) -> list[ChatMessage]:
    if len(originals) <= config.recent_floor_turns:
        return []
    recent_by_turn = originals[-config.recent_floor_turns :]
    time_cutoff = now - timedelta(hours=config.recent_floor_hours)
    recent_by_time = [row for row in originals if _aware(row.created_at) >= time_cutoff]
    protected = (
        recent_by_time if len(recent_by_time) < len(recent_by_turn) else recent_by_turn
    )
    protected_ids = {row.id for row in protected}
    return [row for row in originals if row.id not in protected_ids]


def _triggered(
    eligible: list[ChatMessage],
    *,
    config: CompactionConfig,
    now: datetime,
) -> bool:
    if len(eligible) > config.trigger_turns:
        return True
    oldest = min(_aware(row.created_at) for row in eligible)
    return oldest < now - timedelta(days=config.trigger_days)


def _covered_originals(
    session: Session,
    ctx: WorkspaceContext,
    channel_id: str,
    *,
    first_id: str,
    last_id: str,
) -> list[ChatMessage]:
    return list(
        session.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.workspace_id == ctx.workspace_id,
                ChatMessage.channel_id == channel_id,
                ChatMessage.kind != "summary",
                ChatMessage.id >= first_id,
                ChatMessage.id <= last_id,
            )
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        ).all()
    )


def _compaction_messages(
    *,
    system_prompt: str,
    live_summary: ChatMessage | None,
    eligible: list[ChatMessage],
) -> list[LlmChatMessage]:
    lines: list[str] = []
    if live_summary is not None:
        lines.append("Existing summary:")
        lines.append(live_summary.body_md)
        lines.append("")
    lines.append("Original messages to compact:")
    for row in eligible:
        lines.append(f"- {row.author_label}: {row.body_md}")
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _call_compactor(
    session: Session,
    ctx: WorkspaceContext,
    *,
    llm_client: LLMClient,
    model_pick: ModelPick,
    messages: list[LlmChatMessage],
    correlation_id: str,
    pricing: PricingTable,
    clock: Clock,
) -> LLMResponse:
    started = clock.now()
    response = llm_client.chat(
        model_id=model_pick.api_model_id,
        messages=messages,
        max_tokens=model_pick.max_tokens or _PROJECTED_COMPLETION_TOKENS,
        temperature=model_pick.temperature
        if model_pick.temperature is not None
        else 0.0,
    )
    elapsed_ms = int((clock.now() - started).total_seconds() * 1000)
    cost_cents = estimate_cost_cents(
        prompt_tokens=response.usage.prompt_tokens,
        max_output_tokens=response.usage.completion_tokens,
        api_model_id=model_pick.api_model_id,
        pricing=pricing,
        workspace_id=ctx.workspace_id,
    )
    record(
        session,
        ctx,
        capability=COMPACT_CAPABILITY,
        model_pick=model_pick,
        fallback_attempts=0,
        correlation_id=correlation_id,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        cost_cents=cost_cents,
        latency_ms=elapsed_ms,
        status="ok",
        finish_reason=response.finish_reason,
        attribution=AgentAttribution(
            actor_user_id=None,
            token_id=None,
            agent_label=COMPACT_AGENT_LABEL,
        ),
        clock=clock,
    )
    return response


def _default_compaction_prompt() -> str:
    return (
        "Summarise resolved chat topics for future agent turns. Keep durable facts "
        "whose usefulness outlives the exchange, such as stable user constraints, "
        "property instructions, dates, names, and access details. Omit one-off "
        "chatter and completed actions whose outcome is already recorded on a "
        "canonical entity row; err toward stripping those because the entity is "
        "the source of truth. Never write or propose agent preference edits. "
        "Do not introduce numeric claims that are not present in the supplied "
        "summary or original messages. Write concise Markdown."
    )


def _verify_numeric_claims(
    summary_text: str,
    *,
    live_summary: ChatMessage | None,
    eligible: list[ChatMessage],
) -> str:
    source_parts = [row.body_md for row in eligible]
    if live_summary is not None:
        source_parts.append(live_summary.body_md)
    source_numbers = set(_NUMERIC_TOKEN_RE.findall("\n".join(source_parts)))
    if not source_numbers:
        verified = "\n".join(
            line
            for line in summary_text.splitlines()
            if not _NUMERIC_TOKEN_RE.search(line)
        ).strip()
        return verified or "No verified durable numeric facts survived compaction."
    verified_lines = [
        line
        for line in summary_text.splitlines()
        if set(_NUMERIC_TOKEN_RE.findall(line)).issubset(source_numbers)
    ]
    verified = "\n".join(verified_lines).strip()
    if verified:
        return verified
    return "No verified durable numeric facts survived compaction."


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
