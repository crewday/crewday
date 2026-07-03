"""Workspace booking read router -- ``/bookings``.

Mounted inside ``/w/<slug>/api/v1`` by the app factory. The current
surface is deliberately read-only and flat because the SPA promoted
from the mock calls ``fetchJson<Booking[]>('/api/v1/bookings')`` from
both manager and worker shells.

Managers and owners read the workspace feed through
``bookings.view_other``. Workers use the same URL for their schedule
chrome, but the query is self-scoped by construction and never widens
past ``ctx.actor_id``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.availability.repositories import SqlAlchemyCapabilityChecker
from app.adapters.db.payroll.models import Booking
from app.adapters.db.payroll.repositories import SqlAlchemyBookingWriteRepository
from app.api.deps import current_workspace_context, db_session
from app.api.v1._problem_json import IDENTITY_PROBLEM_RESPONSES
from app.authz import InvalidScope, PermissionDenied, UnknownActionKey, require
from app.domain.errors import Conflict, Forbidden, NotFound, Validation
from app.domain.payroll.booking_write import (
    BookingNotDeclinable,
    BookingNotFound,
    BookingPermissionDenied,
    amend_booking,
    decline_booking,
)
from app.domain.payroll.ports import BookingWriteRow
from app.tenancy import WorkspaceContext
from app.util.clock import SystemClock

__all__ = [
    "BookingAmendRequest",
    "BookingDeclineRequest",
    "BookingResponse",
    "build_bookings_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]

# A single booking spanning more than a week is implausible; the bound
# keeps a fat-fingered amend from persisting an absurd payable total.
_MAX_BOOKING_MINUTES = 7 * 24 * 60
_MAX_REASON_LEN = 20_000


class BookingResponse(BaseModel):
    """Flat booking projection matching ``app/web/src/types/booking.ts``."""

    id: str
    employee_id: str
    user_id: str
    work_engagement_id: str
    property_id: str | None
    client_org_id: str | None
    status: str
    kind: str
    scheduled_start: datetime
    scheduled_end: datetime
    actual_minutes: int | None
    actual_minutes_paid: int | None
    break_seconds: int
    pending_amend_minutes: int | None
    pending_amend_reason: str | None
    declined_at: datetime | None
    declined_reason: str | None
    notes_md: str
    adjusted: bool
    adjustment_reason: str | None


def _reason_not_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("reason must not be blank")
    return stripped


class BookingAmendRequest(BaseModel):
    """Request body for ``POST /bookings/{id}/amend`` (§09 "Amend operation").

    Matches the SPA day-drawer amend dialog, which posts exactly
    ``{actual_minutes, reason}``. ``reason`` is mandatory whenever a
    time field moves (422 ``amend_reason_required`` otherwise) — and
    this endpoint always moves ``actual_minutes`` — so it is required
    at the DTO layer.
    """

    model_config = ConfigDict(extra="forbid")

    actual_minutes: int = Field(ge=0, le=_MAX_BOOKING_MINUTES)
    reason: str = Field(min_length=1, max_length=_MAX_REASON_LEN)

    _strip_reason = field_validator("reason")(_reason_not_blank)


class BookingDeclineRequest(BaseModel):
    """Request body for ``POST /bookings/{id}/decline`` (§09 "Worker decline").

    The SPA decline dialog posts ``{reason}`` and requires a non-blank
    value so the manager sees why the worker refused the slot.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=_MAX_REASON_LEN)

    _strip_reason = field_validator("reason")(_reason_not_blank)


_STRING_QUERY_SCHEMA = {"anyOf": [{"type": "string"}]}
_DATETIME_QUERY_SCHEMA = {"anyOf": [{"type": "string", "format": "date-time"}]}
_BOOLEAN_QUERY_SCHEMA = {"anyOf": [{"type": "boolean"}]}

_UserFilter = Annotated[
    str | None, Query(alias="user_id", json_schema_extra=_STRING_QUERY_SCHEMA)
]
_PropertyFilter = Annotated[
    str | None, Query(alias="property_id", json_schema_extra=_STRING_QUERY_SCHEMA)
]
_FromFilter = Annotated[
    datetime | None, Query(alias="from", json_schema_extra=_DATETIME_QUERY_SCHEMA)
]
_ToFilter = Annotated[
    datetime | None, Query(alias="to", json_schema_extra=_DATETIME_QUERY_SCHEMA)
]
_StatusFilter = Annotated[
    str | None, Query(alias="status", json_schema_extra=_STRING_QUERY_SCHEMA)
]
_PendingAmendFilter = Annotated[
    bool | None, Query(alias="pending_amend", json_schema_extra=_BOOLEAN_QUERY_SCHEMA)
]


