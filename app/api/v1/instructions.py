"""Instructions KB HTTP router."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.instructions.models import Instruction, InstructionVersion
from app.adapters.db.instructions.repositories import SqlAlchemyInstructionsRepository
from app.adapters.db.places.models import Area
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    encode_cursor,
)
from app.events import InstructionArchived, InstructionCreated, InstructionUpdated
from app.events.bus import bus as default_event_bus
from app.services.instructions import service
from app.tenancy import WorkspaceContext
from app.util.clock import SystemClock

router = APIRouter(tags=["instructions"])

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_MaybeId = Annotated[str | None, Query(max_length=64)]
_Scope = Literal["global", "property", "area"]
_InstructionEventType = (
    type[InstructionArchived] | type[InstructionCreated] | type[InstructionUpdated]
)


class InstructionPayload(BaseModel):
    id: str
    workspace_id: str
    slug: str
    title: str
    scope: _Scope
    property_id: str | None
    area_id: str | None
    current_revision_id: str | None
    tags: tuple[str, ...]
    archived_at: datetime | None
    created_by: str | None
    created_at: datetime

    @classmethod
    def from_view(cls, view: service.InstructionView) -> InstructionPayload:
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            slug=view.slug,
            title=view.title,
            scope=view.scope,
            property_id=view.property_id,
            area_id=view.area_id,
            current_revision_id=view.current_version_id,
            tags=view.tags,
            archived_at=view.archived_at,
            created_by=view.created_by,
            created_at=view.created_at,
        )


class InstructionRevisionPayload(BaseModel):
    id: str
    instruction_id: str
    version: int
    body_md: str
    body_hash: str
    author_id: str | None
    change_note: str | None
    created_at: datetime

    @classmethod
    def from_view(
        cls, view: service.InstructionVersionView
    ) -> InstructionRevisionPayload:
        return cls(
            id=view.id,
            instruction_id=view.instruction_id,
            version=view.version_num,
            body_md=view.body_md,
            body_hash=view.body_hash,
            author_id=view.author_id,
            change_note=view.change_note,
            created_at=view.created_at,
        )

    @classmethod
    def from_row(cls, row: InstructionVersion) -> InstructionRevisionPayload:
        return cls(
            id=row.id,
            instruction_id=row.instruction_id,
            version=row.version_num,
            body_md=row.body_md,
            body_hash=row.body_hash,
            author_id=row.author_id,
            change_note=row.change_note,
            created_at=row.created_at,
        )


class InstructionListItemPayload(InstructionPayload):
    body_md: str
    version: int
    updated_at: datetime
    area: str | None

    @classmethod
    def from_views(
        cls,
        instruction: service.InstructionView,
        revision: InstructionRevisionPayload,
        *,
        area: str | None = None,
    ) -> InstructionListItemPayload:
        return cls(
            **InstructionPayload.from_view(instruction).model_dump(),
            body_md=revision.body_md,
            version=revision.version,
            updated_at=revision.created_at,
            area=area,
        )


class InstructionWithRevisionPayload(BaseModel):
    instruction: InstructionPayload
    current_revision: InstructionRevisionPayload

    @classmethod
    def from_result(
        cls, result: service.InstructionResult
    ) -> InstructionWithRevisionPayload:
        return cls(
            instruction=InstructionPayload.from_view(result.instruction),
            current_revision=InstructionRevisionPayload.from_view(result.revision),
        )


class InstructionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    body_md: str = Field(default="", max_length=200_000)
    scope: _Scope = "global"
    property_id: str | None = Field(default=None, max_length=64)
    area_id: str | None = Field(default=None, max_length=64)
    tags: tuple[str, ...] = ()
    change_note: str | None = Field(default=None, max_length=1024)


class InstructionPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=256)
    body_md: str | None = Field(default=None, max_length=200_000)
    scope: _Scope | None = None
    property_id: str | None = Field(default=None, max_length=64)
    area_id: str | None = Field(default=None, max_length=64)
    tags: tuple[str, ...] | None = None
    change_note: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def reject_null_updates(self) -> Self:
        for field in ("title", "body_md", "scope", "tags"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} may not be null")
        return self


class InstructionListResponse(BaseModel):
    data: tuple[InstructionListItemPayload, ...]
    next_cursor: str | None
    has_more: bool


class InstructionRevisionListResponse(BaseModel):
    data: tuple[InstructionRevisionPayload, ...]
    next_cursor: str | None
    has_more: bool


class ResolvedInstructionPayload(BaseModel):
    instruction_id: str
    current_revision_id: str
    body_md: str
    provenance: service.InstructionProvenance

    @classmethod
    def from_view(cls, view: service.ResolvedInstruction) -> ResolvedInstructionPayload:
        return cls(
            instruction_id=view.instruction_id,
            current_revision_id=view.current_revision_id,
            body_md=view.body_md,
            provenance=view.provenance,
        )


class ResolvedInstructionListResponse(BaseModel):
    data: tuple[ResolvedInstructionPayload, ...]
    next_cursor: None = None
    has_more: Literal[False] = False


def _repo(session: Session) -> SqlAlchemyInstructionsRepository:
    return SqlAlchemyInstructionsRepository(session)


def _publish_instruction_event(
    ctx: WorkspaceContext,
    event_type: _InstructionEventType,
    instruction_id: str,
) -> None:
    default_event_bus.publish(
        event_type(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=SystemClock().now(),
            instruction_id=instruction_id,
        )
    )


def _http_error(
    status_code: int, error: str, *, message: str | None = None
) -> HTTPException:
    detail: dict[str, object] = {"error": error}
    if message is not None:
        detail["message"] = message
    return HTTPException(status_code=status_code, detail=detail)


def _http_for_service_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, service.InstructionNotFound):
        return _http_error(status.HTTP_404_NOT_FOUND, "instruction_not_found")
    if isinstance(exc, service.InstructionPermissionDenied):
        return _http_error(status.HTTP_403_FORBIDDEN, "forbidden")
    if isinstance(exc, service.ArchivedInstructionError):
        return _http_error(
            status.HTTP_409_CONFLICT,
            "instruction_archived",
            message=str(exc),
        )
    if isinstance(exc, service.ScopeValidationError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "scope_invalid",
                "message": str(exc),
                "field": exc.field,
            },
        )
    if isinstance(exc, service.TagValidationError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "tags_invalid",
                "message": str(exc),
                "field": exc.field,
                "limit": exc.limit,
            },
        )
    if isinstance(exc, ValueError):
        return _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation",
            message=str(exc),
        )
    return _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal")


def _http_for_integrity_error(session: Session, exc: IntegrityError) -> HTTPException:
    session.rollback()
    message = str(exc.orig).lower()
    if (
        "uq_instruction_workspace_slug" in message
        or "instruction.workspace_id" in message
    ):
        return _http_error(status.HTTP_409_CONFLICT, "instruction_slug_conflict")
    return _http_error(status.HTTP_409_CONFLICT, "instruction_conflict")


def _instruction_view(
    repo: SqlAlchemyInstructionsRepository,
    ctx: WorkspaceContext,
    instruction_id: str,
) -> service.InstructionView:
    row = repo.get_instruction(
        workspace_id=ctx.workspace_id,
        instruction_id=instruction_id,
    )
    if row is None:
        raise _http_for_service_error(service.InstructionNotFound(instruction_id))
    scope: _Scope
    property_id: str | None
    area_id: str | None
    if row.scope_kind == "workspace":
        scope = "global"
        property_id = None
        area_id = None
    elif row.scope_kind == "property":
        scope = "property"
        property_id = row.scope_id
        area_id = None
    elif row.scope_kind == "area":
        scope = "area"
        property_id = row.property_id
        area_id = row.scope_id
    else:
        raise _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "invalid_scope")
    return service.InstructionView(
        id=row.id,
        workspace_id=row.workspace_id,
        slug=row.slug,
        title=row.title,
        scope=scope,
        property_id=property_id,
        area_id=area_id,
        current_version_id=row.current_version_id,
        tags=row.tags,
        archived_at=row.archived_at,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _current_revision(
    repo: SqlAlchemyInstructionsRepository,
    ctx: WorkspaceContext,
    instruction: service.InstructionView,
) -> InstructionRevisionPayload:
    if instruction.current_version_id is None:
        raise _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "missing_revision")
    row = repo.get_version(
        workspace_id=ctx.workspace_id,
        version_id=instruction.current_version_id,
    )
    if row is None:
        raise _http_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "missing_revision")
    return InstructionRevisionPayload.from_view(
        service.InstructionVersionView(
            id=row.id,
            instruction_id=row.instruction_id,
            version_num=row.version_num,
            body_md=row.body_md,
            body_hash=row.body_hash,
            author_id=row.author_id,
            change_note=row.change_note,
            created_at=row.created_at,
        )
    )


@router.get(
    ":scope",
    response_model=ResolvedInstructionListResponse,
    operation_id="instructions.scope",
    summary="Resolve instructions for a work context",
    openapi_extra={
        "x-cli": {
            "group": "instructions",
            "verb": "scope",
            "summary": "Resolve instructions for a work context",
            "mutates": False,
        }
    },
)
def resolve_instruction_scope_route(
    ctx: _Ctx,
    session: _Db,
    template: _MaybeId = None,
    property: _MaybeId = None,
    area: _MaybeId = None,
    asset: _MaybeId = None,
    stay: _MaybeId = None,
    role: _MaybeId = None,
) -> ResolvedInstructionListResponse:
    resolved = service.resolve_instructions(
        _repo(session),
        ctx,
        property_id=property,
        area_id=area,
        template_id=template,
        asset_id=asset,
        stay_id=stay,
        work_role_id=role,
    )
    return ResolvedInstructionListResponse(
        data=tuple(ResolvedInstructionPayload.from_view(row) for row in resolved)
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=InstructionWithRevisionPayload,
    operation_id="instructions.create",
    summary="Create an instruction",
    openapi_extra={
        "x-cli": {
            "group": "instructions",
            "verb": "create",
            "summary": "Create an instruction",
        },
        "x-agent-confirm": {
            "summary": "Create instruction {title}?",
            "verb": "Create instruction",
            "risk": "low",
            "fields_to_show": ["slug", "title", "scope"],
        },
    },
)
def create_instruction_route(
    body: InstructionCreateRequest,
    ctx: _Ctx,
    session: _Db,
) -> InstructionWithRevisionPayload:
    try:
        result = service.create(
            _repo(session),
            ctx,
            slug=body.slug,
            title=body.title,
            body_md=body.body_md,
            scope=body.scope,
            tags=body.tags,
            property_id=body.property_id,
            area_id=body.area_id,
            change_note=body.change_note,
        )
    except IntegrityError as exc:
        raise _http_for_integrity_error(session, exc) from exc
    except Exception as exc:
        raise _http_for_service_error(exc) from exc
    _publish_instruction_event(ctx, InstructionCreated, result.instruction.id)
    return InstructionWithRevisionPayload.from_result(result)


@router.get(
    "",
    response_model=InstructionListResponse,
    operation_id="instructions.list",
    summary="List instructions",
    openapi_extra={
        "x-cli": {
            "group": "instructions",
            "verb": "list",
            "summary": "List instructions",
            "mutates": False,
        }
    },
)
def list_instructions_route(
    ctx: _Ctx,
    session: _Db,
    include_archived: bool = False,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> InstructionListResponse:
    cursor_id = decode_cursor(cursor)
    stmt = select(Instruction).where(Instruction.workspace_id == ctx.workspace_id)
    if not include_archived:
        stmt = stmt.where(Instruction.archived_at.is_(None))
    if cursor_id is not None:
        stmt = stmt.where(Instruction.id > cursor_id)
    stmt = stmt.order_by(Instruction.id.asc()).limit(limit + 1)
    rows = tuple(session.scalars(stmt).all())
    page_rows = rows[:limit]
    repo = _repo(session)
    instructions = tuple(_instruction_view(repo, ctx, row.id) for row in page_rows)
    area_ids = tuple(
        area_id
        for area_id in {instruction.area_id for instruction in instructions}
        if area_id is not None
    )
    area_labels: dict[str, str] = {}
    if area_ids:
        area_rows = session.scalars(
            select(Area).where(Area.id.in_(area_ids), Area.deleted_at.is_(None))
        )
        area_labels = {
            row.id: row.name or row.label
            for row in area_rows
            if row.name is not None or row.label
        }
    data: list[InstructionListItemPayload] = []
    for instruction in instructions:
        data.append(
            InstructionListItemPayload.from_views(
                instruction,
                _current_revision(repo, ctx, instruction),
                area=(
                    area_labels.get(instruction.area_id)
                    if instruction.area_id is not None
                    else None
                ),
            )
        )
    return InstructionListResponse(
        data=tuple(data),
        next_cursor=(
            encode_cursor(page_rows[-1].id) if len(rows) > limit and page_rows else None
        ),
        has_more=len(rows) > limit,
    )


@router.get(
    "/{instruction_id}",
    response_model=InstructionWithRevisionPayload,
    operation_id="instructions.get",
    summary="Read an instruction",
    openapi_extra={
        "x-cli": {
            "group": "instructions",
            "verb": "get",
            "summary": "Read an instruction",
            "mutates": False,
        }
    },
)
def get_instruction_route(
    instruction_id: str,
    ctx: _Ctx,
    session: _Db,
) -> InstructionWithRevisionPayload:
    repo = _repo(session)
    instruction = _instruction_view(repo, ctx, instruction_id)
    return InstructionWithRevisionPayload(
        instruction=InstructionPayload.from_view(instruction),
        current_revision=_current_revision(repo, ctx, instruction),
    )


@router.patch(
    "/{instruction_id}",
    response_model=InstructionWithRevisionPayload,
    operation_id="instructions.patch",
    summary="Patch instruction metadata or body",
    openapi_extra={
        "x-cli": {
            "group": "instructions",
            "verb": "patch",
            "summary": "Patch instruction metadata or body",
        },
        "x-agent-confirm": {
            "summary": "Update instruction?",
            "verb": "Update instruction",
            "risk": "low",
            "fields_to_show": ["title", "scope", "change_note"],
        },
    },
)
def patch_instruction_route(
    instruction_id: str,
    body: InstructionPatchRequest,
    ctx: _Ctx,
    session: _Db,
) -> InstructionWithRevisionPayload:
    repo = _repo(session)
    fields = body.model_fields_set
    mutation_requested = False
    try:
        if {"title", "scope", "property_id", "area_id", "tags"} & fields:
            service.update_metadata(
                repo,
                ctx,
                instruction_id=instruction_id,
                title=body.title if "title" in fields else None,
                tags=body.tags if "tags" in fields else None,
                scope=body.scope if "scope" in fields else None,
                property_id=body.property_id,
                area_id=body.area_id,
            )
            mutation_requested = True
        if "body_md" in fields:
            body_md = body.body_md
            if body_md is None:
                raise _http_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "validation",
                    message="body_md may not be null",
                )
            result = service.update_body(
                repo,
                ctx,
                instruction_id=instruction_id,
                body_md=body_md,
                change_note=body.change_note,
            )
            _publish_instruction_event(ctx, InstructionUpdated, instruction_id)
            return InstructionWithRevisionPayload.from_result(result)
    except IntegrityError as exc:
        raise _http_for_integrity_error(session, exc) from exc
    except Exception as exc:
        raise _http_for_service_error(exc) from exc

    instruction = _instruction_view(repo, ctx, instruction_id)
    if mutation_requested:
        _publish_instruction_event(ctx, InstructionUpdated, instruction_id)
    return InstructionWithRevisionPayload(
        instruction=InstructionPayload.from_view(instruction),
        current_revision=_current_revision(repo, ctx, instruction),
    )


@router.post(
    "/{instruction_id}/archive",
    response_model=InstructionPayload,
    operation_id="instructions.archive",
    summary="Archive an instruction",
    openapi_extra={
        "x-cli": {
            "group": "instructions",
            "verb": "archive",
            "summary": "Archive an instruction",
        },
        "x-agent-confirm": {
            "summary": "Archive instruction?",
            "verb": "Archive instruction",
            "risk": "medium",
        },
    },
)
def archive_instruction_route(
    instruction_id: str,
    ctx: _Ctx,
    session: _Db,
) -> InstructionPayload:
    try:
        view = service.archive(_repo(session), ctx, instruction_id=instruction_id)
    except Exception as exc:
        raise _http_for_service_error(exc) from exc
    _publish_instruction_event(ctx, InstructionArchived, instruction_id)
    return InstructionPayload.from_view(view)


@router.get(
    "/{instruction_id}/versions",
    response_model=InstructionRevisionListResponse,
    operation_id="instructions.versions.list",
    summary="List instruction versions",
    openapi_extra={
        "x-cli": {
            "group": "instructions",
            "verb": "versions",
            "summary": "List instruction versions",
            "mutates": False,
        }
    },
)
def list_instruction_versions_route(
    instruction_id: str,
    ctx: _Ctx,
    session: _Db,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> InstructionRevisionListResponse:
    try:
        page = service.list_revisions(
            _repo(session),
            ctx,
            instruction_id,
            limit=limit,
            cursor=decode_cursor(cursor),
        )
    except Exception as exc:
        raise _http_for_service_error(exc) from exc
    return InstructionRevisionListResponse(
        data=tuple(InstructionRevisionPayload.from_view(row) for row in page.data),
        next_cursor=encode_cursor(page.next_cursor) if page.next_cursor else None,
        has_more=page.has_more,
    )


@router.get(
    "/{instruction_id}/versions/{version}",
    response_model=InstructionRevisionPayload,
    operation_id="instructions.versions.get",
    summary="Read one instruction version",
    openapi_extra={
        "x-cli": {
            "group": "instructions",
            "verb": "version",
            "summary": "Read one instruction version",
            "mutates": False,
        }
    },
)
def get_instruction_version_route(
    instruction_id: str,
    version: Annotated[int, Path(ge=1)],
    ctx: _Ctx,
    session: _Db,
) -> InstructionRevisionPayload:
    repo = _repo(session)
    _instruction_view(repo, ctx, instruction_id)
    row = session.scalars(
        select(InstructionVersion).where(
            InstructionVersion.workspace_id == ctx.workspace_id,
            InstructionVersion.instruction_id == instruction_id,
            InstructionVersion.version_num == version,
        )
    ).one_or_none()
    if row is None:
        raise _http_error(status.HTTP_404_NOT_FOUND, "instruction_version_not_found")
    return InstructionRevisionPayload.from_row(row)


__all__ = ["router"]
