"""Public-holidays HTTP router — ``/public_holidays``."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    Cursor,
    LimitQuery,
    PageCursorQuery,
    decode_page_cursor,
    encode_page_cursor,
    validate_limit,
)
from app.api.v1._problem_json import IDENTITY_PROBLEM_RESPONSES
from app.authz.dep import Permission
from app.domain.identity.public_holidays import (
    PublicHolidayConflict,
    PublicHolidayCreate,
    PublicHolidayListFilter,
    PublicHolidayNotFound,
    PublicHolidayUpdate,
    PublicHolidayView,
    create_public_holiday,
    delete_public_holiday,
    get_public_holiday,
    list_public_holidays,
    update_public_holiday,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "PublicHolidayCreateRequest",
    "PublicHolidayListResponse",
    "PublicHolidayResponse",
    "PublicHolidayUpdateRequest",
    "build_public_holidays_router",
    "router",
]

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_SchedulingEffect = Literal["block", "allow", "reduced"]
_Recurrence = Literal["annual"]


class PublicHolidayCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=160)
    date: dt.date
    country: str | None = Field(default=None, min_length=2, max_length=2)
    scheduling_effect: _SchedulingEffect
    reduced_starts_local: dt.time | None = None
    reduced_ends_local: dt.time | None = None
    payroll_multiplier: Decimal | None = Field(default=None, ge=Decimal("0"))
    recurrence: _Recurrence | None = None
    notes_md: str | None = Field(default=None, max_length=20_000)


class PublicHolidayUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    date: dt.date | None = None
    country: str | None = Field(default=None, min_length=2, max_length=2)
    scheduling_effect: _SchedulingEffect | None = None
    reduced_starts_local: dt.time | None = None
    reduced_ends_local: dt.time | None = None
    payroll_multiplier: Decimal | None = Field(default=None, ge=Decimal("0"))
    recurrence: _Recurrence | None = None
    notes_md: str | None = Field(default=None, max_length=20_000)


class PublicHolidayResponse(BaseModel):
    id: str
    workspace_id: str
    name: str
    date: dt.date
    country: str | None
    scheduling_effect: str
    reduced_starts_local: dt.time | None
    reduced_ends_local: dt.time | None
    payroll_multiplier: Decimal | None
    recurrence: str | None
    notes_md: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    deleted_at: dt.datetime | None


class PublicHolidayListResponse(BaseModel):
    data: list[PublicHolidayResponse]
    next_cursor: str | None = None
    has_more: bool = False


def _view_to_response(view: PublicHolidayView) -> PublicHolidayResponse:
    return PublicHolidayResponse(
        id=view.id,
        workspace_id=view.workspace_id,
        name=view.name,
        date=view.date,
        country=view.country,
        scheduling_effect=view.scheduling_effect,
        reduced_starts_local=view.reduced_starts_local,
        reduced_ends_local=view.reduced_ends_local,
        payroll_multiplier=view.payroll_multiplier,
        recurrence=view.recurrence,
        notes_md=view.notes_md,
        created_at=view.created_at,
        updated_at=view.updated_at,
        deleted_at=view.deleted_at,
    )


def _not_found(exc: PublicHolidayNotFound) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "public_holiday_not_found"},
    )


def _conflict(exc: PublicHolidayConflict) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "public_holiday_conflict", "message": str(exc)},
    )


def _validation(exc: ValidationError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": "validation", "message": str(exc)},
    )


def _decode_cursor(cursor: str | None) -> tuple[dt.date, str] | None:
    decoded = decode_page_cursor(cursor)
    if decoded is None:
        return None
    if not isinstance(decoded.last_sort_value, str):
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_cursor", "message": "cursor date is malformed"},
        )
    try:
        return dt.date.fromisoformat(decoded.last_sort_value), decoded.last_id_ulid
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_cursor", "message": "cursor date is invalid"},
        ) from exc


def _next_cursor(rows: list[PublicHolidayView], *, has_more: bool) -> str | None:
    if not has_more or not rows:
        return None
    last = rows[-1]
    return encode_page_cursor(
        Cursor(last_sort_value=last.date.isoformat(), last_id_ulid=last.id)
    )


def _filter(
    from_date: dt.date | None,
    to_date: dt.date | None,
    country: str | None,
) -> PublicHolidayListFilter:
    if from_date is not None and to_date is not None and from_date > to_date:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_date_range", "message": "from must be <= to"},
        )
    if country is not None:
        normalized = country.strip().upper()
        if len(normalized) != 2 or not normalized.isalpha():
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "validation",
                    "message": "country must be an ISO-3166-1 alpha-2 code",
                },
            )
        country = normalized
    return PublicHolidayListFilter(
        starts_on=from_date, ends_on=to_date, country=country
    )


def build_public_holidays_router() -> APIRouter:
    api = APIRouter(
        prefix="/public_holidays",
        tags=["identity", "public_holidays"],
        responses=IDENTITY_PROBLEM_RESPONSES,
    )

    manage_gate = Depends(Permission("work_roles.manage", scope_kind="workspace"))

    @api.get(
        "",
        response_model=PublicHolidayListResponse,
        operation_id="public_holidays.list",
        summary="List public holidays in the caller's workspace",
        dependencies=[manage_gate],
    )
    def list_(
        ctx: _Ctx,
        session: _Db,
        from_date: Annotated[dt.date | None, Query(alias="from")] = None,
        to_date: Annotated[dt.date | None, Query(alias="to")] = None,
        country: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> PublicHolidayListResponse:
        validated_limit = validate_limit(limit)
        rows = list(
            list_public_holidays(
                session,
                ctx,
                filters=_filter(from_date, to_date, country),
                limit=validated_limit,
                after=_decode_cursor(cursor),
            )
        )
        has_more = len(rows) > validated_limit
        page_rows = rows[:validated_limit]
        return PublicHolidayListResponse(
            data=[_view_to_response(view) for view in page_rows],
            next_cursor=_next_cursor(page_rows, has_more=has_more),
            has_more=has_more,
        )

    @api.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=PublicHolidayResponse,
        operation_id="public_holidays.create",
        summary="Create a public holiday",
        dependencies=[manage_gate],
    )
    def create(
        body: PublicHolidayCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> PublicHolidayResponse:
        try:
            service_body = PublicHolidayCreate.model_validate(body.model_dump())
        except ValidationError as exc:
            raise _validation(exc) from exc
        try:
            view = create_public_holiday(session, ctx, body=service_body)
        except PublicHolidayConflict as exc:
            raise _conflict(exc) from exc
        return _view_to_response(view)

    @api.get(
        "/{public_holiday_id}",
        response_model=PublicHolidayResponse,
        operation_id="public_holidays.read",
        summary="Read a public holiday",
        dependencies=[manage_gate],
    )
    def read(
        public_holiday_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> PublicHolidayResponse:
        try:
            view = get_public_holiday(session, ctx, public_holiday_id=public_holiday_id)
        except PublicHolidayNotFound as exc:
            raise _not_found(exc) from exc
        return _view_to_response(view)

    @api.patch(
        "/{public_holiday_id}",
        response_model=PublicHolidayResponse,
        operation_id="public_holidays.update",
        summary="Update a public holiday",
        dependencies=[manage_gate],
    )
    def update(
        public_holiday_id: str,
        body: PublicHolidayUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> PublicHolidayResponse:
        try:
            service_body = PublicHolidayUpdate.model_validate(
                {field: getattr(body, field) for field in body.model_fields_set}
            )
        except ValidationError as exc:
            raise _validation(exc) from exc
        try:
            view = update_public_holiday(
                session, ctx, public_holiday_id=public_holiday_id, body=service_body
            )
        except PublicHolidayNotFound as exc:
            raise _not_found(exc) from exc
        except PublicHolidayConflict as exc:
            raise _conflict(exc) from exc
        return _view_to_response(view)

    @api.delete(
        "/{public_holiday_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="public_holidays.delete",
        summary="Soft-delete a public holiday",
        dependencies=[manage_gate],
    )
    def delete(
        public_holiday_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        try:
            delete_public_holiday(session, ctx, public_holiday_id=public_holiday_id)
        except PublicHolidayNotFound as exc:
            raise _not_found(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return api


router = build_public_holidays_router()
