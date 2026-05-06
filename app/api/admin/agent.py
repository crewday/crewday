"""Deployment-admin embedded agent endpoints.

The admin shell mounts the shared web ``AgentSidebar`` with
``role="admin"``. This router provides the deployment-scoped HTTP
surface listed in specs §11/§12 so the shell has real authenticated
endpoints even before the full deployment-agent runtime grows a
deployment chat transcript store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.api.admin._audit import audit_admin
from app.api.admin.deps import current_deployment_admin_principal
from app.api.deps import db_session
from app.domain.errors import NotFound
from app.tenancy import DeploymentContext

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
    def get_log(ctx: _Ctx) -> list[AdminAgentMessage]:
        _ = ctx
        return []

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
        audit_admin(
            session,
            ctx=ctx,
            request=request,
            entity_kind="admin_agent_message",
            entity_id=ctx.user_id,
            action="admin_agent.message.sent",
            diff={"page": page},
        )
        return AdminAgentMessage(
            at=_now_utc(),
            kind="user",
            body=body.body,
            channel_kind=None,
        )

    @router.get(
        "/actions",
        response_model=list[AdminAgentAction],
        operation_id="admin.agent.actions.list",
    )
    def get_actions(ctx: _Ctx, session: _Db) -> list[AdminAgentAction]:
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


def _now_utc() -> datetime:
    return datetime.now(UTC)
