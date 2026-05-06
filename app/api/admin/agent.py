"""Deployment-admin embedded agent endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.ops.models import AdminAgentChatMessage as AdminAgentMessageRow
from app.api.admin._audit import audit_admin
from app.api.admin.deps import current_deployment_admin_principal
from app.api.deps import db_session
from app.api.transport import admin_sse
from app.domain.errors import NotFound
from app.tenancy import DeploymentContext, tenant_agnostic
from app.util.ulid import new_ulid

__all__ = [
    "AdminAgentAction",
    "AdminAgentDecisionResponse",
    "AdminAgentMessage",
    "AdminAgentMessageRequest",
    "build_admin_agent_router",
]


_Db = Annotated[Session, Depends(db_session)]
_Ctx = Annotated[DeploymentContext, Depends(current_deployment_admin_principal)]

_ADMIN_INLINE_CHANNEL: Literal["web_admin_sidebar"] = "web_admin_sidebar"
_ADMIN_AGENT_CAPABILITY: Literal["chat.admin"] = "chat.admin"
_ADMIN_AGENT_CHANNEL: Literal["web_admin_sidebar"] = "web_admin_sidebar"


class AdminAgentMessageRequest(BaseModel):
    """Request body for ``POST /admin/api/v1/agent/message``."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=8_000)

    @field_validator("body")
    @classmethod
    def _body_must_have_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("body must contain text")
        return stripped


class AdminAgentMessage(BaseModel):
    """Frontend-compatible admin agent chat message."""

    at: datetime
    kind: Literal["agent", "user", "action"]
    body: str
    channel_kind: None = None


class AdminAgentAction(BaseModel):
    """Pending admin action card consumed by ``AgentSidebar``."""

    id: str
    title: str
    detail: str
    risk: Literal["low", "medium", "high"]
    card_summary: str
    card_fields: list[tuple[str, str]]
    gate_source: Literal[
        "workspace_always",
        "workspace_configurable",
        "user_auto_annotation",
        "user_strict_mutation",
    ]
    inline_channel: Literal["web_admin_sidebar"] = _ADMIN_INLINE_CHANNEL


class AdminAgentDecisionResponse(BaseModel):
    """Mutation response shape used by ``AgentSidebar``."""

    ok: Literal[True]


