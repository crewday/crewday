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

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.identity.models import User
from app.adapters.db.llm.repositories import SqlAlchemyAgentRelayRequestRepository
from app.adapters.db.messaging.audiences import list_owner_manager_user_ids
from app.adapters.db.messaging.models import ChatChannel, ChatMessage
from app.adapters.db.messaging.repositories import SqlAlchemyEmailDeliveryRepository
from app.adapters.db.workspace.models import UserWorkspace, WorkEngagement
from app.adapters.llm.ports import LLMClient
from app.adapters.mail.null import NullMailer
from app.agent.dispatcher import (
    make_default_dispatcher,
    resolve_workspace_policy_always_gated_tools,
)
from app.agent.tokens import DelegatedTokenFactory
from app.api.deps import current_workspace_context, db_session, get_llm
from app.audit import write_audit
from app.authz.dep import Permission
from app.domain.agent.notifications import notify_agent_message_fallback
from app.domain.agent.relay_requests import (
    AgentRelayRequestCreate,
    create_relay,
    mark_relay_delivered,
)
from app.domain.agent.runtime import run_turn
from app.domain.errors import Forbidden, Validation
from app.domain.messaging.notifications import NotificationService
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import AgentMessageAppended, AgentMessagePayload
from app.tenancy import WorkspaceContext, tenant_agnostic
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


def _approval_notification_sink(
    session: Session,
    ctx: WorkspaceContext,
) -> NotificationService:
    return NotificationService(
        session=session,
        ctx=ctx,
        mailer=NullMailer(),
        email_deliveries=SqlAlchemyEmailDeliveryRepository(session),
    )


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


class AgentRelayRequestBody(BaseModel):
    """Tool body for manager-side agent-mediated relay requests."""

    model_config = ConfigDict(extra="forbid")

    target_user_id: str | None = Field(default=None, min_length=1, max_length=80)
    target_name: str | None = Field(default=None, min_length=1, max_length=160)
    request: str = Field(min_length=1, max_length=8_000)

    @field_validator("target_user_id", "target_name", "request")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = " ".join(value.split())
        if not stripped:
            raise ValueError("field must contain text")
        return stripped


class AgentRelayRequestResponse(BaseModel):
    """Safe tool result returned to the requester-side agent."""

    confirmation: str
    target_user_id: str
    target_display_label: str


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
    relay_gate = Depends(
        Permission("messaging.manager_channel", scope_kind="workspace")
    )

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
        # code-health: ignore[nloc params] API fields are generated schema contract.  # noqa: E501
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
        session.commit()
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
                    always_gated_tools=resolve_workspace_policy_always_gated_tools(
                        request.app,
                        session=session,
                        ctx=ctx,
                    ),
                    session=session,
                    ctx=ctx,
                ),
                token_factory=token_factory,
                agent_label=_SCOPE_AGENT_LABEL[scope],
                capability=_SCOPE_CAPABILITY[scope],
                event_bus=bus,
                clock=eff_clock,
                include_user_message=False,
                approval_notification_sink=_approval_notification_sink(session, ctx),
                approval_recipient_user_ids=list_owner_manager_user_ids(
                    session,
                    workspace_id=ctx.workspace_id,
                ),
                commit_before_tool_dispatch=True,
            )
        except Exception:
            session.rollback()
            try:
                token_factory.revoke_minted(ctx, session=session)
                session.commit()
            except Exception:
                session.rollback()
                _log.exception(
                    "agent.delegated_token_revoke_failed",
                    extra={
                        "workspace_id": ctx.workspace_id,
                        "actor_id": ctx.actor_id,
                        "scope": scope,
                    },
                )
            raise
        else:
            try:
                token_factory.revoke_minted(ctx, session=session)
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

    @r.post(
        "/manager/relay/request",
        response_model=AgentRelayRequestResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="agent.relay.request",
        summary="Ask a visible worker through their embedded agent",
        dependencies=[relay_gate],
        openapi_extra={
            "x-cli": {
                "group": "agent",
                "verb": "relay-request",
                "summary": "Ask a visible worker through their agent",
                "mutates": True,
            },
            "x-agent-scopes": ["manager"],
        },
    )
    def post_relay_request(
        body: AgentRelayRequestBody,
        ctx: _Ctx,
        session: _Db,
    ) -> AgentRelayRequestResponse:
        """Create and deliver an agent-mediated request to a visible worker."""
        _require_scope_access("manager", ctx)
        target = _resolve_relay_target(session, ctx=ctx, body=body)
        requester_label = _actor_label(session, ctx.actor_id)
        target_channel = _get_or_create_agent_channel_for_user(
            session,
            ctx=ctx,
            scope="employee",
            user_id=target.id,
            clock=eff_clock,
        )
        repo = SqlAlchemyAgentRelayRequestRepository(session)
        relay = create_relay(
            repo,
            ctx,
            AgentRelayRequestCreate(
                requester_user_id=ctx.actor_id,
                target_user_id=target.id,
                requester_display_label=requester_label,
                target_display_label=target.display_name,
                requester_scope="manager",
                requester_thread_ref=_external_ref("manager", ctx.actor_id),
                requester_message_ref=None,
                target_scope="employee",
                target_thread_ref=_external_ref("employee", target.id),
                request_summary=body.request,
            ),
            clock=eff_clock,
        )
        message = _append_relay_target_message(
            session,
            ctx=ctx,
            target_user_id=target.id,
            target_channel_id=target_channel.id,
            requester_label=requester_label,
            request_text=body.request,
            clock=eff_clock,
        )
        relay = mark_relay_delivered(
            repo,
            ctx,
            relay_id=relay.id,
            target_thread_ref=_external_ref("employee", target.id),
            target_message_ref=message.id,
            clock=eff_clock,
        )
        write_audit(
            session,
            ctx,
            entity_kind="agent_relay_request",
            entity_id=relay.id,
            action="agent.relay.requested",
            diff={
                "requester_user_id": ctx.actor_id,
                "target_user_id": target.id,
                "agent_relay_request_id": relay.id,
                "target_channel_id": target_channel.id,
                "target_message_id": message.id,
            },
            clock=eff_clock,
        )
        payload = _message_payload(message)
        session.commit()
        bus.publish(
            AgentMessageAppended(
                workspace_id=ctx.workspace_id,
                actor_id=target.id,
                actor_user_id=target.id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=message.created_at,
                scope="employee",
                message=AgentMessagePayload(
                    at=payload.at,
                    kind=payload.kind,
                    body=payload.body,
                    channel_kind=payload.channel_kind,
                ),
            )
        )
        notify_agent_message_fallback(
            recipient_user_id=target.id,
            message_body=message.body_md,
            workspace_slug=ctx.workspace_slug,
            chat_thread_ref=target_channel.id,
            message_id=message.id,
            sink=NotificationService(
                session=session,
                ctx=ctx,
                mailer=NullMailer(),
                clock=eff_clock,
                bus=bus,
                email_deliveries=SqlAlchemyEmailDeliveryRepository(session),
            ),
        )
        session.commit()
        return AgentRelayRequestResponse(
            confirmation=f"I asked {target.display_name}.",
            target_user_id=target.id,
            target_display_label=target.display_name,
        )

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