def _permission_denied(action_key: str) -> Forbidden:
    return Forbidden(extra={"error": "permission_denied", "action_key": action_key})


def _permission_misconfigured(
    *, error: str, action_key: str, message: str
) -> Validation:
    return Validation(
        message,
        extra={"error": error, "action_key": action_key, "message": message},
    )


def _require_view_other(session: Session, ctx: WorkspaceContext) -> None:
    action_key = "bookings.view_other"
    try:
        require(
            session,
            ctx,
            action_key=action_key,
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
        )
    except PermissionDenied as exc:
        raise _permission_denied(action_key) from exc
    except UnknownActionKey as exc:
        raise _permission_misconfigured(
            error="unknown_action_key",
            action_key=action_key,
            message=str(exc),
        ) from exc
    except InvalidScope as exc:
        raise _permission_misconfigured(
            error="invalid_scope_kind",
            action_key=action_key,
            message=str(exc),
        ) from exc


def _invalid_window() -> Validation:
    message = "to must be on or after from"
    return Validation(
        message,
        extra={"error": "invalid_field", "field": "to", "message": message},
    )


def _booking_to_response(row: Booking) -> BookingResponse:
    return BookingResponse(
        id=row.id,
        employee_id=row.user_id,
        user_id=row.user_id,
        work_engagement_id=row.work_engagement_id,
        property_id=row.property_id,
        client_org_id=row.client_org_id,
        status=row.status,
        kind=row.kind,
        scheduled_start=row.scheduled_start,
        scheduled_end=row.scheduled_end,
        actual_minutes=row.actual_minutes,
        actual_minutes_paid=row.actual_minutes_paid,
        break_seconds=row.break_seconds,
        pending_amend_minutes=row.pending_amend_minutes,
        pending_amend_reason=row.pending_amend_reason,
        declined_at=row.declined_at,
        declined_reason=row.declined_reason,
        notes_md=row.notes_md or "",
        adjusted=row.adjusted,
        adjustment_reason=row.adjustment_reason,
    )


def _write_row_to_response(row: BookingWriteRow) -> BookingResponse:
    return BookingResponse(
        id=row.id,
        employee_id=row.user_id,
        user_id=row.user_id,
        work_engagement_id=row.work_engagement_id,
        property_id=row.property_id,
        client_org_id=row.client_org_id,
        status=row.status,
        kind=row.kind,
        scheduled_start=row.scheduled_start,
        scheduled_end=row.scheduled_end,
        actual_minutes=row.actual_minutes,
        actual_minutes_paid=row.actual_minutes_paid,
        break_seconds=row.break_seconds,
        pending_amend_minutes=row.pending_amend_minutes,
        pending_amend_reason=row.pending_amend_reason,
        declined_at=row.declined_at,
        declined_reason=row.declined_reason,
        notes_md=row.notes_md or "",
        adjusted=row.adjusted,
        adjustment_reason=row.adjustment_reason,
    )


def _booking_not_found() -> NotFound:
    return NotFound(extra={"error": "not_found", "resource": "booking"})


def _booking_permission_denied(action_key: str) -> Forbidden:
    return Forbidden(extra={"error": "permission_denied", "action_key": action_key})


def _booking_not_declinable(current_status: str) -> Conflict:
    message = "only a scheduled booking can be declined"
    return Conflict(
        message,
        extra={
            "error": "booking_not_declinable",
            "booking_status": current_status,
            "message": message,
        },
    )


def _list_booking_rows(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str | None,
    property_id: str | None,
    from_: datetime | None,
    to: datetime | None,
    status: str | None,
    pending_amend: bool | None,
) -> list[Booking]:
    # code-health: ignore[params] FastAPI params are OpenAPI contract.
    stmt = select(Booking).where(
        Booking.workspace_id == ctx.workspace_id,
        Booking.deleted_at.is_(None),
    )
    if user_id is not None:
        stmt = stmt.where(Booking.user_id == user_id)
    if property_id is not None:
        stmt = stmt.where(Booking.property_id == property_id)
    if from_ is not None:
        stmt = stmt.where(Booking.scheduled_end >= from_)
    if to is not None:
        stmt = stmt.where(Booking.scheduled_start <= to)
    if status is not None:
        stmt = stmt.where(Booking.status == status)
    if pending_amend is not None:
        if pending_amend:
            stmt = stmt.where(Booking.pending_amend_minutes.is_not(None))
        else:
            stmt = stmt.where(Booking.pending_amend_minutes.is_(None))
    stmt = stmt.order_by(Booking.scheduled_start.asc(), Booking.id.asc())
    return list(session.scalars(stmt).all())


