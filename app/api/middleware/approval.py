"""Agent approval gate for delegated-token REST mutations."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.adapters.db.identity.models import User
from app.adapters.db.llm.models import ApprovalRequest
from app.adapters.db.session import make_uow
from app.domain.agent.runtime import (
    DelegatedToken,
    GateDecision,
    ToolCall,
    ToolDispatcher,
    ToolResult,
)
from app.domain.tasks.completion import (
    InvalidStateTransition,
    PermissionDenied,
    TaskNotFound,
)
from app.domain.tasks.completion import cancel as cancel_task
from app.tenancy import ActorGrantRole, WorkspaceContext, tenant_agnostic
from app.tenancy.current import get_current
from app.tenancy.middleware import ACTOR_STATE_ATTR, ActorIdentity
from app.util.clock import SystemClock
from app.util.ulid import new_ulid

__all__ = ["AgentApprovalMiddleware", "InProcessApprovalDispatcher"]


_MUTATING_METHODS: Final[frozenset[str]] = frozenset(
    {"POST", "PUT", "PATCH", "DELETE"}
)
_APPROVAL_PATH_MARKER: Final[str] = "/api/v1/approvals"
_REPLAY_HEADER: Final[str] = "X-Crewday-Replay"


@dataclass(frozen=True, slots=True)
class _ApprovalAction:
    tool_name: str
    tool_input: dict[str, object]
    card_summary: str
    card_risk: str


@dataclass(frozen=True, slots=True)
class _ApprovalTarget:
    task_id: str


class AgentApprovalMiddleware(BaseHTTPMiddleware):
    """Queue delegated-token writes when the delegating user is strict."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not _should_consider(request):
            return await call_next(request)
        actor = getattr(request.state, ACTOR_STATE_ATTR, None)
        ctx = get_current()
        if not isinstance(actor, ActorIdentity) or ctx is None:
            return await call_next(request)
        if actor.principal_kind != "token" or actor.token_kind != "delegated":
            return await call_next(request)

        target = _approval_target_for(request, ctx=ctx)
        if target is None:
            return await call_next(request)

        if _approval_mode(ctx.actor_id) != "strict":
            return await call_next(request)

        action = await _approval_action_for(request, ctx=ctx, target=target)
        if action is None:
            return _invalid_approval_input_response()

        approval_id, expires_at = _write_pending_approval(
            ctx,
            actor=actor,
            action=action,
        )
        return _approval_required_response(approval_id, expires_at)


def _should_consider(request: Request) -> bool:
    if request.method.upper() not in _MUTATING_METHODS:
        return False
    if request.headers.get(_REPLAY_HEADER) == "1":
        return False
    return _APPROVAL_PATH_MARKER not in request.url.path


def _approval_target_for(
    request: Request, *, ctx: WorkspaceContext
) -> _ApprovalTarget | None:
    path = request.url.path
    prefix = f"/w/{ctx.workspace_slug}/api/v1/tasks/tasks/"
    if not path.startswith(prefix) or not path.endswith("/cancel"):
        return None
    task_id = path.removeprefix(prefix).removesuffix("/cancel").strip("/")
    if not task_id or "/" in task_id:
        return None
    return _ApprovalTarget(task_id=task_id)


async def _approval_action_for(
    request: Request, *, ctx: WorkspaceContext, target: _ApprovalTarget
) -> _ApprovalAction | None:
    body = await _json_body(request)
    if body is None:
        return None
    reason = body.get("reason_md")
    if not isinstance(reason, str) or not reason:
        return None
    return _ApprovalAction(
        tool_name="cancel_task",
        tool_input={
            "workspace_slug": ctx.workspace_slug,
            "task_id": target.task_id,
            "reason_md": reason,
        },
        card_summary="Cancel task?",
        card_risk="medium",
    )


async def _json_body(request: Request) -> dict[str, object] | None:
    raw = await request.body()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _approval_mode(actor_id: str) -> str:
    with make_uow() as session:
        user = session.get(User, actor_id)
        if user is None:
            return "auto"
        mode = user.agent_approval_mode
        return mode if mode in {"bypass", "auto", "strict"} else "auto"


def _write_pending_approval(
    ctx: WorkspaceContext,
    *,
    actor: ActorIdentity,
    action: _ApprovalAction,
) -> tuple[str, datetime]:
    clock = SystemClock()
    created_at = clock.now()
    expires_at = created_at + timedelta(days=7)
    approval_id = new_ulid(clock=clock)
    with make_uow() as session:
        row = ApprovalRequest(
            id=approval_id,
            workspace_id=ctx.workspace_id,
            requester_actor_id=ctx.actor_id,
            action_json={
                "tool_name": action.tool_name,
                "tool_call_id": new_ulid(clock=clock),
                "tool_input": action.tool_input,
                "card_summary": action.card_summary,
                "card_risk": action.card_risk,
                "pre_approval_source": "user_strict_mutation",
                "requested_by_token_id": actor.token_id,
            },
            status="pending",
            decided_by=None,
            decided_at=None,
            rationale_md=None,
            decision_note_md=None,
            result_json=None,
            expires_at=expires_at,
            inline_channel="desk_only",
            for_user_id=ctx.actor_id,
            resolved_user_mode="strict",
            created_at=created_at,
        )
        # justification: the row is being inserted with the current
        # workspace_id; the tenant filter only protects reads.
        with tenant_agnostic():
            session.add(row)
    return approval_id, expires_at


