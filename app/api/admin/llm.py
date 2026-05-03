"""Deployment-admin LLM graph routes."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import (
    LlmAssignment,
    LlmCapabilityInheritance,
    LlmModel,
    LlmPromptTemplate,
    LlmPromptTemplateRevision,
    LlmProvider,
    LlmProviderModel,
    LlmUsage,
)
from app.adapters.db.workspace.models import Workspace
from app.api.admin.deps import require_deployment_scope
from app.api.deps import db_session
from app.api.transport import admin_sse
from app.domain.agent.compaction import (
    COMPACT_CAPABILITY as _COMPACT_CAPABILITY,
)
from app.domain.agent.compaction import (
    _default_compaction_prompt,
)
from app.domain.agent.runtime import _default_system_prompt
from app.events.bus import bus as default_event_bus
from app.events.types import LlmAssignmentChanged
from app.tenancy import DeploymentContext, tenant_agnostic
from app.util.ulid import new_ulid

__all__ = ["build_admin_llm_router"]


_Db = Annotated[Session, Depends(db_session)]
_ReadCtx = Annotated[
    DeploymentContext, Depends(require_deployment_scope("deployment.llm:read"))
]
_WriteCtx = Annotated[
    DeploymentContext, Depends(require_deployment_scope("deployment.llm:write"))
]

_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1"
_CAPABILITIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "tasks.nl_intake",
        "Parse a free-text description into a task / template / schedule draft",
        ("chat", "json_mode"),
    ),
    ("tasks.assist", "Staff chat assistant", ("chat",)),
    ("digest.manager", "Morning owner/manager digest composition", ("chat",)),
    ("digest.employee", "Morning worker digest composition", ("chat",)),
    (
        "anomaly.detect",
        "Compare recent completions and flag anomalies",
        ("chat", "json_mode"),
    ),
    ("expenses.autofill", "OCR and structure a receipt image", ("vision", "json_mode")),
    ("instructions.draft", "Suggest an instruction from a conversation", ("chat",)),
    (
        "issue.triage",
        "Classify severity/category of a reported issue",
        ("chat", "json_mode"),
    ),
    ("stay.summarize", "Summarize a stay", ("chat",)),
    ("voice.transcribe", "Turn a voice note into text", ("audio_input",)),
    (
        "chat.manager",
        "Owner/manager-side embedded chat agent",
        ("chat", "function_calling"),
    ),
    ("chat.employee", "Worker-side embedded chat agent", ("chat", "function_calling")),
    (
        "chat.admin",
        "Deployment-admin embedded chat agent",
        ("chat", "function_calling"),
    ),
    ("chat.compact", "Summarise resolved topics in a chat thread", ("chat",)),
    (
        "chat.detect_language",
        "Detect message language for auto-translation",
        ("chat", "json_mode"),
    ),
    (
        "chat.translate",
        "Translate a message into the workspace default language",
        ("chat",),
    ),
    ("documents.ocr", "Vision fallback for image-bearing documents", ("vision",)),
    (
        "feedback.moderate",
        "Moderate and reformulate a marketing-site suggestion",
        ("chat", "json_mode"),
    ),
    ("feedback.embed", "Compute dense embeddings for texts", ("embeddings",)),
    (
        "feedback.cluster",
        "Classify a reformulated marketing-site submission against clusters",
        ("chat", "json_mode"),
    ),
)
_CAPABILITY_REQUIRED: dict[str, list[str]] = {
    key: list(required) for key, _description, required in _CAPABILITIES
}
_CAPABILITY_DESCRIPTIONS: dict[str, str] = {
    key: description for key, description, _required in _CAPABILITIES
}
_MODEL_CAPABILITY_TAGS = frozenset(
    {
        "chat",
        "vision",
        "audio_input",
        "reasoning",
        "function_calling",
        "json_mode",
        "streaming",
        "embeddings",
    }
)


class LlmProviderResponse(BaseModel):
    id: str
    name: str
    provider_type: Literal["openrouter", "openai_compatible", "fake"]
    endpoint: str
    api_key_ref: str | None
    api_key_status: Literal["present", "missing", "rotating"]
    default_model: str | None
    requests_per_minute: int
    timeout_s: int
    priority: int
    is_enabled: bool
    provider_model_count: int


class LlmModelResponse(BaseModel):
    id: str
    canonical_name: str
    display_name: str
    vendor: str
    capabilities: list[str]
    context_window: int | None
    max_output_tokens: int | None
    price_source: Literal["openrouter", "manual", ""]
    price_source_model_id: str | None
    is_active: bool
    notes: str | None
    provider_model_count: int


class LlmProviderModelResponse(BaseModel):
    id: str
    provider_id: str
    model_id: str
    api_model_id: str
    input_cost_per_million: float
    output_cost_per_million: float
    max_tokens_override: int | None
    temperature_override: float | None
    supports_system_prompt: bool
    supports_temperature: bool
    reasoning_effort: Literal["", "low", "medium", "high"]
    price_source_override: Literal["", "none", "openrouter"]
    price_last_synced_at: str | None
    is_enabled: bool


class LlmCapabilityEntry(BaseModel):
    key: str
    description: str
    required_capabilities: list[str]


class LlmCapabilityInheritanceResponse(BaseModel):
    capability: str
    inherits_from: str


class LlmAssignmentResponse(BaseModel):
    id: str
    capability: str
    description: str
    priority: int
    provider_model_id: str
    max_tokens: int | None
    temperature: float | None
    extra_api_params: dict[str, Any]
    required_capabilities: list[str]
    is_enabled: bool
    last_used_at: str | None
    spend_usd_30d: float
    calls_30d: int


class LlmAssignmentIssue(BaseModel):
    assignment_id: str
    capability: str
    missing_capabilities: list[str]


class LlmPromptTemplateResponse(BaseModel):
    id: str
    capability: str
    name: str
    version: int
    is_active: bool
    is_customised: bool
    default_hash: str
    updated_at: str
    revisions_count: int
    preview: str


class LlmPromptTemplateDetail(LlmPromptTemplateResponse):
    template: str
    notes: str | None


class LlmPromptRevisionResponse(BaseModel):
    id: str
    template_id: str
    version: int
    body: str
    notes: str | None
    created_at: str
    created_by_user_id: str | None


class LlmCallResponse(BaseModel):
    """One row of the /admin/usage call feed.

    ``model_id`` and ``provider_model_id`` are intentionally distinct
    fields and **not** redundant — do not collapse them:

    * ``model_id`` is the **wire-name string** that flowed across the
      network on this call (sourced from ``llm_usage.provider_model_id``,
      which stores the free-form provider wire name — see
      :class:`~app.adapters.db.llm.models.LlmUsage`). Always present;
      survives a registry row's retirement so historical calls still
      render.
    * ``provider_model_id`` is the **resolved registry id** (a
      ``LlmProviderModel.id``), looked up at read time from the wire
      string via :func:`_llm_usage_provider_model_id`. ``None`` when
      the registry row has been retired since the call was made.

    The DB column was renamed in cd-v6dj from ``model_id`` to
    ``provider_model_id`` to match the §02 spec, but the JSON wire
    contract here keeps both fields under their pre-existing names —
    a frontend (``mocks/web/src/types/api.ts::LLMCall``) and OpenAPI
    consumers depend on the shape.
    """

    at: str
    capability: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cost_cents: int
    latency_ms: int
    status: Literal["ok", "error", "redacted_block"]
    assignment_id: str | None = None
    provider_model_id: str | None = None
    prompt_template_id: str | None = None
    prompt_version: int | None = None
    fallback_attempts: int = 0
    raw_response_available: bool = False


class LlmGraphTotals(BaseModel):
    spend_usd_30d: float
    calls_30d: int
    provider_count: int
    model_count: int
    capability_count: int
    unassigned_capabilities: list[str]


class LlmGraphPayload(BaseModel):
    providers: list[LlmProviderResponse]
    models: list[LlmModelResponse]
    provider_models: list[LlmProviderModelResponse]
    capabilities: list[LlmCapabilityEntry]
    inheritance: list[LlmCapabilityInheritanceResponse]
    assignments: list[LlmAssignmentResponse]
    assignment_issues: list[LlmAssignmentIssue]
    totals: LlmGraphTotals


class LlmSyncPricingDelta(BaseModel):
    provider_model_id: str
    api_model_id: str
    input_before: float
    input_after: float
    output_before: float
    output_after: float
    status: Literal["updated", "unchanged", "pinned", "error"]


class LlmSyncPricingResult(BaseModel):
    started_at: str
    deltas: list[LlmSyncPricingDelta]
    updated: int
    skipped: int
    errors: int


class ProviderPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    provider_type: Literal["openrouter", "openai_compatible", "fake"]
    api_endpoint: str | None = Field(default=None, max_length=2048)
    api_key_envelope_ref: str | None = Field(default=None, max_length=512)
    default_model: str | None = None
    timeout_s: int = Field(default=60, ge=1, le=600)
    requests_per_minute: int = Field(default=60, ge=1, le=100_000)
    priority: int = Field(default=0, ge=0)
    is_enabled: bool = True

    @model_validator(mode="after")
    def _validate_endpoint(self) -> Self:
        if self.provider_type == "openai_compatible" and not self.api_endpoint:
            raise ValueError("openai_compatible providers require api_endpoint")
        return self


class ModelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(min_length=1, max_length=240)
    display_name: str = Field(min_length=1, max_length=240)
    vendor: str = Field(min_length=1, max_length=80)
    capabilities: list[str] = Field(default_factory=list)
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    price_source: Literal["openrouter", "manual", ""] = ""
    price_source_model_id: str | None = Field(default=None, max_length=240)
    is_active: bool = True
    notes: str | None = None

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for tag in value:
            if tag not in _MODEL_CAPABILITY_TAGS:
                raise ValueError(f"unknown model capability tag: {tag}")
            if tag in seen:
                raise ValueError(f"duplicate model capability tag: {tag}")
            seen.add(tag)
        return value


class ProviderModelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    model_id: str
    api_model_id: str = Field(min_length=1, max_length=240)
    input_cost_per_million: float = Field(default=0, ge=0)
    output_cost_per_million: float = Field(default=0, ge=0)
    max_tokens_override: int | None = Field(default=None, ge=1)
    temperature_override: float | None = Field(default=None, ge=0, le=2)
    supports_system_prompt: bool = True
    supports_temperature: bool = True
    reasoning_effort: Literal["", "low", "medium", "high"] = ""
    extra_api_params: dict[str, Any] = Field(default_factory=dict)
    price_source_override: Literal["", "none", "openrouter"] = ""
    price_source_model_id_override: str | None = None
    is_enabled: bool = True


class AssignmentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    capability: str
    provider_model_id: str
    priority: int = Field(default=0, ge=0)
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    extra_api_params: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] | None = None
    is_enabled: bool = True


class AssignmentUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priority: int | None = Field(default=None, ge=0)
    provider_model_id: str | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    extra_api_params: dict[str, Any] | None = None
    required_capabilities: list[str] | None = None
    is_enabled: bool | None = None


class AssignmentReorderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str
    ids_in_priority_order: list[str]


class PromptUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str = Field(min_length=1)
    notes: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _money(value: Decimal | int | float | None) -> float:
    if value is None:
        return 0.0
    return float(value)


def _hash_body(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _current_prompt_default(capability: str) -> str:
    if capability == "chat.manager":
        return _default_system_prompt("manager")
    if capability == "chat.employee":
        return _default_system_prompt("employee")
    if capability == "chat.admin":
        return _default_system_prompt("admin")
    if capability == _COMPACT_CAPABILITY:
        return _default_compaction_prompt()
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={"error": "prompt_default_unavailable"},
    )


def _endpoint(provider: LlmProvider) -> str:
    if provider.api_endpoint:
        return provider.api_endpoint
    if provider.provider_type == "openrouter":
        return _OPENROUTER_ENDPOINT
    return ""


def _provider_response(
    provider: LlmProvider, provider_model_counts: Counter[str]
) -> LlmProviderResponse:
    api_key_status: Literal["present", "missing", "rotating"] = (
        "present" if provider.api_key_envelope_ref else "missing"
    )
    return LlmProviderResponse(
        id=provider.id,
        name=provider.name,
        provider_type=provider.provider_type,
        endpoint=_endpoint(provider),
        api_key_ref=provider.api_key_envelope_ref,
        api_key_status=api_key_status,
        default_model=provider.default_model,
        requests_per_minute=provider.requests_per_minute,
        timeout_s=provider.timeout_s,
        priority=provider.priority,
        is_enabled=provider.is_enabled,
        provider_model_count=provider_model_counts[provider.id],
    )


def _model_response(
    model: LlmModel, provider_model_counts: Counter[str]
) -> LlmModelResponse:
    return LlmModelResponse(
        id=model.id,
        canonical_name=model.canonical_name,
        display_name=model.display_name,
        vendor=model.vendor,
        capabilities=list(model.capabilities or []),
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        price_source=model.price_source,
        price_source_model_id=model.price_source_model_id,
        is_active=model.is_active,
        notes=model.notes,
        provider_model_count=provider_model_counts[model.id],
    )


def _provider_model_response(row: LlmProviderModel) -> LlmProviderModelResponse:
    return LlmProviderModelResponse(
        id=row.id,
        provider_id=row.provider_id,
        model_id=row.model_id,
        api_model_id=row.api_model_id,
        input_cost_per_million=_money(row.input_cost_per_million),
        output_cost_per_million=_money(row.output_cost_per_million),
        max_tokens_override=row.max_tokens_override,
        temperature_override=row.temperature_override,
        supports_system_prompt=row.supports_system_prompt,
        supports_temperature=row.supports_temperature,
        reasoning_effort=row.reasoning_effort or "",
        price_source_override=row.price_source_override or "",
        price_last_synced_at=_iso(row.price_last_synced_at),
        is_enabled=row.is_enabled,
    )


def _capabilities() -> list[LlmCapabilityEntry]:
    return [
        LlmCapabilityEntry(
            key=key,
            description=description,
            required_capabilities=list(required),
        )
        for key, description, required in _CAPABILITIES
    ]


def _assignment_response(
    row: LlmAssignment,
    *,
    usage: dict[str, tuple[int, int]],
) -> LlmAssignmentResponse:
    calls, spend_cents = usage.get(row.id, (0, 0))
    return LlmAssignmentResponse(
        id=row.id,
        capability=row.capability,
        description=_CAPABILITY_DESCRIPTIONS.get(row.capability, row.capability),
        priority=row.priority,
        provider_model_id=row.model_id,
        max_tokens=row.max_tokens,
        temperature=row.temperature,
        extra_api_params=dict(row.extra_api_params or {}),
        required_capabilities=list(row.required_capabilities or []),
        is_enabled=row.enabled,
        last_used_at=None,
        spend_usd_30d=round(spend_cents / 100, 2),
        calls_30d=calls,
    )


def _prompt_response(
    row: LlmPromptTemplate,
    revisions_count: int,
) -> LlmPromptTemplateResponse:
    preview = row.template.strip().replace("\n", " ")
    return LlmPromptTemplateResponse(
        id=row.id,
        capability=row.capability,
        name=row.name,
        version=row.version,
        is_active=row.is_active,
        is_customised=_hash_body(row.template) != row.default_hash,
        default_hash=row.default_hash,
        updated_at=_iso(row.updated_at) or "",
        revisions_count=revisions_count,
        preview=preview[:160],
    )


def _assignment_usage(session: Session, cutoff: datetime) -> dict[str, tuple[int, int]]:
    rows = session.execute(
        select(
            LlmUsage.assignment_id,
            func.count(LlmUsage.id),
            func.coalesce(func.sum(LlmUsage.cost_cents), 0),
        )
        .where(LlmUsage.created_at >= cutoff, LlmUsage.assignment_id.is_not(None))
        .group_by(LlmUsage.assignment_id)
    ).all()
    return {
        str(assignment_id): (int(count or 0), int(spend or 0))
        for assignment_id, count, spend in rows
        if assignment_id is not None
    }


def _capability_has_chain(
    capability: str,
    *,
    assigned_capabilities: set[str],
    inheritance: dict[str, str],
) -> bool:
    seen: set[str] = set()
    current = capability
    for _ in range(16):
        if current in assigned_capabilities:
            return True
        if current in seen:
            return False
        seen.add(current)
        parent = inheritance.get(current)
        if parent is None:
            return False
        current = parent
    return False


def _load_graph(session: Session) -> LlmGraphPayload:
    cutoff = _now() - timedelta(days=30)
    with tenant_agnostic():
        providers = list(
            session.scalars(
                select(LlmProvider).order_by(LlmProvider.priority, LlmProvider.name)
            ).all()
        )
        models = list(
            session.scalars(
                select(LlmModel).order_by(LlmModel.display_name, LlmModel.id)
            ).all()
        )
        provider_models = list(
            session.scalars(
                select(LlmProviderModel).order_by(
                    LlmProviderModel.api_model_id, LlmProviderModel.id
                )
            ).all()
        )
        assignments = list(
            session.scalars(
                select(LlmAssignment).order_by(
                    LlmAssignment.capability,
                    LlmAssignment.priority,
                    LlmAssignment.id,
                )
            ).all()
        )
        inheritance = list(
            session.scalars(
                select(LlmCapabilityInheritance).order_by(
                    LlmCapabilityInheritance.capability
                )
            ).all()
        )
        usage = _assignment_usage(session, cutoff)

    provider_counts: Counter[str] = Counter(row.provider_id for row in provider_models)
    model_counts: Counter[str] = Counter(row.model_id for row in provider_models)
    provider_models_by_id = {row.id: row for row in provider_models}
    models_by_id = {row.id: row for row in models}
    inheritance_by_capability = {
        row.capability: row.inherits_from for row in inheritance
    }

    assignment_responses = [
        _assignment_response(row, usage=usage) for row in assignments
    ]
    issues: list[LlmAssignmentIssue] = []
    for assignment in assignments:
        provider_model = provider_models_by_id.get(assignment.model_id)
        model = models_by_id.get(provider_model.model_id) if provider_model else None
        model_caps = set(model.capabilities if model else [])
        missing = [
            cap
            for cap in assignment.required_capabilities or []
            if cap not in model_caps
        ]
        if missing:
            issues.append(
                LlmAssignmentIssue(
                    assignment_id=assignment.id,
                    capability=assignment.capability,
                    missing_capabilities=missing,
                )
            )

    enabled_assignment_caps = {row.capability for row in assignments if row.enabled}
    capability_keys = [entry.key for entry in _capabilities()]
    total_calls = sum(calls for calls, _spend in usage.values())
    total_spend = sum(spend for _calls, spend in usage.values())
    return LlmGraphPayload(
        providers=[_provider_response(row, provider_counts) for row in providers],
        models=[_model_response(row, model_counts) for row in models],
        provider_models=[_provider_model_response(row) for row in provider_models],
        capabilities=_capabilities(),
        inheritance=[
            LlmCapabilityInheritanceResponse(
                capability=row.capability, inherits_from=row.inherits_from
            )
            for row in inheritance
        ],
        assignments=assignment_responses,
        assignment_issues=issues,
        totals=LlmGraphTotals(
            spend_usd_30d=round(total_spend / 100, 2),
            calls_30d=total_calls,
            provider_count=len(providers),
            model_count=len(models),
            capability_count=len(capability_keys),
            unassigned_capabilities=[
                capability
                for capability in capability_keys
                if not _capability_has_chain(
                    capability,
                    assigned_capabilities=enabled_assignment_caps,
                    inheritance=inheritance_by_capability,
                )
            ],
        ),
    )


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found"}
    )


def _conflict(error: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": error})


def _unprocessable(error: str, **extra: object) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"error": error, **extra},
    )


def _commit_or_conflict(session: Session, error: str) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise _conflict(error) from exc


def _flush_or_conflict(session: Session, error: str) -> None:
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _conflict(error) from exc


def _publish_assignment_changed(
    ctx: DeploymentContext,
    request: Request,
    workspace_id: str,
) -> None:
    default_event_bus.publish(
        LlmAssignmentChanged(
            workspace_id=workspace_id,
            actor_id=ctx.user_id,
            correlation_id=new_ulid(),
            occurred_at=_now(),
        )
    )
    admin_sse.publish_admin_event(
        kind="admin.llm.assignment_updated",
        ctx=ctx,
        request=request,
        payload={"workspace_id": workspace_id},
    )


def _workspace_exists(session: Session, workspace_id: str) -> bool:
    return session.get(Workspace, workspace_id) is not None


def _provider_exists(session: Session, provider_id: str) -> bool:
    return session.get(LlmProvider, provider_id) is not None


def _model_exists(session: Session, model_id: str) -> bool:
    return session.get(LlmModel, model_id) is not None


def _provider_model(session: Session, provider_model_id: str) -> LlmProviderModel:
    row = session.get(LlmProviderModel, provider_model_id)
    if row is None:
        raise _not_found()
    return row


def _missing_capabilities(
    session: Session,
    *,
    provider_model_id: str,
    required_capabilities: list[str],
) -> list[str]:
    provider_model = _provider_model(session, provider_model_id)
    model = session.get(LlmModel, provider_model.model_id)
    model_capabilities = set(model.capabilities if model is not None else [])
    return [cap for cap in required_capabilities if cap not in model_capabilities]


def _raise_missing_capabilities(missing: list[str]) -> None:
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "assignment_missing_capability",
                "missing_capabilities": missing,
            },
        )


def _catalog_required_capabilities(capability: str) -> list[str]:
    required = _CAPABILITY_REQUIRED.get(capability)
    if required is None:
        raise _unprocessable("unknown_capability", capability=capability)
    return list(required)


def _validate_required_capabilities(
    capability: str, provided: list[str] | None
) -> list[str]:
    required = _catalog_required_capabilities(capability)
    if provided is not None and provided != required:
        raise _unprocessable(
            "required_capabilities_mismatch",
            capability=capability,
            required_capabilities=required,
        )
    return required


def _validate_provider_payload(
    session: Session,
    payload: ProviderPayload,
    *,
    provider_id: str | None = None,
) -> None:
    duplicate = session.scalar(
        select(LlmProvider.id).where(LlmProvider.name == payload.name).limit(1)
    )
    if duplicate is not None and duplicate != provider_id:
        raise _conflict("provider_name_exists")
    if payload.default_model is None:
        return
    provider_model = session.get(LlmProviderModel, payload.default_model)
    if provider_model is None:
        raise _unprocessable("default_model_not_found")
    if provider_id is None or provider_model.provider_id != provider_id:
        raise _unprocessable("default_model_provider_mismatch")


def _validate_model_payload(
    session: Session,
    payload: ModelPayload,
    *,
    model_id: str | None = None,
) -> None:
    duplicate = session.scalar(
        select(LlmModel.id)
        .where(LlmModel.canonical_name == payload.canonical_name)
        .limit(1)
    )
    if duplicate is not None and duplicate != model_id:
        raise _conflict("model_canonical_name_exists")


def _validate_provider_model_payload(
    session: Session,
    payload: ProviderModelPayload,
    *,
    provider_model_id: str | None = None,
) -> None:
    if not _provider_exists(session, payload.provider_id):
        raise _unprocessable("provider_not_found")
    if not _model_exists(session, payload.model_id):
        raise _unprocessable("model_not_found")
    duplicate = session.scalar(
        select(LlmProviderModel.id)
        .where(
            LlmProviderModel.provider_id == payload.provider_id,
            LlmProviderModel.model_id == payload.model_id,
        )
        .limit(1)
    )
    if duplicate is not None and duplicate != provider_model_id:
        raise _conflict("provider_model_exists")


def _validate_assignment_priority(
    session: Session,
    *,
    workspace_id: str,
    capability: str,
    priority: int,
    assignment_id: str | None = None,
) -> None:
    duplicate = session.scalar(
        select(LlmAssignment.id)
        .where(
            LlmAssignment.workspace_id == workspace_id,
            LlmAssignment.capability == capability,
            LlmAssignment.priority == priority,
        )
        .limit(1)
    )
    if duplicate is not None and duplicate != assignment_id:
        raise _conflict("assignment_priority_exists")


def _assignment(session: Session, assignment_id: str) -> LlmAssignment:
    row = session.get(LlmAssignment, assignment_id)
    if row is None:
        raise _not_found()
    return row


def _status(row: LlmUsage) -> Literal["ok", "error", "redacted_block"]:
    if row.status == "ok":
        return "ok"
    if row.status == "refused":
        return "redacted_block"
    return "error"


def _llm_usage_provider_model_id(
    row: LlmUsage,
    *,
    provider_model_ids: set[str],
    provider_model_ids_by_api_model_id: dict[str, str],
) -> str | None:
    if row.provider_model_id in provider_model_ids:
        return row.provider_model_id
    return provider_model_ids_by_api_model_id.get(row.provider_model_id)


def build_admin_llm_router() -> APIRouter:
    router = APIRouter(prefix="/llm", tags=["admin", "llm"])

    @router.get(
        "/graph", response_model=LlmGraphPayload, operation_id="admin.llm.graph"
    )
    def graph(_ctx: _ReadCtx, session: _Db) -> LlmGraphPayload:
        return _load_graph(session)

    @router.get(
        "/providers",
        response_model=list[LlmProviderResponse],
        operation_id="admin.llm.providers.list",
    )
    def list_providers(_ctx: _ReadCtx, session: _Db) -> list[LlmProviderResponse]:
        with tenant_agnostic():
            provider_models = list(session.scalars(select(LlmProviderModel)).all())
            providers = list(
                session.scalars(
                    select(LlmProvider).order_by(LlmProvider.priority, LlmProvider.name)
                ).all()
            )
        counts: Counter[str] = Counter(row.provider_id for row in provider_models)
        return [_provider_response(row, counts) for row in providers]

    @router.post(
        "/providers",
        response_model=LlmProviderResponse,
        operation_id="admin.llm.providers.create",
    )
    def create_provider(
        ctx: _WriteCtx, session: _Db, payload: ProviderPayload
    ) -> LlmProviderResponse:
        now = _now()
        row = LlmProvider(
            id=new_ulid(),
            name=payload.name,
            provider_type=payload.provider_type,
            api_endpoint=payload.api_endpoint,
            api_key_envelope_ref=payload.api_key_envelope_ref,
            default_model=payload.default_model,
            timeout_s=payload.timeout_s,
            requests_per_minute=payload.requests_per_minute,
            priority=payload.priority,
            is_enabled=payload.is_enabled,
            created_at=now,
            updated_at=now,
            updated_by_user_id=ctx.user_id,
        )
        with tenant_agnostic():
            _validate_provider_payload(session, payload)
            session.add(row)
            _commit_or_conflict(session, "provider_constraint_violation")
            session.refresh(row)
        return _provider_response(row, Counter())

    @router.get(
        "/providers/{provider_id}",
        response_model=LlmProviderResponse,
        operation_id="admin.llm.providers.get",
    )
    def get_provider(
        _ctx: _ReadCtx, session: _Db, provider_id: str
    ) -> LlmProviderResponse:
        with tenant_agnostic():
            row = session.get(LlmProvider, provider_id)
            if row is None:
                raise _not_found()
            count = session.scalar(
                select(func.count(LlmProviderModel.id)).where(
                    LlmProviderModel.provider_id == provider_id
                )
            )
        return _provider_response(row, Counter({provider_id: int(count or 0)}))

    @router.put(
        "/providers/{provider_id}",
        response_model=LlmProviderResponse,
        operation_id="admin.llm.providers.update",
    )
    def update_provider(
        ctx: _WriteCtx, session: _Db, provider_id: str, payload: ProviderPayload
    ) -> LlmProviderResponse:
        with tenant_agnostic():
            row = session.get(LlmProvider, provider_id)
            if row is None:
                raise _not_found()
            _validate_provider_payload(session, payload, provider_id=provider_id)
            row.name = payload.name
            row.provider_type = payload.provider_type
            row.api_endpoint = payload.api_endpoint
            row.api_key_envelope_ref = payload.api_key_envelope_ref
            row.default_model = payload.default_model
            row.timeout_s = payload.timeout_s
            row.requests_per_minute = payload.requests_per_minute
            row.priority = payload.priority
            row.is_enabled = payload.is_enabled
            row.updated_at = _now()
            row.updated_by_user_id = ctx.user_id
            _commit_or_conflict(session, "provider_constraint_violation")
            session.refresh(row)
            count = session.scalar(
                select(func.count(LlmProviderModel.id)).where(
                    LlmProviderModel.provider_id == provider_id
                )
            )
        return _provider_response(row, Counter({provider_id: int(count or 0)}))

    @router.delete(
        "/providers/{provider_id}",
        status_code=204,
        operation_id="admin.llm.providers.delete",
    )
    def delete_provider(_ctx: _WriteCtx, session: _Db, provider_id: str) -> None:
        with tenant_agnostic():
            row = session.get(LlmProvider, provider_id)
            if row is None:
                raise _not_found()
            references = session.scalar(
                select(func.count(LlmProviderModel.id)).where(
                    LlmProviderModel.provider_id == provider_id
                )
            )
            if references:
                raise _conflict("provider_in_use")
            session.delete(row)
            _commit_or_conflict(session, "provider_constraint_violation")

    @router.get(
        "/models",
        response_model=list[LlmModelResponse],
        operation_id="admin.llm.models.list",
    )
    def list_models(_ctx: _ReadCtx, session: _Db) -> list[LlmModelResponse]:
        with tenant_agnostic():
            provider_models = list(session.scalars(select(LlmProviderModel)).all())
            models = list(
                session.scalars(
                    select(LlmModel).order_by(LlmModel.display_name, LlmModel.id)
                ).all()
            )
        counts: Counter[str] = Counter(row.model_id for row in provider_models)
        return [_model_response(row, counts) for row in models]

    @router.post(
        "/models",
        response_model=LlmModelResponse,
        operation_id="admin.llm.models.create",
    )
    def create_model(
        ctx: _WriteCtx, session: _Db, payload: ModelPayload
    ) -> LlmModelResponse:
        now = _now()
        row = LlmModel(
            id=new_ulid(),
            canonical_name=payload.canonical_name,
            display_name=payload.display_name,
            vendor=payload.vendor,
            capabilities=payload.capabilities,
            context_window=payload.context_window,
            max_output_tokens=payload.max_output_tokens,
            price_source=payload.price_source,
            price_source_model_id=payload.price_source_model_id,
            is_active=payload.is_active,
            notes=payload.notes,
            created_at=now,
            updated_at=now,
            updated_by_user_id=ctx.user_id,
        )
        with tenant_agnostic():
            _validate_model_payload(session, payload)
            session.add(row)
            _commit_or_conflict(session, "model_constraint_violation")
            session.refresh(row)
        return _model_response(row, Counter())

    @router.get(
        "/models/{model_id}",
        response_model=LlmModelResponse,
        operation_id="admin.llm.models.get",
    )
    def get_model(_ctx: _ReadCtx, session: _Db, model_id: str) -> LlmModelResponse:
        with tenant_agnostic():
            row = session.get(LlmModel, model_id)
            if row is None:
                raise _not_found()
            count = session.scalar(
                select(func.count(LlmProviderModel.id)).where(
                    LlmProviderModel.model_id == model_id
                )
            )
        return _model_response(row, Counter({model_id: int(count or 0)}))

    @router.put(
        "/models/{model_id}",
        response_model=LlmModelResponse,
        operation_id="admin.llm.models.update",
    )
    def update_model(
        ctx: _WriteCtx, session: _Db, model_id: str, payload: ModelPayload
    ) -> LlmModelResponse:
        with tenant_agnostic():
            row = session.get(LlmModel, model_id)
            if row is None:
                raise _not_found()
            _validate_model_payload(session, payload, model_id=model_id)
            row.canonical_name = payload.canonical_name
            row.display_name = payload.display_name
            row.vendor = payload.vendor
            row.capabilities = payload.capabilities
            row.context_window = payload.context_window
            row.max_output_tokens = payload.max_output_tokens
            row.price_source = payload.price_source
            row.price_source_model_id = payload.price_source_model_id
            row.is_active = payload.is_active
            row.notes = payload.notes
            row.updated_at = _now()
            row.updated_by_user_id = ctx.user_id
            _commit_or_conflict(session, "model_constraint_violation")
            session.refresh(row)
            count = session.scalar(
                select(func.count(LlmProviderModel.id)).where(
                    LlmProviderModel.model_id == model_id
                )
            )
        return _model_response(row, Counter({model_id: int(count or 0)}))

    @router.delete(
        "/models/{model_id}", status_code=204, operation_id="admin.llm.models.delete"
    )
    def delete_model(_ctx: _WriteCtx, session: _Db, model_id: str) -> None:
        with tenant_agnostic():
            row = session.get(LlmModel, model_id)
            if row is None:
                raise _not_found()
            references = session.scalar(
                select(func.count(LlmProviderModel.id)).where(
                    LlmProviderModel.model_id == model_id
                )
            )
            if references:
                raise _conflict("model_in_use")
            session.delete(row)
            _commit_or_conflict(session, "model_constraint_violation")

    @router.get(
        "/provider-models",
        response_model=list[LlmProviderModelResponse],
        operation_id="admin.llm.provider_models.list",
    )
    def list_provider_models(
        _ctx: _ReadCtx,
        session: _Db,
        provider_id: str | None = Query(default=None),
        model_id: str | None = Query(default=None),
    ) -> list[LlmProviderModelResponse]:
        stmt = select(LlmProviderModel).order_by(
            LlmProviderModel.api_model_id, LlmProviderModel.id
        )
        if provider_id is not None:
            stmt = stmt.where(LlmProviderModel.provider_id == provider_id)
        if model_id is not None:
            stmt = stmt.where(LlmProviderModel.model_id == model_id)
        with tenant_agnostic():
            rows = list(session.scalars(stmt).all())
        return [_provider_model_response(row) for row in rows]

    @router.post(
        "/provider-models",
        response_model=LlmProviderModelResponse,
        operation_id="admin.llm.provider_models.create",
    )
    def create_provider_model(
        _ctx: _WriteCtx, session: _Db, payload: ProviderModelPayload
    ) -> LlmProviderModelResponse:
        now = _now()
        row = LlmProviderModel(
            id=new_ulid(),
            provider_id=payload.provider_id,
            model_id=payload.model_id,
            api_model_id=payload.api_model_id,
            input_cost_per_million=Decimal(str(payload.input_cost_per_million)),
            output_cost_per_million=Decimal(str(payload.output_cost_per_million)),
            max_tokens_override=payload.max_tokens_override,
            temperature_override=payload.temperature_override,
            supports_system_prompt=payload.supports_system_prompt,
            supports_temperature=payload.supports_temperature,
            reasoning_effort=payload.reasoning_effort,
            extra_api_params=payload.extra_api_params,
            price_source_override=payload.price_source_override,
            price_source_model_id_override=payload.price_source_model_id_override,
            is_enabled=payload.is_enabled,
            created_at=now,
            updated_at=now,
        )
        with tenant_agnostic():
            _validate_provider_model_payload(session, payload)
            session.add(row)
            _commit_or_conflict(session, "provider_model_constraint_violation")
            session.refresh(row)
        return _provider_model_response(row)

    @router.get(
        "/provider-models/{provider_model_id}",
        response_model=LlmProviderModelResponse,
        operation_id="admin.llm.provider_models.get",
    )
    def get_provider_model(
        _ctx: _ReadCtx, session: _Db, provider_model_id: str
    ) -> LlmProviderModelResponse:
        with tenant_agnostic():
            row = _provider_model(session, provider_model_id)
        return _provider_model_response(row)

    @router.put(
        "/provider-models/{provider_model_id}",
        response_model=LlmProviderModelResponse,
        operation_id="admin.llm.provider_models.update",
    )
    def update_provider_model(
        _ctx: _WriteCtx,
        session: _Db,
        provider_model_id: str,
        payload: ProviderModelPayload,
    ) -> LlmProviderModelResponse:
        with tenant_agnostic():
            row = _provider_model(session, provider_model_id)
            _validate_provider_model_payload(
                session, payload, provider_model_id=provider_model_id
            )
            row.provider_id = payload.provider_id
            row.model_id = payload.model_id
            row.api_model_id = payload.api_model_id
            row.input_cost_per_million = Decimal(str(payload.input_cost_per_million))
            row.output_cost_per_million = Decimal(str(payload.output_cost_per_million))
            row.max_tokens_override = payload.max_tokens_override
            row.temperature_override = payload.temperature_override
            row.supports_system_prompt = payload.supports_system_prompt
            row.supports_temperature = payload.supports_temperature
            row.reasoning_effort = payload.reasoning_effort
            row.extra_api_params = payload.extra_api_params
            row.price_source_override = payload.price_source_override
            row.price_source_model_id_override = payload.price_source_model_id_override
            row.is_enabled = payload.is_enabled
            row.updated_at = _now()
            _commit_or_conflict(session, "provider_model_constraint_violation")
            session.refresh(row)
        return _provider_model_response(row)

    @router.delete(
        "/provider-models/{provider_model_id}",
        status_code=204,
        operation_id="admin.llm.provider_models.delete",
    )
    def delete_provider_model(
        _ctx: _WriteCtx, session: _Db, provider_model_id: str
    ) -> None:
        with tenant_agnostic():
            row = _provider_model(session, provider_model_id)
            references = session.scalar(
                select(func.count(LlmAssignment.id)).where(
                    LlmAssignment.model_id == provider_model_id
                )
            )
            if references:
                raise _conflict("provider_model_in_use")
            session.delete(row)
            _commit_or_conflict(session, "provider_model_constraint_violation")

    @router.get(
        "/assignments",
        response_model=list[LlmAssignmentResponse],
        operation_id="admin.llm.assignments.list",
    )
    def list_assignments(_ctx: _ReadCtx, session: _Db) -> list[LlmAssignmentResponse]:
        cutoff = _now() - timedelta(days=30)
        with tenant_agnostic():
            rows = list(
                session.scalars(
                    select(LlmAssignment).order_by(
                        LlmAssignment.capability,
                        LlmAssignment.priority,
                        LlmAssignment.id,
                    )
                ).all()
            )
            usage = _assignment_usage(session, cutoff)
        return [_assignment_response(row, usage=usage) for row in rows]

    @router.post(
        "/assignments",
        response_model=LlmAssignmentResponse,
        operation_id="admin.llm.assignments.create",
    )
    def create_assignment(
        ctx: _WriteCtx, session: _Db, request: Request, payload: AssignmentPayload
    ) -> LlmAssignmentResponse:
        now = _now()
        with tenant_agnostic():
            if not _workspace_exists(session, payload.workspace_id):
                raise _not_found()
            provider_model = _provider_model(session, payload.provider_model_id)
            provider = session.get(LlmProvider, provider_model.provider_id)
            required_capabilities = _validate_required_capabilities(
                payload.capability, payload.required_capabilities
            )
            _validate_assignment_priority(
                session,
                workspace_id=payload.workspace_id,
                capability=payload.capability,
                priority=payload.priority,
            )
            _raise_missing_capabilities(
                _missing_capabilities(
                    session,
                    provider_model_id=payload.provider_model_id,
                    required_capabilities=required_capabilities,
                )
            )
            row = LlmAssignment(
                id=new_ulid(),
                workspace_id=payload.workspace_id,
                capability=payload.capability,
                model_id=payload.provider_model_id,
                provider=provider.name
                if provider is not None
                else provider_model.provider_id,
                priority=payload.priority,
                enabled=payload.is_enabled,
                max_tokens=payload.max_tokens,
                temperature=payload.temperature,
                extra_api_params=payload.extra_api_params,
                required_capabilities=required_capabilities,
                created_at=now,
            )
            session.add(row)
            _flush_or_conflict(session, "assignment_constraint_violation")
            _publish_assignment_changed(ctx, request, row.workspace_id)
            _commit_or_conflict(session, "assignment_constraint_violation")
            session.refresh(row)
        return _assignment_response(row, usage={})

    @router.patch(
        "/assignments/reorder",
        response_model=list[LlmAssignmentResponse],
        operation_id="admin.llm.assignments.reorder",
    )
    def reorder_assignments(
        ctx: _WriteCtx,
        session: _Db,
        request: Request,
        payload: list[AssignmentReorderItem],
    ) -> list[LlmAssignmentResponse]:
        changed_workspaces: set[str] = set()
        with tenant_agnostic():
            for group in payload:
                rows = list(
                    session.scalars(
                        select(LlmAssignment).where(
                            LlmAssignment.id.in_(group.ids_in_priority_order),
                            LlmAssignment.capability == group.capability,
                        )
                    ).all()
                )
                by_id = {row.id: row for row in rows}
                if set(by_id) != set(group.ids_in_priority_order):
                    raise HTTPException(
                        status_code=422, detail={"error": "assignment_reorder_mismatch"}
                    )
                workspace_ids = {row.workspace_id for row in rows}
                if len(workspace_ids) != 1:
                    raise HTTPException(
                        status_code=422, detail={"error": "assignment_reorder_mismatch"}
                    )
                workspace_id = next(iter(workspace_ids))
                all_group_ids = set(
                    session.scalars(
                        select(LlmAssignment.id).where(
                            LlmAssignment.workspace_id == workspace_id,
                            LlmAssignment.capability == group.capability,
                        )
                    ).all()
                )
                if all_group_ids != set(group.ids_in_priority_order):
                    raise HTTPException(
                        status_code=422, detail={"error": "assignment_reorder_mismatch"}
                    )
                for priority, assignment_id in enumerate(group.ids_in_priority_order):
                    row = by_id[assignment_id]
                    row.priority = priority
                    changed_workspaces.add(row.workspace_id)
            _flush_or_conflict(session, "assignment_constraint_violation")
            for workspace_id in changed_workspaces:
                _publish_assignment_changed(ctx, request, workspace_id)
            _commit_or_conflict(session, "assignment_constraint_violation")
            all_rows = list(
                session.scalars(
                    select(LlmAssignment).order_by(
                        LlmAssignment.capability,
                        LlmAssignment.priority,
                        LlmAssignment.id,
                    )
                ).all()
            )
        return [_assignment_response(row, usage={}) for row in all_rows]

    @router.get(
        "/assignments/{assignment_id}",
        response_model=LlmAssignmentResponse,
        operation_id="admin.llm.assignments.get",
    )
    def get_assignment(
        _ctx: _ReadCtx, session: _Db, assignment_id: str
    ) -> LlmAssignmentResponse:
        cutoff = _now() - timedelta(days=30)
        with tenant_agnostic():
            row = _assignment(session, assignment_id)
            usage = _assignment_usage(session, cutoff)
        return _assignment_response(row, usage=usage)

    @router.put(
        "/assignments/{assignment_id}",
        response_model=LlmAssignmentResponse,
        operation_id="admin.llm.assignments.update",
    )
    def update_assignment(
        ctx: _WriteCtx,
        session: _Db,
        request: Request,
        assignment_id: str,
        payload: AssignmentUpdatePayload,
    ) -> LlmAssignmentResponse:
        with tenant_agnostic():
            row = _assignment(session, assignment_id)
            sent = payload.model_fields_set
            required_capabilities = _validate_required_capabilities(
                row.capability, payload.required_capabilities
            )
            if "priority" in sent:
                if payload.priority is None:
                    raise _unprocessable("priority_required")
                _validate_assignment_priority(
                    session,
                    workspace_id=row.workspace_id,
                    capability=row.capability,
                    priority=payload.priority,
                    assignment_id=row.id,
                )
                row.priority = payload.priority
            if "provider_model_id" in sent:
                if payload.provider_model_id is None:
                    raise _unprocessable("provider_model_id_required")
                provider_model = _provider_model(session, payload.provider_model_id)
                provider = session.get(LlmProvider, provider_model.provider_id)
                _raise_missing_capabilities(
                    _missing_capabilities(
                        session,
                        provider_model_id=payload.provider_model_id,
                        required_capabilities=required_capabilities,
                    )
                )
                row.model_id = payload.provider_model_id
                row.provider = (
                    provider.name
                    if provider is not None
                    else provider_model.provider_id
                )
            elif "required_capabilities" in sent:
                _raise_missing_capabilities(
                    _missing_capabilities(
                        session,
                        provider_model_id=row.model_id,
                        required_capabilities=required_capabilities,
                    )
                )
            if "max_tokens" in sent:
                row.max_tokens = payload.max_tokens
            if "temperature" in sent:
                row.temperature = payload.temperature
            if "extra_api_params" in sent:
                row.extra_api_params = payload.extra_api_params or {}
            if "required_capabilities" in sent or "provider_model_id" in sent:
                row.required_capabilities = required_capabilities
            if "is_enabled" in sent:
                if payload.is_enabled is None:
                    raise _unprocessable("is_enabled_required")
                row.enabled = payload.is_enabled
            _flush_or_conflict(session, "assignment_constraint_violation")
            workspace_id = row.workspace_id
            _publish_assignment_changed(ctx, request, workspace_id)
            _commit_or_conflict(session, "assignment_constraint_violation")
            session.refresh(row)
        return _assignment_response(row, usage={})

    @router.delete(
        "/assignments/{assignment_id}",
        status_code=204,
        operation_id="admin.llm.assignments.delete",
    )
    def delete_assignment(
        ctx: _WriteCtx,
        session: _Db,
        request: Request,
        assignment_id: str,
    ) -> None:
        with tenant_agnostic():
            row = _assignment(session, assignment_id)
            workspace_id = row.workspace_id
            session.delete(row)
            _flush_or_conflict(session, "assignment_constraint_violation")
            _publish_assignment_changed(ctx, request, workspace_id)
            _commit_or_conflict(session, "assignment_constraint_violation")

    @router.get(
        "/prompts",
        response_model=list[LlmPromptTemplateResponse],
        operation_id="admin.llm.prompts.list",
    )
    def list_prompts(_ctx: _ReadCtx, session: _Db) -> list[LlmPromptTemplateResponse]:
        with tenant_agnostic():
            rows = list(
                session.scalars(
                    select(LlmPromptTemplate)
                    .where(LlmPromptTemplate.is_active.is_(True))
                    .order_by(LlmPromptTemplate.capability)
                ).all()
            )
            revision_count_rows = session.execute(
                select(
                    LlmPromptTemplateRevision.template_id,
                    func.count(LlmPromptTemplateRevision.id),
                ).group_by(LlmPromptTemplateRevision.template_id)
            ).all()
            revision_counts: dict[str, int] = {
                template_id: int(count or 0)
                for template_id, count in revision_count_rows
            }
        return [
            _prompt_response(row, int(revision_counts.get(row.id, 0))) for row in rows
        ]

    @router.get(
        "/prompts/{prompt_id}",
        response_model=LlmPromptTemplateDetail,
        operation_id="admin.llm.prompts.get",
    )
    def get_prompt(
        _ctx: _ReadCtx, session: _Db, prompt_id: str
    ) -> LlmPromptTemplateDetail:
        with tenant_agnostic():
            row = session.get(LlmPromptTemplate, prompt_id)
            if row is None:
                raise _not_found()
            count = session.scalar(
                select(func.count(LlmPromptTemplateRevision.id)).where(
                    LlmPromptTemplateRevision.template_id == prompt_id
                )
            )
        base = _prompt_response(row, int(count or 0))
        return LlmPromptTemplateDetail(
            **base.model_dump(), template=row.template, notes=row.notes
        )

    @router.put(
        "/prompts/{prompt_id}",
        response_model=LlmPromptTemplateDetail,
        operation_id="admin.llm.prompts.update",
    )
    def update_prompt(
        ctx: _WriteCtx, session: _Db, prompt_id: str, payload: PromptUpdatePayload
    ) -> LlmPromptTemplateDetail:
        with tenant_agnostic():
            row = session.get(LlmPromptTemplate, prompt_id)
            if row is None:
                raise _not_found()
            revision = LlmPromptTemplateRevision(
                id=new_ulid(),
                template_id=row.id,
                version=row.version,
                body=row.template,
                notes=row.notes,
                created_at=_now(),
                created_by_user_id=ctx.user_id,
            )
            session.add(revision)
            row.template = payload.template
            row.notes = payload.notes
            row.version += 1
            row.updated_at = _now()
            _commit_or_conflict(session, "prompt_constraint_violation")
            session.refresh(row)
            count = session.scalar(
                select(func.count(LlmPromptTemplateRevision.id)).where(
                    LlmPromptTemplateRevision.template_id == prompt_id
                )
            )
        base = _prompt_response(row, int(count or 0))
        return LlmPromptTemplateDetail(
            **base.model_dump(), template=row.template, notes=row.notes
        )

    @router.get(
        "/prompts/{prompt_id}/revisions",
        response_model=list[LlmPromptRevisionResponse],
        operation_id="admin.llm.prompts.revisions",
    )
    def prompt_revisions(
        _ctx: _ReadCtx, session: _Db, prompt_id: str
    ) -> list[LlmPromptRevisionResponse]:
        with tenant_agnostic():
            if session.get(LlmPromptTemplate, prompt_id) is None:
                raise _not_found()
            rows = list(
                session.scalars(
                    select(LlmPromptTemplateRevision)
                    .where(LlmPromptTemplateRevision.template_id == prompt_id)
                    .order_by(LlmPromptTemplateRevision.version.desc())
                ).all()
            )
        return [
            LlmPromptRevisionResponse(
                id=row.id,
                template_id=row.template_id,
                version=row.version,
                body=row.body,
                notes=row.notes,
                created_at=_iso(row.created_at) or "",
                created_by_user_id=row.created_by_user_id,
            )
            for row in rows
        ]

    @router.post(
        "/prompts/{prompt_id}/reset-to-default",
        response_model=LlmPromptTemplateDetail,
        operation_id="admin.llm.prompts.reset",
    )
    def reset_prompt(
        ctx: _WriteCtx, session: _Db, prompt_id: str
    ) -> LlmPromptTemplateDetail:
        with tenant_agnostic():
            row = session.get(LlmPromptTemplate, prompt_id)
            if row is None:
                raise _not_found()
            default = _current_prompt_default(row.capability)
            revision = LlmPromptTemplateRevision(
                id=new_ulid(),
                template_id=row.id,
                version=row.version,
                body=row.template,
                notes=row.notes,
                created_at=_now(),
                created_by_user_id=ctx.user_id,
            )
            session.add(revision)
            row.template = default
            row.default_hash = _hash_body(default)
            row.notes = None
            row.version += 1
            row.updated_at = _now()
            _commit_or_conflict(session, "prompt_constraint_violation")
            session.refresh(row)
            count = session.scalar(
                select(func.count(LlmPromptTemplateRevision.id)).where(
                    LlmPromptTemplateRevision.template_id == prompt_id
                )
            )
        base = _prompt_response(row, int(count or 0))
        return LlmPromptTemplateDetail(
            **base.model_dump(), template=row.template, notes=row.notes
        )

    @router.get(
        "/calls",
        response_model=list[LlmCallResponse],
        operation_id="admin.llm.calls.list",
    )
    def list_calls(
        _ctx: _ReadCtx,
        session: _Db,
        capability: str | None = Query(default=None),
        provider_model_id: str | None = Query(default=None),
        assignment_id: str | None = Query(default=None),
        fallback_attempts_gt: int | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[LlmCallResponse]:
        provider_model_filter_values: tuple[str, ...] = ()
        if provider_model_id is not None:
            with tenant_agnostic():
                provider_model = session.get(LlmProviderModel, provider_model_id)
            provider_model_filter_values = (
                (provider_model_id, provider_model.api_model_id)
                if provider_model is not None
                else (provider_model_id,)
            )
        stmt = (
            select(LlmUsage)
            .order_by(LlmUsage.created_at.desc(), LlmUsage.id.desc())
            .limit(limit)
        )
        if capability is not None:
            stmt = stmt.where(LlmUsage.capability == capability)
        if provider_model_filter_values:
            stmt = stmt.where(
                or_(
                    *(
                        LlmUsage.provider_model_id == value
                        for value in provider_model_filter_values
                    )
                )
            )
        if assignment_id is not None:
            stmt = stmt.where(LlmUsage.assignment_id == assignment_id)
        if fallback_attempts_gt is not None:
            stmt = stmt.where(LlmUsage.fallback_attempts > fallback_attempts_gt)
        with tenant_agnostic():
            rows = list(session.scalars(stmt).all())
            provider_models = list(session.scalars(select(LlmProviderModel)).all())
        provider_model_ids = {row.id for row in provider_models}
        provider_model_ids_by_api_model_id = {
            row.api_model_id: row.id for row in provider_models
        }
        return [
            LlmCallResponse(
                at=_iso(row.created_at) or "",
                capability=row.capability,
                model_id=row.provider_model_id,
                input_tokens=row.tokens_in,
                output_tokens=row.tokens_out,
                cost_cents=row.cost_cents,
                latency_ms=row.latency_ms,
                status=_status(row),
                assignment_id=row.assignment_id,
                provider_model_id=_llm_usage_provider_model_id(
                    row,
                    provider_model_ids=provider_model_ids,
                    provider_model_ids_by_api_model_id=(
                        provider_model_ids_by_api_model_id
                    ),
                ),
                fallback_attempts=row.fallback_attempts,
            )
            for row in rows
        ]

    @router.post(
        "/sync-pricing",
        response_model=LlmSyncPricingResult,
        operation_id="admin.llm.sync_pricing",
    )
    def sync_pricing(_ctx: _WriteCtx, session: _Db) -> LlmSyncPricingResult:
        started_at = _now()
        with tenant_agnostic():
            rows = list(
                session.scalars(
                    select(LlmProviderModel).order_by(
                        LlmProviderModel.api_model_id, LlmProviderModel.id
                    )
                ).all()
            )
        deltas = [
            LlmSyncPricingDelta(
                provider_model_id=row.id,
                api_model_id=row.api_model_id,
                input_before=_money(row.input_cost_per_million),
                input_after=_money(row.input_cost_per_million),
                output_before=_money(row.output_cost_per_million),
                output_after=_money(row.output_cost_per_million),
                status="pinned" if row.price_source_override == "none" else "unchanged",
            )
            for row in rows
        ]
        return LlmSyncPricingResult(
            started_at=_iso(started_at) or "",
            deltas=deltas,
            updated=0,
            skipped=len(deltas),
            errors=0,
        )

    return router