def build_bookings_router() -> APIRouter:
    api = APIRouter(
        prefix="/bookings",
        tags=["payroll", "bookings"],
        responses=IDENTITY_PROBLEM_RESPONSES,
    )

    @api.get(
        "",
        response_model=list[BookingResponse],
        operation_id="bookings.list",
        summary="List bookings in the caller's workspace",
        openapi_extra={
            "x-cli": {
                "group": "bookings",
                "verb": "list",
                "summary": "List bookings in a workspace",
                "mutates": False,
            },
        },
    )
    def list_(
        ctx: _Ctx,
        session: _Db,
        user_id: _UserFilter = None,
        property_id: _PropertyFilter = None,
        from_: _FromFilter = None,
        to: _ToFilter = None,
        status: _StatusFilter = None,
        pending_amend: _PendingAmendFilter = None,
    ) -> list[BookingResponse]:
        # code-health: ignore[params] FastAPI params are OpenAPI contract.
        if from_ is not None and to is not None and to < from_:
            raise _invalid_window()

        if ctx.actor_grant_role == "worker":
            if user_id is not None and user_id != ctx.actor_id:
                raise _permission_denied("bookings.view_other")
            user_id = ctx.actor_id
        else:
            _require_view_other(session, ctx)

        rows = _list_booking_rows(
            session,
            ctx,
            user_id=user_id,
            property_id=property_id,
            from_=from_,
            to=to,
            status=status,
            pending_amend=pending_amend,
        )
        return [_booking_to_response(row) for row in rows]

    @api.post(
        "/{booking_id}/amend",
        response_model=BookingResponse,
        operation_id="bookings.amend",
        summary="Amend a booking's actual worked time",
        openapi_extra={
            "x-cli": {
                "group": "bookings",
                "verb": "amend",
                "summary": "Amend a booking's actual worked minutes",
                "mutates": True,
            },
        },
    )
    def amend(
        booking_id: str,
        body: BookingAmendRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> BookingResponse:
        """Record the real worked time on a booking (§09 "Amend operation").

        A self-amend within the engagement's
        ``bookings.auto_approve_overrun_minutes`` (or any decrease)
        auto-approves and moves ``actual_minutes_paid``; a larger
        increase lands on the manager amend queue via
        ``pending_amend_*``. Managers/owners amend other workers'
        bookings unconditionally through ``bookings.amend_other``.
        """
        repo = SqlAlchemyBookingWriteRepository(session)
        checker = SqlAlchemyCapabilityChecker(session, ctx)
        try:
            updated = amend_booking(
                repo,
                checker,
                ctx,
                booking_id=booking_id,
                requested_minutes=body.actual_minutes,
                reason=body.reason,
                now=SystemClock().now(),
                via="api",
            )
        except BookingNotFound as exc:
            raise _booking_not_found() from exc
        except BookingPermissionDenied as exc:
            raise _booking_permission_denied(str(exc)) from exc
        return _write_row_to_response(updated)

    @api.post(
        "/{booking_id}/decline",
        response_model=BookingResponse,
        operation_id="bookings.decline",
        summary="Decline an assigned scheduled booking",
        openapi_extra={
            "x-cli": {
                "group": "bookings",
                "verb": "decline",
                "summary": "Decline a scheduled booking assigned to you",
                "mutates": True,
            },
        },
        responses={
            status.HTTP_409_CONFLICT: {
                "description": (
                    "The booking is not in a ``scheduled`` state "
                    "(``booking_not_declinable``)."
                ),
                "content": {
                    "application/problem+json": {
                        "schema": {"type": "object", "additionalProperties": True}
                    }
                },
            },
        },
    )
    def decline(
        booking_id: str,
        body: BookingDeclineRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> BookingResponse:
        """Decline a ``scheduled`` booking you are assigned to (§09).

        Stamps ``declined_at`` / ``declined_reason`` and returns the row
        to ``status = pending_approval`` for manager reassignment.
        Self-only: declining another worker's booking is a 403.
        """
        repo = SqlAlchemyBookingWriteRepository(session)
        checker = SqlAlchemyCapabilityChecker(session, ctx)
        try:
            updated = decline_booking(
                repo,
                checker,
                ctx,
                booking_id=booking_id,
                reason=body.reason,
                now=SystemClock().now(),
                via="api",
            )
        except BookingNotFound as exc:
            raise _booking_not_found() from exc
        except BookingPermissionDenied as exc:
            raise _booking_permission_denied(str(exc)) from exc
        except BookingNotDeclinable as exc:
            raise _booking_not_declinable(str(exc)) from exc
        return _write_row_to_response(updated)

    return api


router = build_bookings_router()
