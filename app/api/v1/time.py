"""Time context router — shifts clock-in / clock-out + leave requests.

Mounted by the app factory under ``/w/<slug>/api/v1/time``. All
routes require an active :class:`~app.tenancy.WorkspaceContext`.

Routes (cd-whl, cd-31c):

* ``POST /shifts/open`` — worker opens a shift for themselves (or a
  manager opens one for someone else via ``time.edit_others``).
* ``POST /shifts/{shift_id}/close`` — worker closes their own shift
  or a manager closes someone else's via ``time.edit_others``.
* ``PATCH /shifts/{shift_id}`` — manager-only retroactive amend.
* ``GET /shifts`` — list shifts in the workspace (filtered by
  ``user_id`` / ``starts_from`` / ``starts_until`` / ``open_only``).
* ``GET /shifts/{shift_id}`` — read a single shift.
* ``POST /me/leaves`` — worker self-create a pending leave request.
* ``GET /me/leaves`` — worker lists their own leaves.
* ``PATCH /me/leaves/{leave_id}`` — worker rewrites dates while pending.
* ``DELETE /me/leaves/{leave_id}`` — worker cancels their own leave.
* ``GET /leaves`` — workspace-wide leave queue (manager inbox).
* ``GET /leaves/{leave_id}`` — read a single leave.
* ``GET /leaves/{leave_id}/conflicts`` — manager advisory overlap ids.
* ``POST /leaves/{leave_id}/decision`` — manager approve / reject.
* ``DELETE /leaves/{leave_id}`` — manager cancel of someone else's leave.
* ``GET|PUT|DELETE /properties/{property_id}/geofence`` — manager reads,
  replaces, or deletes one property's geofence setting.

The handlers are thin: unpack the DTO, call the domain service, map
typed errors to HTTP. The UoW (:func:`app.api.deps.db_session`) owns
the transaction boundary; domain code never commits itself.

Module name shadows the stdlib ``time`` module locally — this is a
relative-import-only context module under ``app.api.v1`` so no import
collision is possible.

See ``docs/specs/09-time-payroll-expenses.md`` §"Bookings",
§"Owner and manager adjustments", §"Leave";
``docs/specs/12-rest-api.md`` §"REST API".
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.api.errors import problem_response
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.api.v1._problem_json import IDENTITY_PROBLEM_RESPONSES
from app.domain.errors import (
    Conflict,
    DomainError,
    Forbidden,
    NotFound,
    ServiceUnavailable,
    Validation,
)
from app.domain.time.geofence_settings import (
    GeofenceMode,
    GeofenceSettingNotFound,
    GeofenceSettingPermissionDenied,
    GeofenceSettingUpsert,
    GeofenceSettingView,
    delete_geofence_setting,
    get_geofence_setting,
    upsert_geofence_setting,
)
from app.domain.time.shifts import (
    ShiftAlreadyOpen,
    ShiftBoundaryInvalid,
    ShiftClose,
    ShiftEdit,
    ShiftEditForbidden,
    ShiftGeofenceRejected,
    ShiftNotFound,
    ShiftOpen,
    ShiftView,
    close_shift,
    edit_shift,
    get_shift,
    list_open_shifts,
    list_shifts,
    open_shift,
)
from app.services.leave import (
    LeaveBoundaryInvalid,
    LeaveCreate,
    LeaveDecision,
    LeaveDecisionRequest,
    LeaveKind,
    LeaveKindInvalid,
    LeaveNotFound,
    LeavePermissionDenied,
    LeaveStatus,
    LeaveTransitionForbidden,
    LeaveUpdateDates,
    LeaveView,
    cancel_own,
    create_leave,
    decide_leave,
    get_conflicts,
    get_leave,
    list_for_user,
    list_for_workspace,
    update_dates,
)
from app.tenancy import WorkspaceContext

__all__ = ["router"]


router = APIRouter(tags=["time"], responses=IDENTITY_PROBLEM_RESPONSES)


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ShiftPayload(BaseModel):
    """HTTP projection of :class:`~app.domain.time.shifts.ShiftView`.

    A Pydantic model rather than re-exporting the frozen dataclass so
    FastAPI's OpenAPI generator emits a named component schema the
    SPA can pattern-match on. Mirrors the read shape of the domain
    view one-to-one — no filtering, no derived fields.
    """

    id: str
    workspace_id: str
    user_id: str
    starts_at: datetime
    ends_at: datetime | None
    property_id: str | None
    source_occurrence_id: str | None
    source: str
    notes_md: str | None
    approved_by: str | None
    approved_at: datetime | None

    @classmethod
    def from_view(cls, view: ShiftView) -> ShiftPayload:
        """Copy a :class:`ShiftView` into its HTTP payload shape."""
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            user_id=view.user_id,
            starts_at=view.starts_at,
            ends_at=view.ends_at,
            property_id=view.property_id,
            source_occurrence_id=view.source_occurrence_id,
            source=view.source,
            notes_md=view.notes_md,
            approved_by=view.approved_by,
            approved_at=view.approved_at,
        )


class ShiftListResponse(BaseModel):
    """Response body for ``GET /shifts``.

    Collection envelope per ``docs/specs/12-rest-api.md`` §"Pagination" —
    ``{"data": [...], "next_cursor": "...", "has_more": ...}``. Mirrors
    the work_engagements / work_roles list shape so SPA clients can
    pattern-match identically across the v1 surface.
    """

    data: list[ShiftPayload]
    next_cursor: str | None = None
    has_more: bool = False


class LeavePayload(BaseModel):
    """HTTP projection of :class:`~app.services.leave.LeaveView`.

    A Pydantic model rather than re-exporting the frozen dataclass so
    FastAPI's OpenAPI generator emits a named component schema the
    SPA can pattern-match on. Mirrors the read shape of the domain
    view one-to-one — no filtering, no derived fields.
    """

    id: str
    workspace_id: str
    user_id: str
    kind: str
    starts_at: datetime
    ends_at: datetime
    status: str
    reason_md: str | None
    decided_by: str | None
    decided_at: datetime | None
    created_at: datetime

    @classmethod
    def from_view(cls, view: LeaveView) -> LeavePayload:
        """Copy a :class:`LeaveView` into its HTTP payload shape."""
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            user_id=view.user_id,
            kind=view.kind,
            starts_at=view.starts_at,
            ends_at=view.ends_at,
            status=view.status,
            reason_md=view.reason_md,
            decided_by=view.decided_by,
            decided_at=view.decided_at,
            created_at=view.created_at,
        )


class LeaveListResponse(BaseModel):
    """Response body for ``GET /me/leaves`` and ``GET /leaves``.

    Collection envelope per ``docs/specs/12-rest-api.md`` §"Pagination" —
    ``{"data": [...], "next_cursor": "...", "has_more": ...}``. Mirrors
    the work_engagements / work_roles list shape so SPA clients can
    pattern-match identically across the v1 surface.
    """

    data: list[LeavePayload]
    next_cursor: str | None = None
    has_more: bool = False


class LeaveConflictsPayload(BaseModel):
    """HTTP projection of advisory leave conflicts."""

    leave_id: str
    shift_ids: list[str]
    occurrence_ids: list[str]


class LeaveDecisionBody(BaseModel):
    """Request body for ``POST /leaves/{leave_id}/decision``."""

    model_config = {"extra": "forbid"}

    decision: LeaveDecision
    rationale_md: str | None = None


class GeofenceSettingPayload(BaseModel):
    """HTTP projection of a per-property geofence setting."""

    id: str
    workspace_id: str
    property_id: str
    lat: float
    lon: float
    radius_m: int
    enabled: bool
    mode: GeofenceMode

    @classmethod
    def from_view(cls, view: GeofenceSettingView) -> GeofenceSettingPayload:
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            property_id=view.property_id,
            lat=view.lat,
            lon=view.lon,
            radius_m=view.radius_m,
            enabled=view.enabled,
            mode=view.mode,
        )


class GeofenceSettingBody(GeofenceSettingUpsert):
    """Request body for ``PUT /properties/{property_id}/geofence``."""


_PropertyId = Annotated[str, Path(max_length=40)]


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _domain_for_shift_error(exc: Exception) -> DomainError:
    """Map a domain shift error to the router's problem+json shape.

    Keeps the mapping centralised so every route returns the same
    ``error`` symbol for the same domain type.
    """
    if isinstance(exc, ShiftNotFound):
        return NotFound(extra={"error": "not_found"})
    if isinstance(exc, ShiftAlreadyOpen):
        return Conflict(
            extra={
                "error": "already_open",
                "existing_shift_id": exc.existing_shift_id,
            }
        )
    if isinstance(exc, ShiftBoundaryInvalid):
        return Validation(str(exc), extra={"error": "invalid_window"})
    if isinstance(exc, ShiftGeofenceRejected):
        return Validation(extra=exc.verdict.to_http_detail())
    if isinstance(exc, ShiftEditForbidden):
        return Forbidden(extra={"error": "forbidden"})
    return ServiceUnavailable("Internal server error", extra={"error": "internal"})


def _domain_for_leave_error(exc: Exception) -> DomainError:
    """Map a domain leave error to the router's problem+json shape.

    Mirrors :func:`_domain_for_shift_error`. The split between 409 and
    422 follows the shifts convention:

    * :class:`LeaveBoundaryInvalid` -> 422 ``invalid_window`` (bad
      payload shape — ``starts_at >= ends_at``).
    * :class:`LeaveKindInvalid` -> 422 ``invalid_kind`` (service-
      layer defence when a Python caller bypassed the DTO's
      ``LeaveKind`` literal).
    * :class:`LeaveTransitionForbidden` -> 409 ``invalid_transition``
      (well-formed payload against an inhospitable state machine).
    * :class:`LeaveNotFound` -> 404.
    * :class:`LeavePermissionDenied` -> 403.
    """
    if isinstance(exc, LeaveNotFound):
        return NotFound(extra={"error": "not_found"})
    if isinstance(exc, LeaveBoundaryInvalid):
        return Validation(str(exc), extra={"error": "invalid_window"})
    if isinstance(exc, LeaveKindInvalid):
        return Validation(str(exc), extra={"error": "invalid_kind"})
    if isinstance(exc, LeaveTransitionForbidden):
        return Conflict(str(exc), extra={"error": "invalid_transition"})
    if isinstance(exc, LeavePermissionDenied):
        return Forbidden(extra={"error": "forbidden"})
    return ServiceUnavailable("Internal server error", extra={"error": "internal"})


def _domain_for_geofence_setting_error(exc: Exception) -> DomainError:
    """Map geofence-setting service errors to HTTP envelopes."""
    if isinstance(exc, GeofenceSettingNotFound):
        return NotFound(extra={"error": "not_found"})
    if isinstance(exc, GeofenceSettingPermissionDenied):
        return Forbidden(extra={"error": "forbidden"})
    return ServiceUnavailable("Internal server error", extra={"error": "internal"})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/shifts/open",
    status_code=status.HTTP_201_CREATED,
    response_model=ShiftPayload,
    operation_id="time.open_shift",
    summary="Open (clock-in) a shift",
)
def post_open_shift(
    body: ShiftOpen,
    request: Request,
    ctx: _Ctx,
    session: _Db,
) -> ShiftPayload | JSONResponse:
    """Open a fresh shift for the caller (or the body's ``user_id``)."""
    try:
        view = open_shift(
            session,
            ctx,
            user_id=body.user_id,
            property_id=body.property_id,
            source=body.source,
            notes_md=body.notes_md,
            client_lat=body.client_lat,
            client_lon=body.client_lon,
            gps_accuracy_m=body.gps_accuracy_m,
        )
    except ShiftGeofenceRejected as exc:
        err = _domain_for_shift_error(exc)
        # The service wrote a rejection audit row before raising; return the
        # problem response directly so the request UoW can commit it.
        return problem_response(
            request,
            status=422,
            type_name=err.type_name,
            title=err.title,
            detail=err.detail,
            extra=err.extra,
        )
    except (ShiftAlreadyOpen, ShiftEditForbidden) as exc:
        raise _domain_for_shift_error(exc) from exc

    return ShiftPayload.from_view(view)


@router.post(
    "/shifts/{shift_id}/close",
    response_model=ShiftPayload,
    operation_id="time.close_shift",
    summary="Close (clock-out) a shift",
)
def post_close_shift(
    shift_id: str,
    body: ShiftClose,
    ctx: _Ctx,
    session: _Db,
) -> ShiftPayload:
    """Close the shift identified by ``shift_id``."""
    try:
        view = close_shift(
            session,
            ctx,
            shift_id=shift_id,
            ends_at=body.ends_at,
        )
    except (ShiftNotFound, ShiftBoundaryInvalid, ShiftEditForbidden) as exc:
        raise _domain_for_shift_error(exc) from exc

    return ShiftPayload.from_view(view)


@router.patch(
    "/shifts/{shift_id}",
    response_model=ShiftPayload,
    operation_id="time.edit_shift",
    summary="Manager edit of a shift",
    openapi_extra={"x-cli": {"group": "time", "verb": "shift-update"}},
)
def patch_edit_shift(
    shift_id: str,
    body: ShiftEdit,
    ctx: _Ctx,
    session: _Db,
) -> ShiftPayload:
    """Patch the mutable fields of a shift."""
    kwargs: dict[str, Any] = {}
    # Forward only fields the client actually sent so "None ==
    # leave untouched" semantics hold. The PATCH DTO is all-optional
    # with ``None`` defaults, so we walk ``model_fields_set`` to
    # know which were explicit.
    for field in body.model_fields_set:
        kwargs[field] = getattr(body, field)

    try:
        view = edit_shift(session, ctx, shift_id=shift_id, **kwargs)
    except (ShiftNotFound, ShiftBoundaryInvalid, ShiftEditForbidden) as exc:
        raise _domain_for_shift_error(exc) from exc

    return ShiftPayload.from_view(view)


@router.get(
    "/shifts",
    response_model=ShiftListResponse,
    operation_id="time.list_shifts",
    summary="List shifts in the workspace",
    openapi_extra={"x-cli": {"group": "time", "verb": "shifts-list"}},
)
def get_list_shifts(
    ctx: _Ctx,
    session: _Db,
    user_id: Annotated[str | None, Query(max_length=40)] = None,
    starts_from: Annotated[datetime | None, Query()] = None,
    starts_until: Annotated[datetime | None, Query()] = None,
    open_only: Annotated[bool, Query()] = False,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> ShiftListResponse:
    """Return every shift matching the optional filters.

    Cursor-paginated per spec §"Pagination" — the response envelope
    is ``{data, next_cursor, has_more}``. The service returns up to
    ``limit + 1`` rows so :func:`paginate` can compute ``has_more``
    without a second query.
    """
    after_id = decode_cursor(cursor)
    if open_only:
        views = list_open_shifts(
            session, ctx, user_id=user_id, limit=limit, after_id=after_id
        )
    else:
        views = list_shifts(
            session,
            ctx,
            user_id=user_id,
            starts_from=starts_from,
            starts_until=starts_until,
            limit=limit,
            after_id=after_id,
        )
    page = paginate(views, limit=limit, key_getter=lambda v: v.id)
    return ShiftListResponse(
        data=[ShiftPayload.from_view(v) for v in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get(
    "/shifts/{shift_id}",
    response_model=ShiftPayload,
    operation_id="time.get_shift",
    summary="Read a single shift",
    openapi_extra={"x-cli": {"group": "time", "verb": "shift-show"}},
)
def get_one_shift(
    shift_id: str,
    ctx: _Ctx,
    session: _Db,
) -> ShiftPayload:
    """Return the shift identified by ``shift_id``."""
    try:
        view = get_shift(session, ctx, shift_id=shift_id)
    except ShiftNotFound as exc:
        raise _domain_for_shift_error(exc) from exc
    return ShiftPayload.from_view(view)


# ---------------------------------------------------------------------------
# Geofence setting routes (cd-gdit)
# ---------------------------------------------------------------------------


@router.get(
    "/properties/{property_id}/geofence",
    response_model=GeofenceSettingPayload,
    operation_id="time.get_geofence_setting",
    summary="Read one property's geofence setting",
    openapi_extra={"x-cli": {"group": "time", "verb": "geofence-setting-show"}},
)
def get_one_geofence_setting(
    property_id: _PropertyId,
    ctx: _Ctx,
    session: _Db,
) -> GeofenceSettingPayload:
    """Return one property's geofence setting."""
    try:
        view = get_geofence_setting(session, ctx, property_id=property_id)
    except (GeofenceSettingNotFound, GeofenceSettingPermissionDenied) as exc:
        raise _domain_for_geofence_setting_error(exc) from exc
    return GeofenceSettingPayload.from_view(view)


@router.put(
    "/properties/{property_id}/geofence",
    response_model=GeofenceSettingPayload,
    operation_id="time.upsert_geofence_setting",
    summary="Create or replace one property's geofence setting",
    openapi_extra={"x-cli": {"group": "time", "verb": "geofence-setting-upsert"}},
)
def put_geofence_setting(
    property_id: _PropertyId,
    body: GeofenceSettingBody,
    ctx: _Ctx,
    session: _Db,
) -> GeofenceSettingPayload:
    """Create or replace the geofence setting for ``property_id``."""
    try:
        view = upsert_geofence_setting(
            session,
            ctx,
            property_id=property_id,
            body=body,
        )
    except (GeofenceSettingNotFound, GeofenceSettingPermissionDenied) as exc:
        raise _domain_for_geofence_setting_error(exc) from exc
    return GeofenceSettingPayload.from_view(view)


@router.delete(
    "/properties/{property_id}/geofence",
    response_model=GeofenceSettingPayload,
    operation_id="time.delete_geofence_setting",
    summary="Delete one property's geofence setting",
    openapi_extra={"x-cli": {"group": "time", "verb": "geofence-setting-delete"}},
)
def delete_one_geofence_setting(
    property_id: _PropertyId,
    ctx: _Ctx,
    session: _Db,
) -> GeofenceSettingPayload:
    """Delete the geofence setting for ``property_id``."""
    try:
        view = delete_geofence_setting(session, ctx, property_id=property_id)
    except (GeofenceSettingNotFound, GeofenceSettingPermissionDenied) as exc:
        raise _domain_for_geofence_setting_error(exc) from exc
    return GeofenceSettingPayload.from_view(view)


# ---------------------------------------------------------------------------
# Leave routes — self-service (cd-31c)
# ---------------------------------------------------------------------------


class MeLeaveCreate(BaseModel):
    """Request body for ``POST /me/leaves``.

    A narrower shape than :class:`~app.services.leave.LeaveCreate`
    (no ``user_id`` — the caller is always the target) so the SPA
    can't accidentally author a leave for someone else through the
    self-service surface. Managers use ``POST /leaves`` (not shipped
    in this slice — cd-8pi) or go through the domain service for
    cross-user creation today.

    ``kind`` narrows to the :data:`LeaveKind` literal so the HTTP
    boundary rejects out-of-set values with FastAPI's standard
    ``detail[].loc/msg`` 422 envelope. The service-layer DTO
    reasserts the same check via :class:`LeaveKindInvalid` for
    Python callers that bypass this model.
    """

    model_config = {"extra": "forbid"}

    kind: LeaveKind
    starts_at: datetime
    ends_at: datetime
    reason_md: str | None = None


@router.post(
    "/me/leaves",
    status_code=status.HTTP_201_CREATED,
    response_model=LeavePayload,
    operation_id="time.create_my_leave",
    summary="Create a pending leave request for the caller",
)
def post_create_my_leave(
    body: MeLeaveCreate,
    ctx: _Ctx,
    session: _Db,
) -> LeavePayload:
    """Create a ``pending`` leave for the caller and return the fresh view."""
    # Re-validate the window through the service-layer DTO so the
    # domain DTO's ``model_validator`` fires (and the service never
    # sees a malformed shape even when called from Python land).
    try:
        service_body = LeaveCreate(
            user_id=None,
            kind=body.kind,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            reason_md=body.reason_md,
        )
    except ValueError as exc:
        raise Validation(str(exc), extra={"error": "invalid_payload"}) from exc

    try:
        view = create_leave(session, ctx, body=service_body)
    except (
        LeaveBoundaryInvalid,
        LeaveKindInvalid,
        LeavePermissionDenied,
    ) as exc:
        raise _domain_for_leave_error(exc) from exc
    return LeavePayload.from_view(view)


@router.get(
    "/me/leaves",
    response_model=LeaveListResponse,
    operation_id="time.list_my_leaves",
    summary="List the caller's own leaves",
    openapi_extra={"x-cli": {"group": "time", "verb": "my-leaves-list"}},
)
def get_list_my_leaves(
    ctx: _Ctx,
    session: _Db,
    status_: Annotated[
        LeaveStatus | None,
        Query(alias="status"),
    ] = None,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> LeaveListResponse:
    """Return every leave owned by the caller, optionally filtered.

    Cursor-paginated per spec §"Pagination" — the response envelope
    is ``{data, next_cursor, has_more}``. The service returns up to
    ``limit + 1`` rows so :func:`paginate` can compute ``has_more``
    without a second query.
    """
    after_id = decode_cursor(cursor)
    # ``list_for_user`` defaults ``user_id`` to ``ctx.actor_id`` when
    # ``None`` — the self-service path is always self.
    views = list_for_user(
        session,
        ctx,
        user_id=None,
        status=status_,
        limit=limit,
        after_id=after_id,
    )
    page = paginate(views, limit=limit, key_getter=lambda v: v.id)
    return LeaveListResponse(
        data=[LeavePayload.from_view(v) for v in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


def _load_owned_leave_or_404(
    session: Session,
    ctx: WorkspaceContext,
    *,
    leave_id: str,
) -> LeaveView:
    """Return ``leave_id`` iff it belongs to the caller, else 404.

    Shared guard for the ``/me/leaves/{id}`` mutating routes. The
    ``/me/`` URL prefix is a caller-scoped namespace — a manager
    hitting ``/me/leaves/<worker-leave-id>`` would otherwise succeed
    via ``leaves.edit_others``, which contradicts the documented
    "worker cancels their own leave" contract and surprises
    SPA / CLI / agent callers. Managers cross-operate via
    ``/leaves/{id}``; this helper keeps ``/me/`` strictly self.

    Returns 404 (not 403) to avoid enumerating other users' leave
    ids through the ``/me/`` surface (§01 "tenant surface is not
    enumerable").
    """
    try:
        view = get_leave(session, ctx, leave_id=leave_id)
    except LeaveNotFound as exc:
        raise _domain_for_leave_error(exc) from exc
    except LeavePermissionDenied as exc:
        # Caller is a non-owner with cross-user privileges — still
        # not the caller's own leave, so ``/me/`` rejects.
        raise NotFound(extra={"error": "not_found"}) from exc
    if view.user_id != ctx.actor_id:
        raise NotFound(extra={"error": "not_found"})
    return view


@router.patch(
    "/me/leaves/{leave_id}",
    response_model=LeavePayload,
    operation_id="time.update_my_leave_dates",
    summary="Rewrite dates on a pending leave the caller owns",
    openapi_extra={"x-cli": {"group": "time", "verb": "my-leave-update"}},
)
def patch_update_my_leave(
    leave_id: str,
    body: LeaveUpdateDates,
    ctx: _Ctx,
    session: _Db,
) -> LeavePayload:
    """Rewrite ``starts_at`` / ``ends_at`` on a pending leave.

    ``/me/`` paths are caller-scoped: the target leave must belong
    to ``ctx.actor_id``. Managers editing someone else's leave take
    the ``/leaves/{id}`` path (not in this slice — cd-8pi).
    """
    _load_owned_leave_or_404(session, ctx, leave_id=leave_id)
    try:
        view = update_dates(session, ctx, leave_id=leave_id, body=body)
    except (
        LeaveNotFound,
        LeaveBoundaryInvalid,
        LeaveTransitionForbidden,
        LeavePermissionDenied,
    ) as exc:
        raise _domain_for_leave_error(exc) from exc
    return LeavePayload.from_view(view)


@router.delete(
    "/me/leaves/{leave_id}",
    response_model=LeavePayload,
    operation_id="time.cancel_my_leave",
    summary="Cancel a pending or upcoming leave the caller owns",
    openapi_extra={"x-cli": {"group": "time", "verb": "my-leave-cancel"}},
)
def delete_cancel_my_leave(
    leave_id: str,
    ctx: _Ctx,
    session: _Db,
) -> LeavePayload:
    """Transition a leave from pending / upcoming-approved to cancelled.

    ``/me/`` paths are caller-scoped — see
    :func:`patch_update_my_leave` for the rationale.
    """
    _load_owned_leave_or_404(session, ctx, leave_id=leave_id)
    try:
        view = cancel_own(session, ctx, leave_id=leave_id)
    except (
        LeaveNotFound,
        LeaveTransitionForbidden,
        LeavePermissionDenied,
    ) as exc:
        raise _domain_for_leave_error(exc) from exc
    return LeavePayload.from_view(view)


# ---------------------------------------------------------------------------
# Leave routes — workspace queue (cd-31c)
# ---------------------------------------------------------------------------


@router.get(
    "/leaves",
    response_model=LeaveListResponse,
    operation_id="time.list_leaves",
    summary="List leaves in the workspace",
    openapi_extra={"x-cli": {"group": "time", "verb": "leaves-list"}},
)
def get_list_leaves(
    ctx: _Ctx,
    session: _Db,
    user_id: Annotated[str | None, Query(max_length=40)] = None,
    status_: Annotated[
        LeaveStatus | None,
        Query(alias="status"),
    ] = None,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> LeaveListResponse:
    """Return leaves matching the optional filters.

    Gating follows the service's authority model:

    * ``user_id`` == caller -> self-service path, no capability
      check (same as ``GET /me/leaves``).
    * ``user_id`` set to someone else -> cross-user lookup via
      :func:`list_for_user`; service raises 403 unless the caller
      holds ``leaves.view_others``.
    * ``user_id`` omitted -> workspace-wide queue via
      :func:`list_for_workspace`; always requires
      ``leaves.view_others``.

    Cursor-paginated per spec §"Pagination" — the response envelope
    is ``{data, next_cursor, has_more}``. The service returns up to
    ``limit + 1`` rows so :func:`paginate` can compute ``has_more``
    without a second query.
    """
    after_id = decode_cursor(cursor)
    try:
        if user_id is None:
            views = list_for_workspace(
                session, ctx, status=status_, limit=limit, after_id=after_id
            )
        else:
            views = list_for_user(
                session,
                ctx,
                user_id=user_id,
                status=status_,
                limit=limit,
                after_id=after_id,
            )
    except LeavePermissionDenied as exc:
        raise _domain_for_leave_error(exc) from exc
    page = paginate(views, limit=limit, key_getter=lambda v: v.id)
    return LeaveListResponse(
        data=[LeavePayload.from_view(v) for v in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get(
    "/leaves/{leave_id}",
    response_model=LeavePayload,
    operation_id="time.get_leave",
    summary="Read a single leave",
    openapi_extra={"x-cli": {"group": "time", "verb": "leave-show"}},
)
def get_one_leave(
    leave_id: str,
    ctx: _Ctx,
    session: _Db,
) -> LeavePayload:
    """Return the leave identified by ``leave_id``."""
    try:
        view = get_leave(session, ctx, leave_id=leave_id)
    except (LeaveNotFound, LeavePermissionDenied) as exc:
        raise _domain_for_leave_error(exc) from exc
    return LeavePayload.from_view(view)


@router.get(
    "/leaves/{leave_id}/conflicts",
    response_model=LeaveConflictsPayload,
    operation_id="time.get_leave_conflicts",
    summary="Return advisory leave overlap ids",
    openapi_extra={"x-cli": {"group": "time", "verb": "leave-conflicts"}},
)
def get_leave_conflicts(
    leave_id: str,
    ctx: _Ctx,
    session: _Db,
) -> LeaveConflictsPayload:
    """Return shift/task ids overlapping the target leave window."""
    try:
        conflicts = get_conflicts(session, ctx, leave_id=leave_id)
    except (LeaveNotFound, LeavePermissionDenied) as exc:
        raise _domain_for_leave_error(exc) from exc
    return LeaveConflictsPayload(
        leave_id=conflicts.leave_id,
        shift_ids=list(conflicts.shift_ids),
        occurrence_ids=list(conflicts.occurrence_ids),
    )


@router.post(
    "/leaves/{leave_id}/decision",
    response_model=LeavePayload,
    operation_id="time.decide_leave",
    summary="Approve or reject a pending leave",
    openapi_extra={"x-cli": {"group": "time", "verb": "leave-decide"}},
)
def post_decide_leave(
    leave_id: str,
    body: LeaveDecisionBody,
    ctx: _Ctx,
    session: _Db,
) -> LeavePayload:
    """Transition a pending leave to approved or rejected."""
    try:
        service_body = LeaveDecisionRequest.model_validate(body.model_dump())
    except ValueError as exc:
        raise Validation(str(exc), extra={"error": "invalid_payload"}) from exc

    try:
        view = decide_leave(session, ctx, leave_id=leave_id, body=service_body)
    except (
        LeaveNotFound,
        LeaveTransitionForbidden,
        LeavePermissionDenied,
    ) as exc:
        raise _domain_for_leave_error(exc) from exc
    return LeavePayload.from_view(view)


@router.delete(
    "/leaves/{leave_id}",
    response_model=LeavePayload,
    operation_id="time.cancel_leave",
    summary="Cancel a leave (manager path)",
    openapi_extra={"x-cli": {"group": "time", "verb": "leave-cancel"}},
)
def delete_cancel_leave(
    leave_id: str,
    ctx: _Ctx,
    session: _Db,
) -> LeavePayload:
    """Transition a leave to cancelled. Same guards as ``/me/leaves``.

    Kept as a separate URL from ``/me/leaves/{id}`` so the SPA can
    surface a "manage leave" verb distinct from "cancel my own" —
    the service's cross-user gate (``leaves.edit_others``) still
    decides authorisation based on whose leave it is, not on which
    URL the caller hit.
    """
    try:
        view = cancel_own(session, ctx, leave_id=leave_id)
    except (
        LeaveNotFound,
        LeaveTransitionForbidden,
        LeavePermissionDenied,
    ) as exc:
        raise _domain_for_leave_error(exc) from exc
    return LeavePayload.from_view(view)