def build_admin_agent_router() -> APIRouter:
    """Return the deployment-admin agent sidebar router."""

    router = APIRouter(prefix="/agent", tags=["admin-agent"])

    @router.get(
        "/log",
        response_model=list[AdminAgentMessage],
        operation_id="admin.agent.log.list",
    )
    def get_log(ctx: _Ctx, session: _Db) -> list[AdminAgentMessage]:
        # justification: deployment-admin transcript rows are not workspace-scoped.
        with tenant_agnostic():
            rows = session.scalars(
                select(AdminAgentMessageRow)
                .where(AdminAgentMessageRow.admin_user_id == ctx.user_id)
                .order_by(
                    AdminAgentMessageRow.created_at.asc(),
                    AdminAgentMessageRow.id.asc(),
                )
            ).all()
        return [_message_payload(row) for row in rows]

    @router.post(
        "/message",
        response_model=AdminAgentMessage,
        status_code=status.HTTP_201_CREATED,
        operation_id="admin.agent.message.create",
    )
    def post_message(
        request: Request,
        ctx: _Ctx,
        session: _Db,
        body: AdminAgentMessageRequest,
    ) -> AdminAgentMessage:
        page = request.headers.get("X-Agent-Page", "")
        created_at = _now_utc()
        row = AdminAgentMessageRow(
            id=new_ulid(),
            admin_user_id=ctx.user_id,
            kind="user",
            body_md=body.body,
            page_context=page,
            author_label="user",
            created_at=created_at,
        )
        # justification: deployment-admin transcript rows are not workspace-scoped.
        with tenant_agnostic():
            session.add(row)
            session.flush()
        audit_admin(
            session,
            ctx=ctx,
            request=request,
            entity_kind="admin_agent_message",
            entity_id=row.id,
            action="admin_agent.message.sent",
            diff={
                "capability": _ADMIN_AGENT_CAPABILITY,
                "inline_channel": _ADMIN_AGENT_CHANNEL,
                "page": page,
                "principal": ctx.principal,
            },
        )
        payload = _message_payload(row)
        _publish_admin_turn_started(request, ctx, row.created_at)
        _publish_admin_message(request, ctx, payload)
        _publish_admin_turn_finished(request, ctx, row.created_at)
        return payload

    @router.get(
        "/actions",
        response_model=list[AdminAgentAction],
        operation_id="admin.agent.actions.list",
    )
    def get_actions(ctx: _Ctx, session: _Db) -> list[AdminAgentAction]:
        # There is not yet a deployment-scoped executable action model.
        # Returning no cards is the safe state: the UI cannot approve an
        # action that the server cannot replay.
        _ = (ctx, session)
        return []

    @router.post(
        "/action/{action_id}/approve",
        response_model=AdminAgentDecisionResponse,
        operation_id="admin.agent.action.approve",
    )
    def approve_action(
        action_id: str,
        request: Request,
        ctx: _Ctx,
        session: _Db,
    ) -> AdminAgentDecisionResponse:
        return _decide_action(
            action_id,
            request=request,
            ctx=ctx,
            session=session,
            status_value="approved",
            audit_action="admin_agent.action.approved",
        )

    @router.post(
        "/action/{action_id}/deny",
        response_model=AdminAgentDecisionResponse,
        operation_id="admin.agent.action.deny",
    )
    def deny_action(
        action_id: str,
        request: Request,
        ctx: _Ctx,
        session: _Db,
    ) -> AdminAgentDecisionResponse:
        return _decide_action(
            action_id,
            request=request,
            ctx=ctx,
            session=session,
            status_value="rejected",
            audit_action="admin_agent.action.denied",
        )

    return router


def _decide_action(
    action_id: str,
    *,
    request: Request,
    ctx: DeploymentContext,
    session: Session,
    status_value: Literal["approved", "rejected"],
    audit_action: str,
) -> AdminAgentDecisionResponse:
    _ = (request, ctx, session, status_value, audit_action)
    raise NotFound(extra={"error": "admin_agent_action_not_found", "id": action_id})


def _message_payload(row: AdminAgentMessageRow) -> AdminAgentMessage:
    kind: Literal["agent", "user", "action"]
    if row.kind == "agent":
        kind = "agent"
    elif row.kind == "action":
        kind = "action"
    else:
        kind = "user"
    return AdminAgentMessage(
        at=_as_utc(row.created_at),
        kind=kind,
        body=row.body_md,
        channel_kind=None,
    )


def _publish_admin_message(
    request: Request,
    ctx: DeploymentContext,
    message: AdminAgentMessage,
) -> None:
    admin_sse.publish_admin_event(
        kind="agent.message.appended",
        ctx=ctx,
        request=request,
        user_scope=ctx.user_id,
        payload={
            "scope": "admin",
            "message": message.model_dump(mode="json"),
        },
    )


def _publish_admin_turn_started(
    request: Request,
    ctx: DeploymentContext,
    started_at: datetime,
) -> None:
    admin_sse.publish_admin_event(
        kind="agent.turn.started",
        ctx=ctx,
        request=request,
        user_scope=ctx.user_id,
        payload={"scope": "admin", "started_at": _as_utc(started_at).isoformat()},
    )


def _publish_admin_turn_finished(
    request: Request,
    ctx: DeploymentContext,
    started_at: datetime,
) -> None:
    finished_at = _now_utc()
    _ = started_at
    admin_sse.publish_admin_event(
        kind="agent.turn.finished",
        ctx=ctx,
        request=request,
        user_scope=ctx.user_id,
        payload={
            "scope": "admin",
            "finished_at": finished_at.isoformat(),
            "outcome": "error",
            "error": "admin_agent_runtime_unwired",
        },
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _now_utc() -> datetime:
    return datetime.now(UTC)
