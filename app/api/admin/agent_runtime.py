"""Deployment-admin agent action producer and replay dispatcher."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import LlmProvider, LlmProviderModel
from app.adapters.llm.ports import (
    ChatMessage,
    LLMClient,
    LlmContentRefused,
    LlmProviderError,
    LlmRateLimited,
    LLMResponse,
    LlmTransportError,
    Tool,
)
from app.api.admin._owners import ensure_deployment_owner
from app.api.admin._usage_helpers import _QUOTA_CAP_KEY
from app.api.admin._workspace_state import (
    format_archived_at,
    load_workspace,
    set_archived_at,
    set_verification_state,
    verification_state_of,
)
from app.api.admin.agent import AdminAgentActionProducer, AdminAgentActionProposal
from app.api.admin.settings import (
    preview_deployment_setting_for_agent,
    write_deployment_setting,
)
from app.api.admin.usage import UsageCapPayload
from app.api.transport import admin_sse
from app.audit import write_deployment_audit
from app.capabilities import Capabilities
from app.config import Settings
from app.domain.agent.runtime import (
    DelegatedToken,
    GateDecision,
    ToolCall,
    ToolDispatcher,
    ToolResult,
)
from app.domain.errors import DomainError, ServiceUnavailable, Validation
from app.tenancy import DeploymentContext, tenant_agnostic
from app.util.redact import ConsentSet
from app.util.ulid import new_ulid

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = [
    "AdminAgentRuntimeActionProducer",
    "DeploymentAdminToolDispatcher",
]


_log = logging.getLogger(__name__)

_CAPABILITY: Literal["chat.admin"] = "chat.admin"
_AGENT_LABEL: Literal["admin-chat-agent"] = "admin-chat-agent"
_GATE_SOURCE: Literal["workspace_always"] = "workspace_always"

_SUPPORTED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "admin.settings.update",
        "admin.usage.workspaces.cap",
        "admin.workspaces.trust",
        "admin.workspaces.archive",
    }
)


@dataclass(frozen=True, slots=True)
class _ResolvedProposal:
    call: ToolCall
    summary: str
    fields: tuple[tuple[str, str], ...]
    risk: Literal["low", "medium", "high"]


class AdminAgentRuntimeActionProducer(AdminAgentActionProducer):
    """Resolve admin chat turns into one validated deployment action proposal."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        tools: Sequence[Tool] | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tuple(tools) if tools is not None else _admin_tools()

    def produce_action(
        self,
        *,
        message: str,
        page_context: str,
        ctx: DeploymentContext,
        session: Session,
    ) -> AdminAgentActionProposal | None:
        if not self._tools:
            raise _unavailable("dispatcher_not_configured")
        model_id = _resolve_admin_model_id(session)
        if model_id is None:
            raise _unavailable("admin_agent_model_unavailable")
        try:
            response = self._llm.chat(
                model_id=model_id,
                messages=_prompt(message=message, page_context=page_context),
                tools=self._tools,
                consents=ConsentSet.none(),
            )
        except (
            LlmContentRefused,
            LlmProviderError,
            LlmRateLimited,
            LlmTransportError,
            TimeoutError,
        ) as exc:
            _log.info(
                "admin agent model unavailable",
                extra={
                    "event": "admin_agent.model_unavailable",
                    "error_type": type(exc).__name__,
                },
            )
            raise _unavailable("admin_agent_model_unavailable") from exc
        resolved_call = _resolve_tool_call(response)
        if resolved_call is None:
            return None
        proposal = _resolve_supported_proposal(resolved_call, session=session)
        if proposal is None:
            return None
        return AdminAgentActionProposal(
            tool_call=proposal.call,
            card_summary=proposal.summary,
            card_fields=proposal.fields,
            card_risk=proposal.risk,
            gate_source=_GATE_SOURCE,
            requested_by_token_id=ctx.principal if ctx.actor_kind != "user" else None,
        )


