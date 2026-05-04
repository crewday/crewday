"""Issue reporting API routes."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.domain.errors import DomainError, Forbidden, Internal, NotFound
from app.domain.errors import Validation as DomainValidation
from app.domain.issues import (
    IssueAccessDenied,
    IssueCreate,
    IssueNotFound,
    IssueUpdate,
    IssueValidationError,
    IssueView,
    create_issue,
    get_issue,
    list_issues,
    update_issue,
)
from app.tenancy import WorkspaceContext

router = APIRouter(tags=["issues"])

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


class IssueCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=200)
    severity: Literal["low", "normal", "high", "urgent"] = "normal"
    category: Literal["damage", "broken", "supplies", "safety", "other"] = "other"
    property_id: str
    area_id: str | None = None
    area: str | None = Field(default=None, max_length=200)
    body: str = Field(default="", max_length=20_000)
    task_id: str | None = None
    attachment_file_ids: list[str] = Field(default_factory=list, max_length=20)

    def to_domain(self) -> IssueCreate:
        return IssueCreate.model_validate(self.model_dump())


class IssueUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    severity: Literal["low", "normal", "high", "urgent"] | None = None
    category: Literal["damage", "broken", "supplies", "safety", "other"] | None = None
    state: Literal["open", "in_progress", "resolved", "wont_fix"] | None = None
    status: Literal["open", "in_progress", "resolved", "wont_fix"] | None = None
    body: str | None = Field(default=None, max_length=20_000)
    resolution_note: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def _resolve_aliases(self) -> IssueUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("PATCH body must include at least one field")
        if "state" in self.model_fields_set and "status" in self.model_fields_set:
            raise ValueError("send only one of state or status")
        return self

    def to_domain(self) -> IssueUpdate:
        payload = self.model_dump(exclude_unset=True)
        if "status" in payload:
            payload["state"] = payload.pop("status")
        return IssueUpdate.model_validate(payload)


class IssueResponse(BaseModel):
    id: str
    workspace_id: str
    reported_by_user_id: str
    reported_by: str
    property_id: str
    area_id: str | None
    area: str
    task_id: str | None
    title: str
    description_md: str
    body: str
    severity: str
    category: str
    state: str
    status: str
    attachment_file_ids: list[str]
    converted_to_task_id: str | None
    resolution_note: str | None
    resolved_at: datetime | None
    resolved_by: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    reported_at: datetime

    @classmethod
    def from_view(cls, view: IssueView) -> IssueResponse:
        return cls(**asdict(view))


class IssueListResponse(BaseModel):
    data: list[IssueResponse]


def _domain_error_for_issue_error(exc: Exception) -> DomainError:
    if isinstance(exc, IssueNotFound):
        return NotFound(extra={"error": "issue_not_found"})
    if isinstance(exc, IssueAccessDenied):
        return Forbidden(
            extra={"error": "permission_denied", "action_key": "issues.report"}
        )
    if isinstance(exc, IssueValidationError):
        return DomainValidation(extra={"error": exc.error, "field": exc.field})
    return Internal(extra={"error": "internal"})


@router.get(
    "",
    response_model=IssueListResponse,
    operation_id="issues.list",
    summary="List workspace issues",
)
def list_route(
    ctx: _Ctx,
    session: _Db,
    state: Annotated[
        Literal["open", "in_progress", "resolved", "wont_fix"] | None, Query()
    ] = None,
    property_id: Annotated[str | None, Query()] = None,
) -> IssueListResponse:
    try:
        views = list_issues(session, ctx, state=state, property_id=property_id)
    except (IssueAccessDenied, IssueValidationError) as exc:
        raise _domain_error_for_issue_error(exc) from exc
    return IssueListResponse(data=[IssueResponse.from_view(view) for view in views])


@router.post(
    "",
    response_model=IssueResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="issues.create",
    summary="Report a property issue",
)
def create_route(
    body: IssueCreateRequest,
    ctx: _Ctx,
    session: _Db,
) -> IssueResponse:
    try:
        view = create_issue(session, ctx, body=body.to_domain())
    except (IssueAccessDenied, IssueValidationError) as exc:
        raise _domain_error_for_issue_error(exc) from exc
    return IssueResponse.from_view(view)


@router.patch(
    "/{issue_id}",
    response_model=IssueResponse,
    operation_id="issues.update",
    summary="Update a property issue",
)
def update_route(
    issue_id: str,
    body: IssueUpdateRequest,
    ctx: _Ctx,
    session: _Db,
) -> IssueResponse:
    try:
        view = update_issue(session, ctx, issue_id, body=body.to_domain())
    except (IssueAccessDenied, IssueNotFound, IssueValidationError) as exc:
        raise _domain_error_for_issue_error(exc) from exc
    return IssueResponse.from_view(view)


@router.post(
    "/{issue_id}/convert_to_task",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    operation_id="issues.convert_to_task",
    summary="Convert an issue to a task",
)
def convert_to_task_route(issue_id: str, ctx: _Ctx, session: _Db) -> Response:
    try:
        get_issue(session, ctx, issue_id)
    except (IssueAccessDenied, IssueNotFound) as exc:
        raise _domain_error_for_issue_error(exc) from exc
    return Response(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        media_type="application/json",
        content='{"error":"issue_conversion_unavailable"}',
    )