def _approval_required_response(approval_id: str, expires_at: datetime) -> Response:
    return JSONResponse(
        status_code=409,
        media_type="application/problem+json",
        content={
            "type": "https://crewday.dev/errors/approval_required",
            "title": "Approval required",
            "detail": "This delegated-token action is pending human approval.",
            "approval_id": approval_id,
            "status": "pending",
            "expires_at": expires_at.isoformat(),
        },
    )


def _invalid_approval_input_response() -> Response:
    return JSONResponse(
        status_code=422,
        media_type="application/problem+json",
        content={
            "type": "https://crewday.dev/errors/validation",
            "title": "Validation error",
            "detail": "reason_md is required before this action can be queued.",
        },
    )


class InProcessApprovalDispatcher(ToolDispatcher):
    """Replay approval rows through domain services for supported tools."""

    def is_gated(self, call: ToolCall) -> GateDecision:
        del call
        return GateDecision(gated=False)

    def dispatch(
        self,
        call: ToolCall,
        *,
        token: DelegatedToken,
        headers: Mapping[str, str],
    ) -> ToolResult:
        del token
        if call.name != "cancel_task":
            return ToolResult(
                call_id=call.id,
                status_code=404,
                body={"error": "unsupported_tool", "tool": call.name},
                mutated=False,
            )
        actor_id = headers.get("X-Crewday-Replay-Actor-Id")
        if not actor_id:
            return ToolResult(
                call_id=call.id,
                status_code=422,
                body={"error": "missing_replay_actor"},
                mutated=False,
            )
        return _dispatch_cancel_task(
            call,
            actor_id=actor_id,
            actor_grant_role=_actor_grant_role_from_header(
                headers.get("X-Crewday-Replay-Actor-Role")
            ),
            actor_was_owner_member=headers.get("X-Crewday-Replay-Actor-Is-Owner")
            == "1",
        )


def _dispatch_cancel_task(
    call: ToolCall,
    *,
    actor_id: str,
    actor_grant_role: ActorGrantRole,
    actor_was_owner_member: bool,
) -> ToolResult:
    task_id = _input_str(call.input, "task_id")
    workspace_slug = _input_str(call.input, "workspace_slug")
    reason = _input_str(call.input, "reason_md")
    if task_id is None or workspace_slug is None or reason is None:
        return ToolResult(
            call_id=call.id,
            status_code=422,
            body={"error": "invalid_cancel_task_input"},
            mutated=False,
        )
    with make_uow() as session:
        if not isinstance(session, Session):
            return ToolResult(
                call_id=call.id,
                status_code=500,
                body={"error": "unsupported_session"},
                mutated=False,
            )
        workspace_id = _workspace_id_for_slug(session, workspace_slug)
        if workspace_id is None:
            return ToolResult(
                call_id=call.id,
                status_code=404,
                body={"error": "workspace_not_found"},
                mutated=False,
            )
        ctx = WorkspaceContext(
            workspace_id=workspace_id,
            workspace_slug=workspace_slug,
            actor_id=actor_id,
            actor_kind="user",
            actor_grant_role=actor_grant_role,
            actor_was_owner_member=actor_was_owner_member,
            audit_correlation_id=call.id,
            principal_kind="session",
        )
        try:
            view = cancel_task(session, ctx, task_id, reason=reason)
        except TaskNotFound:
            return ToolResult(
                call_id=call.id,
                status_code=404,
                body={"error": "task_not_found"},
                mutated=False,
            )
        except PermissionDenied:
            return ToolResult(
                call_id=call.id,
                status_code=403,
                body={"error": "task_cancel_forbidden"},
                mutated=False,
            )
        except InvalidStateTransition:
            return ToolResult(
                call_id=call.id,
                status_code=409,
                body={"error": "invalid_task_state"},
                mutated=False,
            )
    return ToolResult(
        call_id=call.id,
        status_code=200,
        body={
            "task_id": view.task_id,
            "state": view.state,
            "reason": view.reason,
        },
        mutated=True,
    )


def _workspace_id_for_slug(session: Session, slug: str) -> str | None:
    from app.adapters.db.workspace.models import Workspace

    # justification: replay starts from a stored workspace_slug before
    # a WorkspaceContext exists for the domain call.
    with tenant_agnostic():
        row = session.scalar(select(Workspace).where(Workspace.slug == slug))
    return row.id if row is not None else None


def _input_str(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _actor_grant_role_from_header(value: str | None) -> ActorGrantRole:
    if value == "manager":
        return "manager"
    if value == "worker":
        return "worker"
    if value == "client":
        return "client"
    return "guest"
