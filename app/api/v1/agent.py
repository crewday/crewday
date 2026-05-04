"""Workspace-scoped embedded agent chat endpoints.

The production web shell calls these routes through ``fetchJson`` as
``/api/v1/agent/{employee|manager}/{log,message}``; the frontend wrapper
rewrites them to ``/w/<slug>/api/v1/...``. This router owns only the HTTP
chat-log seam. The deeper LLM turn runner is wired separately in
``app.domain.agent.runtime``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.db.messaging.models import ChatChannel, ChatMessage
from app.adapters.llm.ports import LLMClient
from app.agent.dispatcher import make_default_dispatcher
from app.agent.tokens import DelegatedTokenFactory
from app.api.deps import current_workspace_context, db_session, get_llm
from app.audit import write_audit
from app.domain.agent.runtime import run_turn
from app.domain.errors import Forbidden
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import AgentMessageAppended, AgentMessagePayload
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

_log = logging.getLogger(__name__)

__all__ = [
    "AgentLogMessage",
    "AgentMessageRequest",
    "build_agent_router",
    "get_agent_token_factory",
    "router",
]

AgentScope = Literal["employee", "manager"]
ChannelKind = Literal["staff", "manager"]

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_Llm = Annotated[LLMClient, Depends(get_llm)]

_SCOPE_CHANNEL_KIND: dict[AgentScope, ChannelKind] = {
    "employee": "staff",
    "manager": "manager",
}
_SCOPE_AGENT_LABEL: dict[AgentScope, str] = {
    "employee": "worker-chat-agent",
    "manager": "manager-chat-agent",
}
_SCOPE_CAPABILITY: dict[AgentScope, str] = {
    "employee": "chat.employee",
    "manager": "chat.manager",
}


class AgentMessageRequest(BaseModel):
    """Request body for ``POST /agent/{scope}/message``."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=8_000)

    @field_validator("body")
    @classmethod
    def _body_must_have_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("body must contain text")
        return stripped


class AgentLogMessage(BaseModel):
    """Frontend-compatible agent chat message shape."""

    at: datetime
    kind: Literal["agent", "user", "action"]
    body: str
    channel_kind: None = None


def get_agent_token_factory() -> DelegatedTokenFactory:
    """Return the delegated-token factory used by agent turns."""
    return DelegatedTokenFactory()


_TokenFactory = Annotated[DelegatedTokenFactory, Depends(get_agent_token_factory)]