def _get_or_create_agent_channel_for_user(
    session: Session,
    *,
    ctx: WorkspaceContext,
    scope: AgentScope,
    user_id: str,
    clock: Clock,
) -> ChatChannel:
    existing = session.scalar(
        select(ChatChannel).where(
            ChatChannel.workspace_id == ctx.workspace_id,
            ChatChannel.kind == _SCOPE_CHANNEL_KIND[scope],
            ChatChannel.source == "app",
            ChatChannel.external_ref == _external_ref(scope, user_id),
            ChatChannel.archived_at.is_(None),
        )
    )
    if existing is not None:
        return existing
    channel = ChatChannel(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        kind=_SCOPE_CHANNEL_KIND[scope],
        source="app",
        external_ref=_external_ref(scope, user_id),
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


def _resolve_relay_target(
    session: Session,
    *,
    ctx: WorkspaceContext,
    body: AgentRelayRequestBody,
) -> User:
    if body.target_user_id is None and body.target_name is None:
        raise Validation(
            "target_user_id or target_name is required",
            extra={"error": "agent_relay_target_required"},
        )
    if body.target_user_id is not None and body.target_name is not None:
        raise Validation(
            "provide either target_user_id or target_name, not both",
            extra={"error": "agent_relay_target_conflict"},
        )
    eligible = _eligible_relay_targets(session, ctx=ctx)
    if body.target_user_id is not None:
        target = next(
            (
                candidate
                for candidate in eligible
                if candidate.id == body.target_user_id
            ),
            None,
        )
        if target is None:
            raise Forbidden(extra={"error": "agent_relay_target_forbidden"})
        return target

    assert body.target_name is not None
    needle = body.target_name.casefold()
    matches = [
        candidate
        for candidate in eligible
        if " ".join(candidate.display_name.split()).casefold() == needle
    ]
    if not matches:
        raise Forbidden(extra={"error": "agent_relay_target_forbidden"})
    if len(matches) > 1:
        raise Validation(
            "target name is ambiguous",
            extra={"error": "agent_relay_target_ambiguous"},
        )
    return matches[0]


def _eligible_relay_targets(session: Session, *, ctx: WorkspaceContext) -> list[User]:
    active_engagement = (
        select(WorkEngagement.user_id)
        .where(
            WorkEngagement.workspace_id == ctx.workspace_id,
            WorkEngagement.archived_on.is_(None),
        )
        .subquery()
    )
    active_worker_grant = (
        select(RoleGrant.user_id)
        .where(
            RoleGrant.workspace_id == ctx.workspace_id,
            RoleGrant.scope_kind == "workspace",
            RoleGrant.grant_role == "worker",
            RoleGrant.revoked_at.is_(None),
        )
        .subquery()
    )
    stmt = (
        select(User)
        .join(UserWorkspace, UserWorkspace.user_id == User.id)
        .where(
            UserWorkspace.workspace_id == ctx.workspace_id,
            User.archived_at.is_(None),
            User.id != ctx.actor_id,
            User.id.in_(select(active_engagement.c.user_id)).self_group()
            | User.id.in_(select(active_worker_grant.c.user_id)).self_group(),
        )
        .order_by(User.display_name.asc(), User.id.asc())
    )
    with tenant_agnostic():
        return list(session.scalars(stmt).all())


def _append_relay_target_message(
    session: Session,
    *,
    ctx: WorkspaceContext,
    target_user_id: str,
    target_channel_id: str,
    requester_label: str,
    request_text: str,
    clock: Clock,
) -> ChatMessage:
    row = ChatMessage(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        channel_id=target_channel_id,
        author_user_id=target_user_id,
        author_label="agent",
        body_md=f"{requester_label} is asking: {request_text}",
        attachments_json=[],
        source="app",
        provider_message_id=None,
        gateway_binding_id=None,
        dispatched_to_agent_at=None,
        created_at=clock.now(),
    )
    session.add(row)
    session.flush()
    return row


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
