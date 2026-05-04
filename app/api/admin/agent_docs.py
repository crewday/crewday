"""Deployment-admin system-doc read routes."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import AgentDoc
from app.api.admin.deps import current_deployment_admin_principal
from app.api.deps import db_session
from app.domain.errors import NotFound
from app.services.agent.system_docs import get_agent_doc, list_agent_docs
from app.tenancy import DeploymentContext

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


class AdminAgentDoc(AdminAgentDocSummary):
    """Full document body for ``GET /admin/api/v1/agent_docs/{slug}``."""

    body_md: str
    capabilities: list[str]


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
        )

    return router


def _summary(row: AgentDoc) -> AdminAgentDocSummary:
    return AdminAgentDocSummary(
        slug=row.slug,
        title=row.title,
        summary=row.summary or "",
        roles=list(row.roles),
        updated_at=row.updated_at,
        version=row.version,
        is_customised=_body_hash(row.body_md) != row.default_hash,
        default_hash=row.default_hash,
    )


def _body_hash(body_md: str) -> str:
    return sha256(body_md.encode("utf-8")).hexdigest()[:16]