def build_agent_router(
    *,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> APIRouter:
    """Return the workspace-scoped employee/manager agent router."""

    eff_clock = clock if clock is not None else SystemClock()
    bus = event_bus if event_bus is not None else default_event_bus
    r = APIRouter(prefix="/agent", tags=["agent"])

    @r.get(
        "/{scope}/log",
        response_model=list[AgentLogMessage],
        operation_id="agent.log.list",
        openapi_extra={
            "x-cli": {"group": "agent", "verb": "log", "scope_arg": "scope"}
        },
    )
    def get_log(scope: AgentScope, ctx: _Ctx, session: _Db) -> list[AgentLogMessage]:
        _require_scope_access(scope, ctx)
        channel = _get_agent_channel(session, ctx=ctx, scope=scope)
        if channel is None:
            return []
        rows = session.scalars(
            select(ChatMessage)
            .where(
                ChatMessage.workspace_id == ctx.workspace_id,
                ChatMessage.channel_id == channel.id,
                ChatMessage.kind != "summary",
            )
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        ).all()
        return [_message_payload(row) for row in rows]

    @r.post(
        "/{scope}/message",
        response_model=AgentLogMessage,
        status_code=status.HTTP_201_CREATED,
        operation_id="agent.message.create",
        openapi_extra={
            "x-cli": {
                "group": "agent",
                "verb": "message",
                "scope_arg": "scope",
                "body": "body",
            },
            "x-interactive-only": True,
        },
    )
    def post_message(
        request: Request,
        scope: AgentScope,
        body: AgentMessageRequest,
        ctx: _Ctx,
        session: _Db,
        llm_client: _Llm,
        token_factory: _TokenFactory,
    ) -> AgentLogMessage:
        _require_scope_access(scope, ctx)
        channel = _get_or_create_agent_channel(
            session,
            ctx=ctx,
            scope=scope,
            clock=eff_clock,
        )
        row = ChatMessage(
            id=new_ulid(clock=eff_clock),
            workspace_id=ctx.workspace_id,
            channel_id=channel.id,
            author_user_id=ctx.actor_id,
            author_label=_actor_label(session, ctx.actor_id),
            body_md=body.body.strip(),
            attachments_json=[],
            source="app",
            provider_message_id=None,
            gateway_binding_id=None,
            dispatched_to_agent_at=None,
            created_at=eff_clock.now(),
        )
        session.add(row)
        session.flush()
        write_audit(
            session,
            ctx,
            entity_kind="chat_message",
            entity_id=row.id,
            action="agent.message.sent",
            diff={
                "scope": scope,
                "channel_id": channel.id,
                "author_user_id": ctx.actor_id,
            },
            clock=eff_clock,
        )
        session.flush()
        payload = _message_payload(row)
        bus.publish(
            AgentMessageAppended(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                actor_user_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=row.created_at,
                scope=scope,
                message=AgentMessagePayload(
                    at=payload.at,
                    kind=payload.kind,
                    body=payload.body,
                    channel_kind=payload.channel_kind,
                ),
            )
        )
        try:
            run_turn(
                ctx,
                session=session,
                scope=scope,
                thread_id=channel.id,
                user_message=row.body_md,
                trigger="event",
                llm_client=llm_client,
                tool_dispatcher=make_default_dispatcher(
                    request.app,
                    ctx.workspace_slug,
                    always_gated_tools=frozenset(),
                ),
                token_factory=token_factory,
                agent_label=_SCOPE_AGENT_LABEL[scope],
                capability=_SCOPE_CAPABILITY[scope],
                event_bus=bus,
                clock=eff_clock,
                include_user_message=False,
            )
        finally:
            try:
                token_factory.revoke_minted(ctx)
            except Exception:
                _log.exception(
                    "agent.delegated_token_revoke_failed",
                    extra={
                        "workspace_id": ctx.workspace_id,
                        "actor_id": ctx.actor_id,
                        "scope": scope,
                    },
                )
        return payload

    return r


def _require_scope_access(scope: AgentScope, ctx: WorkspaceContext) -> None:
    if scope == "employee" and ctx.actor_grant_role != "worker":
        raise Forbidden(extra={"error": "agent_scope_forbidden"})
    if scope == "manager" and ctx.actor_grant_role != "manager":
        raise Forbidden(extra={"error": "agent_scope_forbidden"})


def _get_agent_channel(
    session: Session,
    *,
    ctx: WorkspaceContext,
    scope: AgentScope,
) -> ChatChannel | None:
    return session.scalar(
        select(ChatChannel).where(
            ChatChannel.workspace_id == ctx.workspace_id,
            ChatChannel.kind == _SCOPE_CHANNEL_KIND[scope],
            ChatChannel.source == "app",
            ChatChannel.external_ref == _external_ref(scope, ctx.actor_id),
            ChatChannel.archived_at.is_(None),
        )
    )


def _get_or_create_agent_channel(
    session: Session,
    *,
    ctx: WorkspaceContext,
    scope: AgentScope,
    clock: Clock,
) -> ChatChannel:
    existing = _get_agent_channel(session, ctx=ctx, scope=scope)
    if existing is not None:
        return existing
    channel = ChatChannel(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        kind=_SCOPE_CHANNEL_KIND[scope],
        source="app",
        external_ref=_external_ref(scope, ctx.actor_id),
        title=f"{scope} agent",
        created_at=clock.now(),
        archived_at=None,
    )
    session.add(channel)
    session.flush()
    return channel


def _external_ref(scope: AgentScope, actor_id: str) -> str:
    return f"agent:{scope}:{actor_id}"


def _actor_label(session: Session, actor_id: str) -> str:
    user = session.get(User, actor_id)
    if user is None or not user.display_name.strip():
        return "user"
    return user.display_name


def _message_payload(row: ChatMessage) -> AgentLogMessage:
    kind: Literal["agent", "user", "action"] = (
        "agent" if row.author_label == "agent" else "user"
    )
    return AgentLogMessage(
        at=_as_utc(row.created_at),
        kind=kind,
        body=row.body_md,
        channel_kind=None,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


router: APIRouter = build_agent_router()