class DeploymentAdminToolDispatcher(ToolDispatcher):
    """Replay supported deployment-admin action cards through domain code."""

    def __init__(
        self,
        fallback: ToolDispatcher | None = None,
        *,
        app: FastAPI | None = None,
    ) -> None:
        self._fallback = fallback
        self._app = app

    @property
    def tools(self) -> tuple[Tool, ...]:
        return _admin_tools()

    def is_gated(self, call: ToolCall) -> GateDecision:
        if call.name in _SUPPORTED_TOOL_NAMES:
            return GateDecision(
                gated=True,
                card_summary=f"{call.name} requires confirmation.",
                card_risk="medium",
                pre_approval_source=_GATE_SOURCE,
            )
        if self._fallback is not None:
            return self._fallback.is_gated(call)
        return GateDecision(gated=False)

    def dispatch(
        self,
        call: ToolCall,
        *,
        token: DelegatedToken,
        headers: Mapping[str, str],
    ) -> ToolResult:
        if call.name == "admin.settings.update":
            return _dispatch_settings_update(call, headers=headers, app=self._app)
        if call.name == "admin.usage.workspaces.cap":
            return _dispatch_usage_cap(call, headers=headers)
        if call.name == "admin.workspaces.trust":
            return _dispatch_workspace_trust(call, headers=headers)
        if call.name == "admin.workspaces.archive":
            return _dispatch_workspace_archive(call, headers=headers)
        if self._fallback is not None:
            return self._fallback.dispatch(call, token=token, headers=headers)
        return ToolResult(
            call_id=call.id,
            status_code=404,
            body={"error": "unsupported_tool", "tool": call.name},
            mutated=False,
        )

    def activity_label_for(self, call: ToolCall) -> str:
        if self._fallback is not None and call.name not in _SUPPORTED_TOOL_NAMES:
            return self._fallback.activity_label_for(call)
        return {
            "admin.settings.update": "Updating settings",
            "admin.usage.workspaces.cap": "Updating workspace budget",
            "admin.workspaces.trust": "Updating workspace trust",
            "admin.workspaces.archive": "Archiving workspace",
        }.get(call.name, "Working")


def _admin_tools() -> tuple[Tool, ...]:
    return (
        {
            "name": "admin.settings.update",
            "description": (
                "Propose changing one non-secret deployment setting. "
                "Secret settings are not supported through chat."
            ),
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "key": {"type": "string"},
                    "value": {},
                },
                "required": ["key", "value"],
            },
        },
        {
            "name": "admin.usage.workspaces.cap",
            "description": "Propose changing one workspace LLM budget cap.",
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "cap_cents_30d": {"type": "integer", "minimum": 0},
                },
                "required": ["id", "cap_cents_30d"],
            },
        },
        {
            "name": "admin.workspaces.trust",
            "description": "Propose promoting a workspace to trusted.",
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "admin.workspaces.archive",
            "description": "Propose soft-archiving a workspace.",
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    )


def _prompt(*, message: str, page_context: str) -> list[ChatMessage]:
    return [
        {
            "role": "system",
            "content": (
                "You are the crew.day deployment-admin sidebar agent. "
                "For supported deployment mutations, emit exactly one tool call. "
                "For reads, unsupported requests, ambiguity, or missing required "
                "fields, reply with plain text and no tool call. Never invent ids."
            ),
        },
        {
            "role": "user",
            "content": (
                "## Current admin page\n"
                f"{page_context or 'route=/admin'}\n\n"
                "## Admin message\n"
                f"{message}"
            ),
        },
    ]


def _resolve_admin_model_id(session: Session) -> str | None:
    with tenant_agnostic():
        return session.scalar(
            select(LlmProviderModel.api_model_id)
            .join(LlmProvider, LlmProvider.default_model == LlmProviderModel.id)
            .where(LlmProvider.is_enabled.is_(True))
            .order_by(LlmProvider.priority.asc(), LlmProvider.id.asc())
            .limit(1)
        )


def _resolve_tool_call(response: LLMResponse) -> ToolCall | None:
    if len(response.tool_calls) > 1:
        return None
    if response.tool_calls:
        first = response.tool_calls[0]
        return ToolCall(
            id=first.id or new_ulid(),
            name=first.name,
            input=dict(first.arguments),
        )
    return _parse_text_tool_call(response.text)


