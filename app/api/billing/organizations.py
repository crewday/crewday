"""Billing organization HTTP routes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.adapters.db.billing.repositories import SqlAlchemyOrganizationRepository
from app.api.deps import current_workspace_context, db_session
from app.authz.dep import Permission
from app.domain.billing.organizations import (
    OrganizationCreate,
    OrganizationInvalid,
    OrganizationNotFound,
    OrganizationPatch,
    OrganizationService,
    OrganizationView,
)
from app.domain.errors import DomainError, Internal, NotFound, Validation
from app.tenancy import WorkspaceContext

__all__ = [
    "OrganizationCreateRequest",
    "OrganizationListResponse",
    "OrganizationPatchRequest",
    "OrganizationResponse",
    "build_organizations_router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


class OrganizationResponse(BaseModel):
    id: str
    workspace_id: str
    kind: str
    display_name: str
    billing_address: dict[str, object]
    tax_id: str | None
    default_currency: str
    contact_email: str | None
    contact_phone: str | None
    notes_md: str | None
    created_at: datetime
    archived_at: datetime | None

    @classmethod
    def from_view(cls, view: OrganizationView) -> OrganizationResponse:
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            kind=view.kind,
            display_name=view.display_name,
            billing_address=dict(view.billing_address),
            tax_id=view.tax_id,
            default_currency=view.default_currency,
            contact_email=view.contact_email,
            contact_phone=view.contact_phone,
            notes_md=view.notes_md,
            created_at=view.created_at,
            archived_at=view.archived_at,
        )


class OrganizationListResponse(BaseModel):
    data: list[OrganizationResponse]


class OrganizationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["client", "vendor", "mixed"]
    display_name: str = Field(min_length=1, max_length=200)
    billing_address: dict[str, object] = Field(default_factory=dict)
    tax_id: str | None = Field(default=None, max_length=128)
    default_currency: str | None = Field(default=None, min_length=3, max_length=3)
    contact_email: str | None = Field(default=None, max_length=320)
    contact_phone: str | None = Field(default=None, max_length=64)
    notes_md: str | None = None

    def to_domain(self) -> OrganizationCreate:
        return OrganizationCreate(
            kind=self.kind,
            display_name=self.display_name,
            billing_address=self.billing_address,
            tax_id=self.tax_id,
            default_currency=self.default_currency,
            contact_email=self.contact_email,
            contact_phone=self.contact_phone,
            notes_md=self.notes_md,
        )


class OrganizationPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["client", "vendor", "mixed"] | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    billing_address: dict[str, object] | None = None
    tax_id: str | None = Field(default=None, max_length=128)
    default_currency: str | None = Field(default=None, min_length=3, max_length=3)
    contact_email: str | None = Field(default=None, max_length=320)
    contact_phone: str | None = Field(default=None, max_length=64)
    notes_md: str | None = None

    @model_validator(mode="after")
    def _has_mutation(self) -> OrganizationPatchRequest:
        if not self.model_fields_set:
            raise ValueError("PATCH body must include at least one field")
        return self

    def to_domain(self) -> OrganizationPatch:
        fields: dict[str, object | None] = {}
        for field in self.model_fields_set:
            fields[field] = getattr(self, field)
        return OrganizationPatch(fields=fields)


def _http_for_organization_error(exc: Exception) -> DomainError:
    if isinstance(exc, OrganizationNotFound):
        return NotFound(
            str(exc),
            extra={"error": "organization_not_found", "message": str(exc)},
        )
    if isinstance(exc, OrganizationInvalid):
        return Validation(
            str(exc),
            extra={"error": "organization_invalid", "message": str(exc)},
        )
    return Internal(extra={"error": "internal"})


def build_organizations_router() -> APIRouter:
    router = APIRouter(prefix="/organizations", tags=["billing", "organizations"])

    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))
    create_gate = Depends(Permission("organizations.create", scope_kind="workspace"))
    edit_gate = Depends(Permission("organizations.edit", scope_kind="workspace"))

    @router.get(
        "",
        response_model=OrganizationListResponse,
        operation_id="billing.organizations.list",
        dependencies=[view_gate],
        summary="List billing organizations",
    )
    def list_organizations(
        ctx: _Ctx,
        session: _Db,
        kind: Literal["client", "vendor", "mixed"] | None = None,
        q: str | None = None,
        include_archived: bool = False,
    ) -> OrganizationListResponse:
        views = OrganizationService(ctx).list(
            SqlAlchemyOrganizationRepository(session),
            kind=kind,
            search=q,
            include_archived=include_archived,
        )
        return OrganizationListResponse(
            data=[OrganizationResponse.from_view(view) for view in views]
        )

    @router.post(
        "",
        response_model=OrganizationResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="billing.organizations.create",
        dependencies=[create_gate],
        summary="Create a billing organization",
    )
    def create_organization(
        body: OrganizationCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> OrganizationResponse:
        try:
            view = OrganizationService(ctx).create(
                SqlAlchemyOrganizationRepository(session),
                body.to_domain(),
            )
        except OrganizationInvalid as exc:
            raise _http_for_organization_error(exc) from exc
        return OrganizationResponse.from_view(view)

    @router.get(
        "/{organization_id}",
        response_model=OrganizationResponse,
        operation_id="billing.organizations.get",
        dependencies=[view_gate],
        summary="Get a billing organization",
    )
    def get_organization(
        organization_id: str,
        ctx: _Ctx,
        session: _Db,
        include_archived: bool = False,
    ) -> OrganizationResponse:
        try:
            view = OrganizationService(ctx).get(
                SqlAlchemyOrganizationRepository(session),
                organization_id,
                include_archived=include_archived,
            )
        except OrganizationNotFound as exc:
            raise _http_for_organization_error(exc) from exc
        return OrganizationResponse.from_view(view)

    @router.patch(
        "/{organization_id}",
        response_model=OrganizationResponse,
        operation_id="billing.organizations.update",
        dependencies=[edit_gate],
        summary="Update a billing organization",
    )
    def patch_organization(
        organization_id: str,
        body: OrganizationPatchRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> OrganizationResponse:
        try:
            view = OrganizationService(ctx).update(
                SqlAlchemyOrganizationRepository(session),
                organization_id,
                body.to_domain(),
            )
        except (OrganizationInvalid, OrganizationNotFound) as exc:
            raise _http_for_organization_error(exc) from exc
        return OrganizationResponse.from_view(view)

    @router.post(
        "/{organization_id}/archive",
        response_model=OrganizationResponse,
        operation_id="billing.organizations.archive",
        dependencies=[edit_gate],
        summary="Archive a billing organization",
    )
    def archive_organization(
        organization_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> OrganizationResponse:
        try:
            view = OrganizationService(ctx).archive(
                SqlAlchemyOrganizationRepository(session),
                organization_id,
            )
        except OrganizationNotFound as exc:
            raise _http_for_organization_error(exc) from exc
        return OrganizationResponse.from_view(view)

    return router
