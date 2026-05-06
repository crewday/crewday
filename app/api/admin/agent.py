"""Deployment-admin embedded agent endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.ops.models import AdminAgentAction as AdminAgentActionRow
from app.adapters.db.ops.models import AdminAgentChatMessage as AdminAgentMessageRow
from app.api.admin._audit import audit_admin
from app.api.admin.deps import current_deployment_admin_principal
from app.api.deps import db_session
from app.api.transport import admin_sse
from app.domain.agent.runtime import (
    DelegatedToken,
    ToolCall,
    ToolDispatcher,
    ToolResult,
)
from app.domain.errors import (
    Conflict,
    Forbidden,
    NotFound,
    ServiceUnavailable,
    Validation,
)
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
_ADMIN_DECIDED_STATES: frozenset[str] = frozenset({"rejected", "executed"})


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


class AdminAgentActionNotPending(Conflict):
    """The admin action row already has a terminal decision."""

    title = "Admin agent action no longer pending"
    type_name = "admin_agent_action_not_pending"

    def __init__(self, action_id: str, *, state: str) -> None:
        super().__init__(
            f"admin agent action {action_id!r} is in state {state!r}",
            extra={
                "error": "admin_agent_action_not_pending",
                "id": action_id,
                "state": state,
            },
        )


class AdminAgentApprovalRequiresSession(Forbidden):
    """Admin action decisions must come from a human session."""

    title = "Approval requires session credential"
    type_name = "approval_requires_session"


class AdminAgentActionReplayFailed(ServiceUnavailable):
    """The recorded admin action could not be replayed successfully."""

    title = "Admin agent action replay failed"
    type_name = "admin_agent_action_replay_failed"


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
        # justification: deployment-admin action rows are not workspace-scoped.
        with tenant_agnostic():
            rows = session.scalars(
                select(AdminAgentActionRow)
                .where(
                    AdminAgentActionRow.for_user_id == ctx.user_id,
                    AdminAgentActionRow.state == "pending",
                    AdminAgentActionRow.inline_channel == _ADMIN_INLINE_CHANNEL,
                )
                .order_by(
                    AdminAgentActionRow.requested_at.asc(),
                    AdminAgentActionRow.id.asc(),
                )
            ).all()
        return [_action_payload(row) for row in rows]

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
        )

    return router


def _decide_action(
    action_id: str,
    *,
    request: Request,
    ctx: DeploymentContext,
    session: Session,
    status_value: Literal["approved", "rejected"],
) -> AdminAgentDecisionResponse:
    _require_human_decider(ctx, action_id=action_id)
    # justification: deployment-admin action rows are not workspace-scoped.
    with tenant_agnostic():
        row = session.get(AdminAgentActionRow, action_id)
        if row is None or row.for_user_id != ctx.user_id:
            raise NotFound(
                extra={"error": "admin_agent_action_not_found", "id": action_id}
            )
        if status_value == "approved":
            _approve_action_row(row, request=request, ctx=ctx, session=session)
        else:
            _deny_action_row(row, request=request, ctx=ctx, session=session)
    return AdminAgentDecisionResponse(ok=True)


def _require_human_decider(ctx: DeploymentContext, *, action_id: str) -> None:
    if ctx.actor_kind == "user":
        return
    raise AdminAgentApprovalRequiresSession(
        "admin action decisions must be submitted under a passkey session",
        extra={
            "error": "approval_requires_session",
            "id": action_id,
            "credential_kind": ctx.actor_kind,
            "token_id": ctx.principal,
        },
    )


def _approve_action_row(
    row: AdminAgentActionRow,
    *,
    request: Request,
    ctx: DeploymentContext,
    session: Session,
) -> None:
    if row.state == "executed":
        return
    if row.state in _ADMIN_DECIDED_STATES:
        raise AdminAgentActionNotPending(row.id, state=row.state)

    dispatcher = _get_tool_dispatcher(request)
    tool_call = _tool_call_from_row(row)
    result = dispatcher.dispatch(
        tool_call,
        token=_replay_token(),
        headers=_replay_headers(ctx, row),
    )
    if result.status_code >= 400:
        raise AdminAgentActionReplayFailed(
            "admin action replay failed",
            extra={
                "error": "admin_agent_action_replay_failed",
                "id": row.id,
                "result_status_code": result.status_code,
                "result_body": result.body,
            },
        )
    decided_at = _now_utc()
    row.state = "executed"
    row.decided_at = decided_at
    row.decided_by_user_id = ctx.user_id
    row.executed_at = decided_at
    row.result_json = _result_to_json(result)
    audit_admin(
        session,
        ctx=ctx,
        request=request,
        entity_kind="admin_agent_action",
        entity_id=row.id,
        action="admin_agent.action.approved",
        diff={
            "decision": "approved",
            "action": row.action,
            "for_user_id": row.for_user_id,
            "requested_by_token_id": row.requested_by_token_id,
            "idempotency_key": row.idempotency_key,
            "result_status_code": result.status_code,
            "result_mutated": result.mutated,
            "page": _page_context(request, row),
        },
    )


def _deny_action_row(
    row: AdminAgentActionRow,
    *,
    request: Request,
    ctx: DeploymentContext,
    session: Session,
) -> None:
    if row.state in _ADMIN_DECIDED_STATES:
        raise AdminAgentActionNotPending(row.id, state=row.state)
    row.state = "rejected"
    row.decided_at = _now_utc()
    row.decided_by_user_id = ctx.user_id
    audit_admin(
        session,
        ctx=ctx,
        request=request,
        entity_kind="admin_agent_action",
        entity_id=row.id,
        action="admin_agent.action.denied",
        diff={
            "decision": "denied",
            "action": row.action,
            "for_user_id": row.for_user_id,
            "requested_by_token_id": row.requested_by_token_id,
            "idempotency_key": row.idempotency_key,
            "page": _page_context(request, row),
        },
    )


def _get_tool_dispatcher(request: Request) -> ToolDispatcher:
    dispatcher: ToolDispatcher | None = getattr(
        request.app.state, "tool_dispatcher", None
    )
    if dispatcher is None:
        message = "admin action replay requires a configured ToolDispatcher"
        raise ServiceUnavailable(
            message,
            extra={"error": "dispatcher_not_configured", "message": message},
        )
    return dispatcher


def _tool_call_from_row(row: AdminAgentActionRow) -> ToolCall:
    payload = row.resolved_payload_json
    if not isinstance(payload, dict):
        raise Validation(
            "admin action payload must be a JSON object",
            extra={"error": "admin_agent_action_payload_invalid", "id": row.id},
        )
    call_id = row.idempotency_key
    tool_input: dict[str, object]
    if isinstance(payload.get("tool_input"), dict):
        tool_input = dict(payload["tool_input"])
        raw_call_id = payload.get("tool_call_id")
        if isinstance(raw_call_id, str):
            call_id = raw_call_id
    else:
        tool_input = dict(payload)
    return ToolCall(id=call_id, name=row.action, input=tool_input)


def _replay_token() -> DelegatedToken:
    return DelegatedToken(
        plaintext=f"mip_ADMIN_REPLAY_{new_ulid()}",
        token_id=new_ulid(),
    )


def _replay_headers(
    ctx: DeploymentContext,
    row: AdminAgentActionRow,
) -> dict[str, str]:
    return {
        "Idempotency-Key": row.idempotency_key,
        "X-Agent-Channel": _ADMIN_AGENT_CHANNEL,
        "X-Agent-Page": row.page_context,
        "X-Crewday-Replay": "1",
        "X-Crewday-Replay-Actor-Id": ctx.user_id,
    }


def _result_to_json(result: ToolResult) -> dict[str, Any]:
    return {
        "status_code": result.status_code,
        "mutated": result.mutated,
        "body": result.body,
    }


def _page_context(request: Request, row: AdminAgentActionRow) -> str:
    page = request.headers.get("X-Agent-Page")
    if page is not None:
        return page
    return row.page_context


def _action_payload(row: AdminAgentActionRow) -> AdminAgentAction:
    card_fields: list[tuple[str, str]] = []
    raw_fields = row.card_fields_json
    if isinstance(raw_fields, dict):
        card_fields = [(str(key), str(value)) for key, value in raw_fields.items()]
    elif isinstance(raw_fields, list):
        for item in raw_fields:
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and item[0] is not None
                and item[1] is not None
            ):
                card_fields.append((str(item[0]), str(item[1])))
    return AdminAgentAction(
        id=row.id,
        title=row.card_summary,
        detail=row.card_summary,
        risk=_risk(row.card_risk),
        card_summary=row.card_summary,
        card_fields=card_fields,
        gate_source=_gate_source(row.gate_source),
        inline_channel=_ADMIN_INLINE_CHANNEL,
    )


def _risk(value: str) -> Literal["low", "medium", "high"]:
    if value == "medium":
        return "medium"
    if value == "high":
        return "high"
    return "low"


def _gate_source(
    value: str,
) -> Literal[
    "workspace_always",
    "workspace_configurable",
    "user_auto_annotation",
    "user_strict_mutation",
]:
    if value == "workspace_configurable":
        return "workspace_configurable"
    if value == "user_auto_annotation":
        return "user_auto_annotation"
    if value == "user_strict_mutation":
        return "user_strict_mutation"
    return "workspace_always"


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