def _parse_text_tool_call(text: str) -> ToolCall | None:
    if "<tool_call" not in text:
        return None
    prefix = '<tool_call name="'
    start = text.find(prefix)
    if start < 0:
        return None
    name_start = start + len(prefix)
    name_end = text.find('"', name_start)
    input_prefix = " input='"
    input_start = text.find(input_prefix, name_end)
    if name_end < 0 or input_start < 0:
        return None
    input_start += len(input_prefix)
    input_end = text.find("'/>", input_start)
    if input_end < 0:
        return None
    try:
        payload = json.loads(text[input_start:input_end])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return ToolCall(id=new_ulid(), name=text[name_start:name_end], input=payload)


def _resolve_supported_proposal(
    call: ToolCall,
    *,
    session: Session,
) -> _ResolvedProposal | None:
    if call.name == "admin.settings.update":
        return _settings_update_proposal(call)
    if call.name == "admin.usage.workspaces.cap":
        return _usage_cap_proposal(call, session=session)
    if call.name == "admin.workspaces.trust":
        return _workspace_id_proposal(
            call,
            session=session,
            summary="Trust workspace",
            label="Workspace",
            risk="medium",
        )
    if call.name == "admin.workspaces.archive":
        return _workspace_id_proposal(
            call,
            session=session,
            summary="Archive workspace",
            label="Workspace",
            risk="high",
        )
    return None


def _settings_update_proposal(call: ToolCall) -> _ResolvedProposal | None:
    key = _input_str(call.input, "key")
    if key is None or "value" not in call.input:
        return None
    preview = preview_deployment_setting_for_agent(
        key=key,
        raw_value=call.input["value"],
    )
    if preview is None:
        return None
    return _ResolvedProposal(
        call=ToolCall(
            id=call.id,
            name=call.name,
            input={"key": preview.key, "value": preview.value},
        ),
        summary="Update deployment setting",
        fields=(
            ("Setting", preview.key),
            ("Value", _display_setting_value(preview.value)),
        ),
        risk="medium",
    )


def _usage_cap_proposal(
    call: ToolCall,
    *,
    session: Session,
) -> _ResolvedProposal | None:
    workspace_id = _input_str(call.input, "id")
    if (
        workspace_id is None
        or load_workspace(session, workspace_id=workspace_id) is None
    ):
        return None
    try:
        payload = UsageCapPayload(cap_cents_30d=call.input.get("cap_cents_30d"))
    except ValidationError:
        return None
    if payload.cap_cents_30d < 0:
        return None
    return _ResolvedProposal(
        call=ToolCall(
            id=call.id,
            name=call.name,
            input={"id": workspace_id, "cap_cents_30d": payload.cap_cents_30d},
        ),
        summary="Update workspace LLM budget cap",
        fields=(
            ("Workspace", workspace_id),
            ("Cap", str(payload.cap_cents_30d)),
        ),
        risk="high",
    )


def _workspace_id_proposal(
    call: ToolCall,
    *,
    session: Session,
    summary: str,
    label: str,
    risk: Literal["low", "medium", "high"],
) -> _ResolvedProposal | None:
    workspace_id = _input_str(call.input, "id")
    if (
        workspace_id is None
        or load_workspace(session, workspace_id=workspace_id) is None
    ):
        return None
    return _ResolvedProposal(
        call=ToolCall(id=call.id, name=call.name, input={"id": workspace_id}),
        summary=summary,
        fields=((label, workspace_id),),
        risk=risk,
    )


