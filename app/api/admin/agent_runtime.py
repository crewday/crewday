"""Deployment-admin agent action producer and replay dispatcher."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

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
from app.agent.dispatcher import (
    _MUTATING_METHODS,
    _build_index,
    _coerce_body,
    _confirm_metadata,
    _format_path,
    _is_valid_x_cli_metadata,
    _split_inputs,
    _tool_activity_label,
    _tool_description,
    _tool_input_schema,
)
from app.api.admin._workspace_state import load_workspace
from app.api.admin.agent import (
    _ADMIN_AGENT_CHANNEL,
    AdminAgentActionProducer,
    AdminAgentActionProposal,
    AdminAgentTextReply,
)
from app.api.admin.settings import preview_deployment_setting_for_agent
from app.api.admin.usage import UsageCapPayload
from app.domain.agent.runtime import (
    DelegatedToken,
    GateDecision,
    ToolCall,
    ToolDispatcher,
    ToolResult,
)
from app.domain.errors import ServiceUnavailable
from app.domain.llm.router import CapabilityUnassignedError, resolve_primary
from app.tenancy import DeploymentContext, tenant_agnostic
from app.tenancy.context import WorkspaceContext
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
_ADMIN_API_ROOT = "/admin/api/v1"
_ADMIN_AGENT_API_ROOT = f"{_ADMIN_API_ROOT}/agent"
_SURFACE_ADMIN_PATH = (
    Path(__file__).resolve().parents[3] / "cli/crewday/_surface_admin.json"
)
_BODY_METHODS = frozenset({"POST", "PATCH", "PUT"})
_SESSION_ONLY_ADMIN_SECRET_OPERATION_IDS = frozenset(
    {
        "admin.llm.providers.key.set",
        "admin.llm.providers.key.clear",
    }
)
_SESSION_ONLY_ADMIN_SECRET_PATHS = frozenset(
    {
        f"{_ADMIN_API_ROOT}/llm/providers/{{provider_id}}/key",
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
        dispatcher: DeploymentAdminToolDispatcher | None = None,
    ) -> None:
        self._llm = llm
        self._dispatcher = dispatcher
        self._tools = tuple(tools) if tools is not None else _admin_tools(dispatcher)

    def produce_action(
        self,
        *,
        message: str,
        page_context: str,
        ctx: DeploymentContext,
        session: Session,
    ) -> AdminAgentActionProposal | AdminAgentTextReply | None:
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
        if len(response.tool_calls) > 1:
            return None
        resolved_call = _resolve_tool_call(response)
        if resolved_call is None:
            reply = _text_reply(response.text)
            if reply is not None:
                return reply
            return None
        proposal = _resolve_supported_proposal(
            resolved_call,
            session=session,
            dispatcher=self._dispatcher,
        )
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
    """Replay deployment-admin tool calls through the admin OpenAPI surface."""

    def __init__(
        self,
        fallback: ToolDispatcher | None = None,
        *,
        app: FastAPI | None = None,
    ) -> None:
        self._fallback = fallback
        self._app = app
        self._client: TestClient | None = None

    @property
    def tools(self) -> tuple[Tool, ...]:
        if self._app is None:
            return ()
        return self._tools

    @cached_property
    def _schema(self) -> Mapping[str, Any]:
        if self._app is None:
            return {}
        schema = self._app.openapi()
        return schema if isinstance(schema, Mapping) else {}

    @cached_property
    def _index(self) -> Mapping[str, Any]:
        paths = self._schema.get("paths")
        if not isinstance(paths, Mapping):
            paths = {}
        return _build_index(paths)

    @cached_property
    def _components(self) -> Mapping[str, Any]:
        components = self._schema.get("components")
        return components if isinstance(components, Mapping) else {}

    @cached_property
    def _surface_operation_ids(self) -> frozenset[str]:
        return _load_admin_surface_operation_ids()

    @cached_property
    def _tools(self) -> tuple[Tool, ...]:
        return tuple(
            {
                "name": op_id,
                "description": _tool_description(op_id, entry.operation),
                "input_schema": _tool_input_schema(
                    entry,
                    workspace_slug="",
                    components=self._components,
                ),
            }
            for op_id, entry in sorted(self._index.items())
            if _is_deployment_admin_agent_tool(
                op_id,
                entry,
                surface_operation_ids=self._surface_operation_ids,
            )
        )

    def is_gated(self, call: ToolCall) -> GateDecision:
        entry = self._index.get(call.name)
        if (
            entry is not None
            and _deployment_admin_agent_rejection(
                call.name,
                entry,
                surface_operation_ids=self._surface_operation_ids,
            )
            is None
        ):
            if entry.method not in _MUTATING_METHODS:
                return GateDecision(gated=False)
            resolved = _confirm_metadata(entry.operation.get("x-agent-confirm"))
            if resolved is not None:
                summary, risk = resolved
                return GateDecision(
                    gated=True,
                    card_summary=summary,
                    card_risk=risk,
                    pre_approval_source="annotation",
                )
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
        entry = self._index.get(call.name)
        if entry is not None:
            rejection = _deployment_admin_agent_rejection(
                call.name,
                entry,
                surface_operation_ids=self._surface_operation_ids,
            )
            if rejection is None or _is_admin_api_path(entry.path):
                return self._dispatch_admin_openapi_tool(call, entry, token, headers)
            if _is_admin_agent_replay(headers):
                return ToolResult(
                    call_id=call.id,
                    status_code=403,
                    body={"detail": rejection},
                    mutated=False,
                )
        if self._fallback is not None and not _is_admin_agent_replay(headers):
            return self._fallback.dispatch(call, token=token, headers=headers)
        return ToolResult(
            call_id=call.id,
            status_code=404,
            body={"error": "unsupported_tool", "tool": call.name},
            mutated=False,
        )

    def activity_label_for(self, call: ToolCall) -> str:
        entry = self._index.get(call.name)
        if entry is not None and _is_admin_api_path(entry.path):
            return _tool_activity_label(call.name, entry.operation)
        if self._fallback is not None:
            return self._fallback.activity_label_for(call)
        return "Working"

    def resolve_proposal(
        self,
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
        entry = self._index.get(call.name)
        if entry is None:
            return None
        if (
            _deployment_admin_agent_rejection(
                call.name,
                entry,
                surface_operation_ids=self._surface_operation_ids,
            )
            is not None
        ):
            return None
        if entry.method not in _MUTATING_METHODS:
            return None
        try:
            _split_inputs(entry, call.input, components=self._components)
        except ValueError:
            return None
        decision = self.is_gated(call)
        risk = decision.card_risk if decision.gated else "medium"
        summary = (
            decision.card_summary
            if decision.gated and decision.card_summary
            else _tool_activity_label(call.name, entry.operation)
        )
        return _ResolvedProposal(
            call=ToolCall(id=call.id, name=call.name, input=dict(call.input)),
            summary=summary,
            fields=_proposal_fields(call),
            risk=risk,
        )

    def _dispatch_admin_openapi_tool(
        self,
        call: ToolCall,
        entry: Any,
        token: DelegatedToken,
        headers: Mapping[str, str],
    ) -> ToolResult:
        if call.name == "admin.settings.update":
            setting_error = _settings_update_rejection(call)
            if setting_error is not None:
                return _result(call, 422, {"error": setting_error})
        rejection = _deployment_admin_agent_rejection(
            call.name,
            entry,
            surface_operation_ids=self._surface_operation_ids,
        )
        if rejection is not None:
            return ToolResult(
                call_id=call.id,
                status_code=403,
                body={"detail": rejection},
                mutated=False,
            )
        try:
            path_vars, query, body = _split_inputs(
                entry,
                call.input,
                components=self._components,
            )
        except ValueError as exc:
            return ToolResult(
                call_id=call.id,
                status_code=422,
                body={"detail": str(exc)},
                mutated=False,
            )
        response = self._get_client().request(
            method=entry.method,
            url=_format_path(entry.path, path_vars),
            params=_query_params(query),
            json=body if entry.method in _BODY_METHODS else None,
            headers=_admin_request_headers(token, headers, has_body=body is not None),
        )
        return ToolResult(
            call_id=call.id,
            status_code=response.status_code,
            body=_coerce_body(response.content, response.headers.get("content-type")),
            mutated=entry.method in _MUTATING_METHODS and response.status_code < 400,
        )

    def _get_client(self) -> TestClient:
        if self._app is None:
            raise RuntimeError("deployment admin dispatcher requires a FastAPI app")
        if self._client is None:
            self._client = TestClient(self._app, raise_server_exceptions=False)
        return self._client


def _admin_tools(
    dispatcher: DeploymentAdminToolDispatcher | None = None,
) -> tuple[Tool, ...]:
    if dispatcher is None:
        return _legacy_admin_tools()
    return dispatcher.tools


def _legacy_admin_tools() -> tuple[Tool, ...]:
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


def _load_admin_surface_operation_ids() -> frozenset[str]:
    try:
        raw = json.loads(_SURFACE_ADMIN_PATH.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return frozenset()
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(
        op_id
        for item in raw
        if isinstance(item, Mapping)
        and isinstance((op_id := item.get("operation_id")), str)
        and op_id
    )


def _is_deployment_admin_agent_tool(
    op_id: str,
    entry: Any,
    *,
    surface_operation_ids: frozenset[str],
) -> bool:
    return (
        _deployment_admin_agent_rejection(
            op_id,
            entry,
            surface_operation_ids=surface_operation_ids,
        )
        is None
    )


def _deployment_admin_agent_rejection(
    op_id: str,
    entry: Any,
    *,
    surface_operation_ids: frozenset[str],
) -> str | None:
    if not _is_admin_api_path(entry.path):
        return "tool is outside the deployment-admin agent surface"
    if _is_admin_agent_api_path(entry.path):
        return "tool controls the admin agent session itself"
    if _is_session_only_admin_secret_route(op_id, entry.path):
        return "tool requires an interactive session"
    if entry.operation.get("x-agent-forbidden") is True:
        return "tool is forbidden to delegated agents"
    if entry.operation.get("x-interactive-only") is True:
        return "tool requires an interactive session"
    if _is_valid_x_cli_metadata(entry.operation):
        return None
    if op_id in surface_operation_ids:
        return None
    return "tool is not backed by admin surface metadata"


def _is_admin_api_path(path: str) -> bool:
    return path == _ADMIN_API_ROOT or path.startswith(f"{_ADMIN_API_ROOT}/")


def _is_admin_agent_api_path(path: str) -> bool:
    return path == _ADMIN_AGENT_API_ROOT or path.startswith(f"{_ADMIN_AGENT_API_ROOT}/")


def _is_session_only_admin_secret_route(op_id: str, path: str) -> bool:
    return (
        op_id in _SESSION_ONLY_ADMIN_SECRET_OPERATION_IDS
        or path in _SESSION_ONLY_ADMIN_SECRET_PATHS
    )


def _is_admin_agent_replay(headers: Mapping[str, str]) -> bool:
    return headers.get("X-Agent-Channel") == _ADMIN_AGENT_CHANNEL


def _query_params(query: Mapping[str, object]) -> dict[str, str] | None:
    if not query:
        return None
    params: dict[str, str] = {}
    for key, value in query.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            params[key] = "" if value is None else str(value)
        else:
            params[key] = json.dumps(value)
    return params


def _admin_request_headers(
    token: DelegatedToken,
    caller_headers: Mapping[str, str],
    *,
    has_body: bool,
) -> dict[str, str]:
    merged: dict[str, str] = {
        "Authorization": f"Bearer {token.plaintext}",
    }
    if has_body:
        merged["Content-Type"] = "application/json"
    canonical = {key.lower(): key for key in merged}
    for key, value in caller_headers.items():
        if key.lower() == "authorization":
            continue
        existing = canonical.get(key.lower())
        if existing is not None:
            merged[existing] = value
        else:
            merged[key] = value
            canonical[key.lower()] = key
    return merged


def _proposal_fields(call: ToolCall) -> tuple[tuple[str, str], ...]:
    fields: list[tuple[str, str]] = []
    for key, value in call.input.items():
        if len(fields) >= 6:
            break
        fields.append((str(key), _display_setting_value(value)))
    if not fields:
        fields.append(("Tool", call.name))
    return tuple(fields)


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
        ctx = WorkspaceContext(
            workspace_id="__deployment_admin_llm__",
            workspace_slug="deployment-admin",
            actor_id="system",
            actor_kind="system",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id="admin-agent-model-resolver",
            principal_kind="system",
        )
        try:
            return resolve_primary(session, ctx, _CAPABILITY).api_model_id
        except CapabilityUnassignedError:
            return None


def _resolve_tool_call(response: LLMResponse) -> ToolCall | None:
    if response.tool_calls:
        first = response.tool_calls[0]
        return ToolCall(
            id=first.id or new_ulid(),
            name=first.name,
            input=dict(first.arguments),
        )
    return _parse_text_tool_call(response.text)


def _text_reply(text: str) -> AdminAgentTextReply | None:
    body = text.strip()
    if not body:
        return None
    return AdminAgentTextReply(body=body)


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
    dispatcher: DeploymentAdminToolDispatcher | None = None,
) -> _ResolvedProposal | None:
    if dispatcher is not None:
        return dispatcher.resolve_proposal(call, session=session)
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


def _settings_update_rejection(call: ToolCall) -> str | None:
    key = _input_str(call.input, "key")
    if key is None or "value" not in call.input:
        return "invalid_setting_input"
    preview = preview_deployment_setting_for_agent(
        key=key,
        raw_value=call.input["value"],
    )
    if preview is None:
        return "invalid_setting_value"
    return None


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


def _display_setting_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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
