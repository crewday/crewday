"""Deployment-admin embedded agent endpoints."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.ops.models import AdminAgentAction as AdminAgentActionRow
from app.adapters.db.ops.models import AdminAgentChatMessage as AdminAgentMessageRow
from app.api.admin._audit import audit_admin
from app.api.admin.deps import current_deployment_admin_principal
from app.api.admin.settings import preview_deployment_setting_for_agent
from app.api.deps import db_session
from app.api.transport import admin_sse
from app.api.transport.correlation_id import request_correlation_id
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
    "AdminAgentActionProducer",
    "AdminAgentActionProposal",
    "AdminAgentDecisionResponse",
    "AdminAgentMessage",
    "AdminAgentMessageRequest",
    "AdminAgentTextReply",
    "build_admin_agent_router",
]


_Db = Annotated[Session, Depends(db_session)]
_Ctx = Annotated[DeploymentContext, Depends(current_deployment_admin_principal)]

_ADMIN_INLINE_CHANNEL: Literal["web_admin_sidebar"] = "web_admin_sidebar"
_ADMIN_AGENT_CAPABILITY: Literal["chat.admin"] = "chat.admin"
_ADMIN_AGENT_CHANNEL: Literal["web_admin_sidebar"] = "web_admin_sidebar"
_ADMIN_DECIDED_STATES: frozenset[str] = frozenset({"rejected", "executed"})
_ADMIN_FALLBACK_ACTION_ERRORS: frozenset[str] = frozenset(
    {
        "admin_agent_model_unavailable",
        "admin_agent_no_action_proposal",
        "admin_agent_runtime_unwired",
    }
)
_ADMIN_RUNTIME_FALLBACK_REPLY = (
    "The admin agent cannot propose an action right now because its chat runtime "
    "is not configured or did not return a supported action. Your message was "
    "recorded, and no admin action was approved or executed."
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AdminAgentActionProposal:
    """Resolved gated deployment mutation produced by the admin agent."""

    tool_call: ToolCall
    card_summary: str
    card_fields: Sequence[tuple[str, str]]
    card_risk: Literal["low", "medium", "high"]
    gate_source: Literal[
        "workspace_always",
        "workspace_configurable",
        "user_auto_annotation",
        "user_strict_mutation",
    ]
    requested_by_token_id: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class AdminAgentTextReply:
    """Plain-text deployment-admin chat reply produced by the agent."""

    body: str


class AdminAgentActionProducer(Protocol):
    """Configured runtime seam for live admin-agent action production."""

    def produce_action(
        self,
        *,
        message: str,
        page_context: str,
        ctx: DeploymentContext,
        session: Session,
    ) -> AdminAgentActionProposal | AdminAgentTextReply | None:
        """Resolve ``message`` into an admin reply or gated action proposal."""
        ...


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
        # code-health: ignore[nloc] Router preserves transcript, audit, SSE order.
        page = request.headers.get("X-Agent-Page", "")
        created_at = _now_utc()
        _publish_admin_turn_started(request, ctx, created_at)
        try:
            producer = _get_action_producer(request)
        except ServiceUnavailable as exc:
            fallback = _record_service_unavailable_fallback(
                request,
                ctx=ctx,
                session=session,
                body=body.body,
                page_context=page,
                created_at=created_at,
                exc=exc,
            )
            if fallback is None:
                raise
            return fallback
        try:
            result = _validated_producer_result(
                producer.produce_action(
                    message=body.body,
                    page_context=page,
                    ctx=ctx,
                    session=session,
                )
            )
        except ServiceUnavailable as exc:
            fallback = _record_service_unavailable_fallback(
                request,
                ctx=ctx,
                session=session,
                body=body.body,
                page_context=page,
                created_at=created_at,
                exc=exc,
            )
            if fallback is None:
                raise
            return fallback
        except Exception as exc:
            error = "admin_agent_runtime_error"
            _log_admin_agent_failure(
                request,
                ctx=ctx,
                event="admin_agent.runtime.unexpected_failure",
                error=error,
                page_context=page,
                exception_type=type(exc).__name__,
            )
            return _record_admin_fallback_turn(
                request,
                ctx=ctx,
                session=session,
                body=body.body,
                page_context=page,
                error=error,
                created_at=created_at,
            )
        row = _record_admin_user_message(
            request,
            ctx=ctx,
            session=session,
            body=body.body,
            page_context=page,
            created_at=created_at,
        )
        payload = _message_payload(row)
        if isinstance(result, AdminAgentTextReply):
            _record_admin_text_reply(
                request,
                ctx=ctx,
                session=session,
                page_context=page,
                user_message=row,
                reply=result,
            )
            _publish_admin_turn_finished(
                request,
                ctx,
                row.created_at,
                outcome="replied",
            )
            return payload
        try:
            action = _produce_admin_action(
                result,
                request,
                ctx=ctx,
                session=session,
                message=body.body,
                page_context=page,
                requested_at=row.created_at,
            )
        except ServiceUnavailable as exc:
            error = str(exc.extra.get("error", "admin_agent_runtime_unwired"))
            _log_admin_agent_failure(
                request,
                ctx=ctx,
                event="admin_agent.runtime.action_failed",
                error=error,
                page_context=page,
            )
            _record_admin_runtime_fallback(
                request,
                ctx=ctx,
                session=session,
                page_context=page,
                user_message=row,
                error=error,
            )
            return payload
        _publish_admin_action_pending(request, ctx, action)
        _publish_admin_turn_finished(request, ctx, row.created_at, outcome="action")
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
        headers=_replay_headers(request, ctx, row),
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


def _get_action_producer(request: Request) -> AdminAgentActionProducer:
    producer: AdminAgentActionProducer | None = getattr(
        request.app.state, "admin_agent_action_producer", None
    )
    if producer is None:
        raise _admin_agent_unavailable("admin_agent_runtime_unwired")
    return producer


def _admin_agent_unavailable(error: str) -> ServiceUnavailable:
    message = "admin agent action production requires a configured runtime"
    return ServiceUnavailable(message, extra={"error": error, "message": message})


def _is_admin_fallback_error(error: str) -> bool:
    return error in _ADMIN_FALLBACK_ACTION_ERRORS


def _record_service_unavailable_fallback(
    request: Request,
    *,
    ctx: DeploymentContext,
    session: Session,
    body: str,
    page_context: str,
    created_at: datetime,
    exc: ServiceUnavailable,
) -> AdminAgentMessage | None:
    # code-health: ignore[params] Fallback helper mirrors route context explicitly.
    error = str(exc.extra.get("error", "admin_agent_runtime_unwired"))
    if not _is_admin_fallback_error(error):
        _publish_admin_turn_finished(
            request,
            ctx,
            created_at,
            outcome="error",
            error=error,
        )
        return None
    return _record_admin_fallback_turn(
        request,
        ctx=ctx,
        session=session,
        body=body,
        page_context=page_context,
        error=error,
        created_at=created_at,
    )


def _validated_action_proposal(
    proposal: AdminAgentActionProposal | None,
) -> AdminAgentActionProposal:
    if proposal is None:
        raise _admin_agent_unavailable("admin_agent_no_action_proposal")
    tool_call = proposal.tool_call
    if not tool_call.id.strip() or not tool_call.name.strip():
        raise _admin_agent_unavailable("admin_agent_action_proposal_invalid")
    if not proposal.card_summary.strip():
        raise _admin_agent_unavailable("admin_agent_action_proposal_invalid")
    if proposal.card_risk not in {"low", "medium", "high"}:
        raise _admin_agent_unavailable("admin_agent_action_proposal_invalid")
    if proposal.gate_source not in {
        "workspace_always",
        "workspace_configurable",
        "user_auto_annotation",
        "user_strict_mutation",
    }:
        raise _admin_agent_unavailable("admin_agent_action_proposal_invalid")
    if proposal.idempotency_key is not None and not proposal.idempotency_key.strip():
        raise _admin_agent_unavailable("admin_agent_action_proposal_invalid")
    if not _proposal_card_fields_valid(proposal):
        raise _admin_agent_unavailable("admin_agent_action_proposal_invalid")
    proposal = _validated_settings_update_proposal(proposal)
    if not _proposal_jsonable(proposal):
        raise _admin_agent_unavailable("admin_agent_action_proposal_invalid")
    return proposal


def _validated_producer_result(
    result: AdminAgentActionProposal | AdminAgentTextReply | None,
) -> AdminAgentActionProposal | AdminAgentTextReply:
    if isinstance(result, AdminAgentTextReply):
        body = result.body.strip()
        if not body:
            raise _admin_agent_unavailable("admin_agent_no_action_proposal")
        return replace(result, body=body)
    return _validated_action_proposal(result)


def _validated_settings_update_proposal(
    proposal: AdminAgentActionProposal,
) -> AdminAgentActionProposal:
    tool_call = proposal.tool_call
    if tool_call.name != "admin.settings.update":
        return proposal
    key = tool_call.input.get("key")
    if not isinstance(key, str) or "value" not in tool_call.input:
        raise _admin_agent_unavailable("admin_agent_action_proposal_invalid")
    preview = preview_deployment_setting_for_agent(
        key=key,
        raw_value=tool_call.input["value"],
    )
    if preview is None:
        raise _admin_agent_unavailable("admin_agent_action_proposal_invalid")
    return replace(
        proposal,
        tool_call=ToolCall(
            id=tool_call.id,
            name=tool_call.name,
            input={"key": preview.key, "value": preview.value},
        ),
    )


def _proposal_card_fields_valid(proposal: AdminAgentActionProposal) -> bool:
    try:
        for item in proposal.card_fields:
            if len(item) != 2:
                return False
            key, value = item
            if not isinstance(key, str) or not isinstance(value, str):
                return False
    except TypeError:
        return False
    return True


def _proposal_jsonable(proposal: AdminAgentActionProposal) -> bool:
    try:
        json.dumps(
            {
                "tool_input": dict(proposal.tool_call.input),
                "card_fields": [[key, value] for key, value in proposal.card_fields],
            },
            sort_keys=True,
        )
    except TypeError, ValueError:
        return False
    return True


def _produce_admin_action(
    proposal: AdminAgentActionProposal,
    request: Request,
    *,
    ctx: DeploymentContext,
    session: Session,
    message: str,
    page_context: str,
    requested_at: datetime,
) -> AdminAgentActionRow:
    # code-health: ignore[nloc,params] Action proposal boundary spells out audit inputs.
    idempotency_key = proposal.idempotency_key or _stable_action_idempotency_key(
        ctx=ctx,
        message=message,
        page_context=page_context,
        proposal=proposal,
    )
    existing = _get_action_by_idempotency_key(session, idempotency_key)
    if existing is not None:
        return existing

    row = AdminAgentActionRow(
        id=new_ulid(),
        requested_at=requested_at,
        requested_by_token_id=proposal.requested_by_token_id,
        for_user_id=ctx.user_id,
        action=proposal.tool_call.name,
        resolved_payload_json={
            "tool_call_id": proposal.tool_call.id,
            "tool_input": dict(proposal.tool_call.input),
        },
        idempotency_key=idempotency_key,
        state="pending",
        gate_source=proposal.gate_source,
        card_summary=proposal.card_summary,
        card_risk=proposal.card_risk,
        card_fields_json=[[key, value] for key, value in proposal.card_fields],
        inline_channel=_ADMIN_INLINE_CHANNEL,
        page_context=page_context,
    )
    with tenant_agnostic():
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            existing = _get_action_by_idempotency_key(session, idempotency_key)
            if existing is not None:
                return existing
            raise
    audit_admin(
        session,
        ctx=ctx,
        request=request,
        entity_kind="admin_agent_action",
        entity_id=row.id,
        action="admin_agent.action.proposed",
        diff={
            "action": row.action,
            "for_user_id": row.for_user_id,
            "inline_channel": row.inline_channel,
            "requested_by_token_id": row.requested_by_token_id,
            "idempotency_key": row.idempotency_key,
            "gate_source": row.gate_source,
            "risk": row.card_risk,
            "page": row.page_context,
        },
    )
    return row


def _record_admin_user_message(
    request: Request,
    *,
    ctx: DeploymentContext,
    session: Session,
    body: str,
    page_context: str,
    created_at: datetime,
) -> AdminAgentMessageRow:
    row = AdminAgentMessageRow(
        id=new_ulid(),
        admin_user_id=ctx.user_id,
        kind="user",
        body_md=body,
        page_context=page_context,
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
            "page": page_context,
            "principal": ctx.principal,
        },
    )
    _publish_admin_message(request, ctx, _message_payload(row))
    return row


def _record_admin_fallback_turn(
    request: Request,
    *,
    ctx: DeploymentContext,
    session: Session,
    body: str,
    page_context: str,
    error: str,
    created_at: datetime,
) -> AdminAgentMessage:
    # code-health: ignore[params] Fallback transcript audit inputs stay explicit.
    row = _record_admin_user_message(
        request,
        ctx=ctx,
        session=session,
        body=body,
        page_context=page_context,
        created_at=created_at,
    )
    _record_admin_runtime_fallback(
        request,
        ctx=ctx,
        session=session,
        page_context=page_context,
        user_message=row,
        error=error,
    )
    return _message_payload(row)


def _record_admin_text_reply(
    request: Request,
    *,
    ctx: DeploymentContext,
    session: Session,
    page_context: str,
    user_message: AdminAgentMessageRow,
    reply: AdminAgentTextReply,
) -> AdminAgentMessageRow:
    row = AdminAgentMessageRow(
        id=new_ulid(),
        admin_user_id=ctx.user_id,
        kind="agent",
        body_md=reply.body,
        page_context=page_context,
        author_label="agent",
        created_at=_now_utc(),
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
        action="admin_agent.message.replied",
        diff={
            "capability": _ADMIN_AGENT_CAPABILITY,
            "inline_channel": _ADMIN_AGENT_CHANNEL,
            "page": page_context,
            "principal": ctx.principal,
            "user_message_id": user_message.id,
        },
    )
    _publish_admin_message(request, ctx, _message_payload(row))
    return row


def _record_admin_runtime_fallback(
    request: Request,
    *,
    ctx: DeploymentContext,
    session: Session,
    page_context: str,
    user_message: AdminAgentMessageRow,
    error: str,
) -> AdminAgentMessageRow:
    error_id = request_correlation_id(request)
    _log_admin_agent_failure(
        request,
        ctx=ctx,
        event="admin_agent.runtime.fallback_reply",
        error=error,
        page_context=page_context,
    )
    fallback = AdminAgentMessageRow(
        id=new_ulid(),
        admin_user_id=ctx.user_id,
        kind="agent",
        body_md=_admin_runtime_fallback_reply(error_id),
        page_context=page_context,
        author_label="agent",
        created_at=_now_utc(),
    )
    # justification: deployment-admin transcript rows are not workspace-scoped.
    with tenant_agnostic():
        session.add(fallback)
        session.flush()
    audit_admin(
        session,
        ctx=ctx,
        request=request,
        entity_kind="admin_agent_message",
        entity_id=fallback.id,
        action="admin_agent.message.fallback",
        diff={
            "capability": _ADMIN_AGENT_CAPABILITY,
            "inline_channel": _ADMIN_AGENT_CHANNEL,
            "page": page_context,
            "principal": ctx.principal,
            "reason": error,
            "user_message_id": user_message.id,
        },
    )
    _publish_admin_message(request, ctx, _message_payload(fallback))
    _publish_admin_turn_finished(
        request,
        ctx,
        user_message.created_at,
        outcome="error",
        error=error,
    )
    return fallback


def _admin_runtime_fallback_reply(error_id: str) -> str:
    return f"{_ADMIN_RUNTIME_FALLBACK_REPLY}\n\nError ID: {error_id}"


def _log_admin_agent_failure(
    request: Request,
    *,
    ctx: DeploymentContext,
    event: str,
    error: str,
    page_context: str,
    exception_type: str | None = None,
) -> None:
    error_id = request_correlation_id(request)
    extra: dict[str, object] = {
        "event": event,
        "error_id": error_id,
        "turn_correlation_id": error_id,
        "actor_id": ctx.user_id,
        "scope": "admin",
        "thread_id": None,
        "agent_label": "admin-chat-agent",
        "capability": _ADMIN_AGENT_CAPABILITY,
        "error_code": error,
        "page_context_present": bool(page_context),
    }
    if exception_type is not None:
        extra["exception_type"] = exception_type
    level = logging.ERROR if exception_type is not None else logging.WARNING
    _log.log(level, event, extra=extra)


def _get_action_by_idempotency_key(
    session: Session,
    idempotency_key: str,
) -> AdminAgentActionRow | None:
    with tenant_agnostic():
        return session.scalar(
            select(AdminAgentActionRow).where(
                AdminAgentActionRow.idempotency_key == idempotency_key
            )
        )


def _stable_action_idempotency_key(
    *,
    ctx: DeploymentContext,
    message: str,
    page_context: str,
    proposal: AdminAgentActionProposal,
) -> str:
    body = {
        "admin_user_id": ctx.user_id,
        "message": message,
        "page_context": page_context,
        "tool_name": proposal.tool_call.name,
        "tool_input": dict(proposal.tool_call.input),
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return f"admin-agent:{hashlib.sha256(encoded).hexdigest()}"


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
    request: Request,
    ctx: DeploymentContext,
    row: AdminAgentActionRow,
) -> dict[str, str]:
    headers = {
        "Idempotency-Key": row.idempotency_key,
        "X-Agent-Channel": _ADMIN_AGENT_CHANNEL,
        "X-Agent-Page": row.page_context,
        "X-Crewday-Replay": "1",
        "X-Crewday-Replay-Actor-Id": ctx.user_id,
    }
    cookie = request.headers.get("cookie")
    if cookie:
        headers["Cookie"] = cookie
    user_agent = request.headers.get("user-agent")
    if user_agent:
        headers["User-Agent"] = user_agent
    accept_language = request.headers.get("accept-language")
    if accept_language:
        headers["Accept-Language"] = accept_language
    return headers


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
    *,
    outcome: Literal["action", "error", "replied"],
    error: str | None = None,
) -> None:
    finished_at = _now_utc()
    _ = started_at
    payload: dict[str, str] = {
        "scope": "admin",
        "finished_at": finished_at.isoformat(),
        "outcome": outcome,
    }
    if error is not None:
        payload["error"] = error
    admin_sse.publish_admin_event(
        kind="agent.turn.finished",
        ctx=ctx,
        request=request,
        user_scope=ctx.user_id,
        payload=payload,
    )


def _publish_admin_action_pending(
    request: Request,
    ctx: DeploymentContext,
    row: AdminAgentActionRow,
) -> None:
    admin_sse.publish_admin_event(
        kind="agent.action.pending",
        ctx=ctx,
        request=request,
        user_scope=ctx.user_id,
        payload={
            "actor_user_id": ctx.user_id,
            "approval_request_id": row.id,
            "scope": "admin",
            "thread_id": None,
        },
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _now_utc() -> datetime:
    return datetime.now(UTC)