def _dispatch_settings_update(
    call: ToolCall,
    *,
    headers: Mapping[str, str],
    app: FastAPI | None,
) -> ToolResult:
    # code-health: ignore[nloc] Replay keeps validation, audit, and SSE inline.
    key = _input_str(call.input, "key")
    if key is None or "value" not in call.input:
        return _result(call, 422, {"error": "invalid_setting_input"})
    ctx = _ctx_from_headers(headers)
    if ctx is None:
        return _result(call, 422, {"error": "missing_replay_actor"})
    if app is None:
        return _result(call, 503, {"error": "dispatcher_not_configured"})
    settings = getattr(app.state, "settings", None)
    if not isinstance(settings, Settings):
        return _result(call, 503, {"error": "deployment_settings_unavailable"})
    capabilities = getattr(app.state, "capabilities", None)
    preview = preview_deployment_setting_for_agent(
        key=key,
        raw_value=call.input["value"],
    )
    if preview is None:
        return _result(call, 422, {"error": "invalid_setting_value"})

    from app.adapters.db.session import make_uow

    with make_uow() as session:
        if not isinstance(session, Session):
            return _result(call, 500, {"error": "unsupported_session"})
        try:
            result = write_deployment_setting(
                key=preview.key,
                raw_value=preview.value,
                ctx=ctx,
                session=session,
                settings=settings,
            )
        except Validation as exc:
            return _result(
                call,
                422,
                {"error": str(exc.extra.get("error", "invalid_setting_value"))},
            )
        except DomainError:
            return _result(call, 404, {"error": "not_found"})
        correlation_id = _replay_correlation_id(headers, call)
        _audit_replay(
            session,
            ctx=ctx,
            correlation_id=correlation_id,
            entity_kind="deployment_setting",
            entity_id=key,
            action="deployment_setting.updated",
            diff=result.audit_diff,
        )
        if isinstance(capabilities, Capabilities):
            capabilities.refresh_settings(session)
        session.flush()

    _publish_admin_replay_event(
        kind="admin.audit.appended",
        ctx=ctx,
        correlation_id=correlation_id,
        payload={
            "entity_kind": "deployment_setting",
            "entity_id": key,
            "action": "deployment_setting.updated",
        },
    )
    _publish_admin_replay_event(
        kind="admin.settings.updated",
        ctx=ctx,
        correlation_id=correlation_id,
        payload={"key": key},
    )
    return _result(
        call,
        200,
        result.response.model_dump(mode="json"),
        mutated=True,
    )


def _dispatch_usage_cap(
    call: ToolCall,
    *,
    headers: Mapping[str, str],
) -> ToolResult:
    workspace_id = _input_str(call.input, "id")
    if workspace_id is None:
        return _result(call, 422, {"error": "invalid_cap_input"})
    try:
        payload = UsageCapPayload(cap_cents_30d=call.input.get("cap_cents_30d"))
    except ValidationError:
        return _result(call, 422, {"error": "invalid_cap"})
    if payload.cap_cents_30d < 0:
        return _result(call, 422, {"error": "invalid_cap"})
    ctx = _ctx_from_headers(headers)
    if ctx is None:
        return _result(call, 422, {"error": "missing_replay_actor"})
    from app.adapters.db.session import make_uow

    with make_uow() as session:
        if not isinstance(session, Session):
            return _result(call, 500, {"error": "unsupported_session"})
        workspace = load_workspace(session, workspace_id=workspace_id)
        if workspace is None:
            return _result(call, 404, {"error": "not_found"})
        previous_quota = (
            workspace.quota_json if isinstance(workspace.quota_json, dict) else {}
        )
        previous_cap = previous_quota.get(_QUOTA_CAP_KEY)
        if previous_cap == payload.cap_cents_30d:
            return _result(
                call,
                200,
                {"workspace_id": workspace.id, "cap_cents_30d": payload.cap_cents_30d},
                mutated=True,
            )
        with tenant_agnostic():
            updated_quota = dict(previous_quota)
            updated_quota[_QUOTA_CAP_KEY] = payload.cap_cents_30d
            workspace.quota_json = updated_quota
            _audit_replay(
                session,
                ctx=ctx,
                correlation_id=call.id,
                entity_kind="workspace",
                entity_id=workspace.id,
                action="usage.cap_updated",
                diff={
                    "cap_cents_30d": {
                        "before": previous_cap,
                        "after": payload.cap_cents_30d,
                    }
                },
            )
            session.flush()
    return _result(
        call,
        200,
        {"workspace_id": workspace_id, "cap_cents_30d": payload.cap_cents_30d},
        mutated=True,
    )


def _dispatch_workspace_trust(
    call: ToolCall,
    *,
    headers: Mapping[str, str],
) -> ToolResult:
    workspace_id = _input_str(call.input, "id")
    if workspace_id is None:
        return _result(call, 422, {"error": "invalid_workspace_input"})
    ctx = _ctx_from_headers(headers)
    if ctx is None:
        return _result(call, 422, {"error": "missing_replay_actor"})
    from app.adapters.db.session import make_uow

    with make_uow() as session:
        if not isinstance(session, Session):
            return _result(call, 500, {"error": "unsupported_session"})
        workspace = load_workspace(session, workspace_id=workspace_id)
        if workspace is None:
            return _result(call, 404, {"error": "not_found"})
        previous = verification_state_of(workspace)
        if previous != "trusted":
            with tenant_agnostic():
                set_verification_state(workspace, value="trusted")
                _audit_replay(
                    session,
                    ctx=ctx,
                    correlation_id=call.id,
                    entity_kind="workspace",
                    entity_id=workspace.id,
                    action="workspace.trusted",
                    diff={
                        "verification_state": {"before": previous, "after": "trusted"}
                    },
                )
                session.flush()
    return _result(
        call,
        200,
        {"id": workspace_id, "verification_state": "trusted"},
        mutated=True,
    )


