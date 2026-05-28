"""Deployment-admin system-doc read routes."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from math import ceil
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import AgentDoc, AgentDocRevision
from app.api.admin.deps import current_deployment_admin_principal
from app.api.deps import db_session
from app.domain.errors import Conflict, NotFound
from app.services.agent.system_docs import (
    agent_doc_metadata_hash,
    get_agent_doc,
    get_agent_doc_default,
    list_agent_docs,
    normalize_agent_doc_roles,
)
from app.tenancy import DeploymentContext
from app.util.ulid import new_ulid

__all__ = [
    "AdminAgentDoc",
    "AdminAgentDocSummary",
    "build_admin_agent_docs_router",
]

_Db = Annotated[Session, Depends(db_session)]
_AdminCtx = Annotated[DeploymentContext, Depends(current_deployment_admin_principal)]


class AdminAgentDocSummary(BaseModel):
    """Summary row for ``GET /admin/api/v1/agent_docs``."""

    slug: str
    title: str
    summary: str
    roles: list[str]
    updated_at: datetime
    version: int
    is_customised: bool
    default_hash: str
    metadata_default_hash: str
    approx_token_count: int


class AdminAgentDoc(AdminAgentDocSummary):
    """Full document body for ``GET /admin/api/v1/agent_docs/{slug}``."""

    body_md: str
    capabilities: list[str]
    notes: str | None


class AdminAgentDocUpdate(BaseModel):
    """Complete editable slice for ``PUT /admin/api/v1/agent_docs/{slug}``."""

    model_config = ConfigDict(extra="forbid")

    body_md: str = Field(min_length=1)
    roles: list[str] = Field(min_length=1)
    notes: str | None = None

    @field_validator("body_md")
    @classmethod
    def _validate_body(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("body_md must not be blank")
        return value

    @field_validator("roles")
    @classmethod
    def _validate_roles(cls, value: list[str]) -> list[str]:
        try:
            return list(normalize_agent_doc_roles(value))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class AdminAgentDocReset(BaseModel):
    """Optional reset note for ``POST /reset-to-default``."""

    model_config = ConfigDict(extra="forbid")

    notes: str | None = None


class AdminAgentDocRevision(BaseModel):
    """One previous agent-doc body snapshot."""

    version: int
    body_md: str
    roles: list[str]
    notes: str | None
    approx_token_count: int
    created_at: datetime
    created_by_user_id: str | None


def build_admin_agent_docs_router() -> APIRouter:
    """Return the deployment-admin system-doc read router."""
    router = APIRouter(prefix="/agent_docs", tags=["admin", "agent_docs"])

    @router.get(
        "",
        response_model=list[AdminAgentDocSummary],
        operation_id="admin.agent_docs.list",
        summary="List deployment agent system docs",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "agent-docs-list",
                "summary": "List agent system docs",
                "mutates": False,
            },
        },
    )
    def list_docs(_ctx: _AdminCtx, session: _Db) -> list[AdminAgentDocSummary]:
        return [_summary(row) for row in list_agent_docs(session)]

    @router.get(
        "/{slug}",
        response_model=AdminAgentDoc,
        operation_id="admin.agent_docs.show",
        summary="Show a deployment agent system doc",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "agent-docs-show",
                "summary": "Show an agent system doc",
                "mutates": False,
            },
        },
    )
    def show_doc(
        slug: str,
        _ctx: _AdminCtx,
        session: _Db,
    ) -> AdminAgentDoc:
        row = get_agent_doc(session, slug)
        if row is None:
            raise NotFound(extra={"error": "not_found"})
        summary = _summary(row)
        return AdminAgentDoc(
            **summary.model_dump(),
            body_md=row.body_md,
            capabilities=list(row.capabilities),
            notes=row.notes,
        )

    @router.put(
        "/{slug}",
        response_model=AdminAgentDoc,
        operation_id="admin.agent_docs.update",
        summary="Update a deployment agent system doc",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "agent-docs-edit",
                "summary": "Update an agent system doc",
                "mutates": True,
            },
        },
    )
    def update_doc(
        slug: str,
        ctx: _AdminCtx,
        session: _Db,
        payload: AdminAgentDocUpdate,
    ) -> AdminAgentDoc:
        row = get_agent_doc(session, slug)
        if row is None:
            raise NotFound(extra={"error": "not_found"})
        _snapshot_revision(session, row, created_by_user_id=ctx.user_id)
        row.body_md = payload.body_md
        row.roles = payload.roles
        row.notes = _clean_note(payload.notes)
        row.version += 1
        row.updated_at = _now()
        _commit_or_conflict(session)
        session.refresh(row)
        return _detail(row)

    @router.get(
        "/{slug}/revisions",
        response_model=list[AdminAgentDocRevision],
        operation_id="admin.agent_docs.revisions",
        summary="List deployment agent system doc revisions",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "agent-docs-revisions",
                "summary": "List agent system doc revisions",
                "mutates": False,
            },
        },
    )
    def list_revisions(
        slug: str,
        _ctx: _AdminCtx,
        session: _Db,
    ) -> list[AdminAgentDocRevision]:
        row = get_agent_doc(session, slug)
        if row is None:
            raise NotFound(extra={"error": "not_found"})
        rows = list(
            session.scalars(
                select(AgentDocRevision)
                .where(AgentDocRevision.doc_id == row.id)
                .order_by(AgentDocRevision.version.desc())
            ).all()
        )
        return [_revision(revision) for revision in rows]

    @router.post(
        "/{slug}/reset-to-default",
        response_model=AdminAgentDoc,
        operation_id="admin.agent_docs.reset",
        summary="Reset a deployment agent system doc to its code default",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "agent-docs-reset-to-default",
                "summary": "Reset an agent system doc to its default",
                "mutates": True,
            },
        },
    )
    def reset_doc(
        slug: str,
        ctx: _AdminCtx,
        session: _Db,
        payload: AdminAgentDocReset | None = None,
    ) -> AdminAgentDoc:
        row = get_agent_doc(session, slug)
        seed = get_agent_doc_default(slug)
        if row is None or seed is None:
            raise NotFound(extra={"error": "not_found"})
        _snapshot_revision(
            session,
            row,
            created_by_user_id=ctx.user_id,
            notes_override=_clean_note(payload.notes) if payload is not None else None,
        )
        row.body_md = seed.body_md
        row.roles = list(seed.roles)
        row.title = seed.title
        row.summary = seed.summary
        row.capabilities = list(seed.capabilities)
        row.default_hash = seed.default_hash
        row.metadata_default_hash = seed.metadata_default_hash
        row.notes = None
        row.version += 1
        row.updated_at = _now()
        _commit_or_conflict(session)
        session.refresh(row)
        return _detail(row)

    return router


def _detail(row: AgentDoc) -> AdminAgentDoc:
    summary = _summary(row)
    return AdminAgentDoc(
        **summary.model_dump(),
        body_md=row.body_md,
        capabilities=list(row.capabilities),
        notes=row.notes,
    )


def _summary(row: AgentDoc) -> AdminAgentDocSummary:
    return AdminAgentDocSummary(
        slug=row.slug,
        title=row.title,
        summary=row.summary or "",
        roles=list(row.roles),
        updated_at=row.updated_at,
        version=row.version,
        is_customised=_is_customised(row),
        default_hash=row.default_hash,
        metadata_default_hash=row.metadata_default_hash,
        approx_token_count=_approx_token_count(row.body_md),
    )


def _revision(row: AgentDocRevision) -> AdminAgentDocRevision:
    return AdminAgentDocRevision(
        version=row.version,
        body_md=row.body_md,
        roles=list(row.roles),
        notes=row.notes,
        approx_token_count=_approx_token_count(row.body_md),
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
    )


def _snapshot_revision(
    session: Session,
    row: AgentDoc,
    *,
    created_by_user_id: str | None,
    notes_override: str | None = None,
) -> None:
    session.add(
        AgentDocRevision(
            id=new_ulid(),
            doc_id=row.id,
            version=row.version,
            body_md=row.body_md,
            roles=list(row.roles),
            notes=row.notes if notes_override is None else notes_override,
            created_at=_now(),
            created_by_user_id=created_by_user_id,
        )
    )


def _is_customised(row: AgentDoc) -> bool:
    return (
        _body_hash(row.body_md) != row.default_hash
        or agent_doc_metadata_hash(row.roles) != row.metadata_default_hash
    )


def _body_hash(body_md: str) -> str:
    return sha256(body_md.encode("utf-8")).hexdigest()[:16]


def _approx_token_count(body_md: str) -> int:
    stripped = body_md.strip()
    if not stripped:
        return 0
    return ceil(len(stripped) / 4)


def _clean_note(note: str | None) -> str | None:
    if note is None:
        return None
    stripped = note.strip()
    return stripped or None


def _now() -> datetime:
    return datetime.now(UTC)


def _commit_or_conflict(session: Session) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise Conflict(extra={"error": "agent_doc_constraint_violation"}) from exc
