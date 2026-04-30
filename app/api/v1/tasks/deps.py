"""Shared FastAPI dependencies and request helpers for task routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import ApiToken
from app.adapters.llm.ports import LLMClient
from app.adapters.storage.ports import MimeSniffer, Storage
from app.api.deps import (
    current_workspace_context,
    db_session,
    get_llm,
    get_mime_sniffer,
    get_storage,
)
from app.domain.llm.usage_recorder import AgentAttribution
from app.domain.time.occurrence_shifts import register_occurrence_shift_subscription
from app.events import Event, EventBus, registered_events
from app.events.bus import bus as default_event_bus
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.tenancy.middleware import ACTOR_STATE_ATTR, ActorIdentity

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_Storage = Annotated[Storage, Depends(get_storage)]
_MimeSniffer = Annotated[MimeSniffer, Depends(get_mime_sniffer)]
_Llm = Annotated[LLMClient, Depends(get_llm)]


def _task_lifecycle_bus(session: Session, ctx: WorkspaceContext) -> EventBus:
    """Return a request-local bus with occurrence-shift hooks and global fanout."""
    local_bus = EventBus()
    register_occurrence_shift_subscription(
        local_bus,
        session_provider=lambda _event: (session, ctx),
    )

    def _forward(event: Event) -> None:
        default_event_bus.publish(event)

    for event_cls in registered_events().values():
        local_bus.subscribe(event_cls)(_forward)
    return local_bus


def _agent_attribution_from_request(
    session: Session,
    ctx: WorkspaceContext,
    request: Request,
) -> AgentAttribution:
    actor = getattr(request.state, ACTOR_STATE_ATTR, None)
    if not isinstance(actor, ActorIdentity) or actor.token_id is None:
        return AgentAttribution(
            actor_user_id=ctx.actor_id,
            token_id=None,
            agent_label=None,
        )
    if ctx.principal_kind == "session":
        return AgentAttribution(
            actor_user_id=ctx.actor_id,
            token_id=None,
            agent_label=None,
        )
    with tenant_agnostic():
        row = session.get(ApiToken, actor.token_id)
    if row is None or row.kind != "delegated":
        return AgentAttribution(
            actor_user_id=ctx.actor_id,
            token_id=None,
            agent_label=None,
        )
    return AgentAttribution(
        actor_user_id=ctx.actor_id,
        token_id=row.id,
        agent_label=row.label,
        agent_conversation_ref=request.headers.get("X-Agent-Conversation-Ref"),
    )