def _dispatch_workspace_archive(
    call: ToolCall,
    *,
    headers: Mapping[str, str],
) -> ToolResult:
    workspace_id = _input_str(call.input, "id")
    if workspace_id is None:
        return _result(call, 422, {"error": "invalid_workspace_input"})
    ctx = _ctx_from_headers(headers)
    if ctx is None:
        return _result(call, 422, {"error": "missing_replay_actor"})
    from app.adapters.db.session import make_uow

    with make_uow() as session:
        if not isinstance(session, Session):
            return _result(call, 500, {"error": "unsupported_session"})
        try:
            ensure_deployment_owner(session, ctx=ctx)
        except DomainError:
            return _result(call, 404, {"error": "not_found"})
        workspace = load_workspace(session, workspace_id=workspace_id)
        if workspace is None:
            return _result(call, 404, {"error": "not_found"})
        archived_at = format_archived_at(workspace)
        if archived_at is None:
            moment = datetime.now(UTC)
            with tenant_agnostic():
                set_archived_at(workspace, when=moment)
                archived_at = moment.isoformat()
                _audit_replay(
                    session,
                    ctx=ctx,
                    correlation_id=call.id,
                    entity_kind="workspace",
                    entity_id=workspace.id,
                    action="workspace.archived",
                    diff={"archived_at": {"before": None, "after": archived_at}},
                )
                session.flush()
    return _result(
        call, 200, {"id": workspace_id, "archived_at": archived_at}, mutated=True
    )


def _ctx_from_headers(headers: Mapping[str, str]) -> DeploymentContext | None:
    actor_id = headers.get("X-Crewday-Replay-Actor-Id")
    if not actor_id:
        return None
    return DeploymentContext(
        principal=headers.get("X-Crewday-Replay-Principal", "admin_agent_replay"),
        user_id=actor_id,
        actor_kind="user",
        deployment_scopes=frozenset(),
    )


def _display_setting_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _replay_correlation_id(headers: Mapping[str, str], call: ToolCall) -> str:
    value = headers.get("Idempotency-Key")
    if value:
        return value
    return call.id


def _publish_admin_replay_event(
    *,
    kind: str,
    ctx: DeploymentContext,
    correlation_id: str,
    payload: Mapping[str, Any],
) -> None:
    occurred_at = datetime.now(UTC)
    body = dict(payload)
    body.setdefault("actor_id", ctx.user_id)
    body.setdefault("correlation_id", correlation_id)
    body.setdefault("occurred_at", occurred_at.isoformat())
    admin_sse.default_admin_fanout.publish(kind=kind, payload=body)


def _audit_replay(
    session: Session,
    *,
    ctx: DeploymentContext,
    correlation_id: str,
    entity_kind: str,
    entity_id: str,
    action: str,
    diff: dict[str, Any],
) -> None:
    # code-health: ignore[params] Deployment audit helper mirrors audit row fields.
    from app.authz.deployment_owners import is_deployment_owner

    write_deployment_audit(
        session,
        actor_id=ctx.user_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=is_deployment_owner(session, user_id=ctx.user_id),
        correlation_id=correlation_id,
        entity_kind=entity_kind,
        entity_id=entity_id,
        action=action,
        diff=diff,
    )


def _result(
    call: ToolCall,
    status_code: int,
    body: object,
    *,
    mutated: bool = False,
) -> ToolResult:
    return ToolResult(
        call_id=call.id,
        status_code=status_code,
        body=body,
        mutated=mutated and status_code < 400,
    )


def _input_str(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _unavailable(error: str) -> ServiceUnavailable:
    return ServiceUnavailable(
        "admin agent action production requires a configured runtime",
        extra={"error": error, "message": "admin agent runtime unavailable"},
    )
