"""Self-service ``/me/*`` HTTP router (cd-6uij) — schedule + leaves + overrides.

Mounted inside ``/w/<slug>/api/v1`` by the app factory. Surface per
``docs/specs/12-rest-api.md`` §"Self-service shortcuts":

```
GET    /me/schedule                       # self-only calendar feed
POST   /me/leaves                         # self-only leave create
GET    /me/availability_overrides         # self-only override list
POST   /me/availability_overrides         # self-only override create
```

Tags: ``identity`` + ``me`` so the OpenAPI surface clusters the verbs
alongside the rest of the identity context (matching the sibling
``user_leaves`` / ``user_availability_overrides`` routers, which tag
themselves under ``identity`` + their own resource tag).

**Self-only by construction.** Each POST forces ``user_id =
ctx.actor_id`` before delegating to the underlying domain service.
The ``user_id`` field is **deliberately absent** from the wire
request body (Pydantic ``extra="forbid"`` rejects an explicit
``user_id`` field with a 422). The cleaner shape — refusing the
field at the schema layer — is preferable to a route-level 403 check
because it keeps the semantic clear: this surface only ever speaks
for the caller.

The ``GET /me/schedule`` aggregator stitches two sources:

* :func:`app.domain.identity.me_schedule.aggregate_schedule` — the
  identity-side seam: weekly pattern, leaves, overrides, bookings,
  resolved window, caller id.
* :mod:`app.api.v1._scheduler_resolver` — shared rota / slot /
  assignment / property / task synthesiser used by both this route
  and ``/scheduler/calendar`` so the two views stay in lockstep
  until the §06 ``schedule_ruleset`` table lands.

See ``docs/specs/12-rest-api.md`` §"Self-service shortcuts",
``docs/specs/14-web-frontend.md`` §"Schedule view",
``docs/specs/06-tasks-and-scheduling.md`` §"user_leave",
§"user_availability_overrides", §"Weekly availability".
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.adapters.db.identity.repositories import (
    SqlAlchemyMeScheduleQueryRepository,
)
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.api.v1._problem_json import IDENTITY_PROBLEM_RESPONSES
from app.api.v1._scheduler_resolver import (
    ScheduleAssignmentResponse,
    SchedulerPropertyResponse,
    SchedulerTaskResponse,
    ScheduleRulesetResponse,
    ScheduleRulesetSlotResponse,
    SchedulerWindowResponse,
    assignment_rows_for_window,
    build_rota_blocks,
    estimated_minutes,
    list_workspace_properties,
    property_name,
    scheduled_start_text,
    task_rows_for_window,
    weekly_rows_for_users,
)
from app.api.v1.user_availability_overrides import (
    UserAvailabilityOverrideListResponse,
    UserAvailabilityOverrideResponse,
)
from app.api.v1.user_availability_overrides import (
    _http_for_invariant as _http_for_override_invariant,
)
from app.api.v1.user_availability_overrides import (
    _view_to_response as _override_view_to_response,
)
from app.api.v1.user_availability_overrides import (
    make_seam_pair as make_override_seam_pair,
)
from app.api.v1.user_leaves import (
    UserLeaveResponse,
)
from app.api.v1.user_leaves import (
    _http_for_invariant as _http_for_leave_invariant,
)
from app.api.v1.user_leaves import _view_to_response as _leave_view_to_response
from app.api.v1.user_leaves import (
    make_seam_pair as make_leave_seam_pair,
)
from app.domain.identity.me_schedule import (
    SchedulePayload,
    WeeklySlotView,
    aggregate_schedule,
)
from app.domain.identity.me_schedule_ports import BookingRefRow
from app.domain.identity.user_availability_overrides import (
    UserAvailabilityOverrideAlreadyExists,
    UserAvailabilityOverrideCreate,
    UserAvailabilityOverrideInvariantViolated,
    UserAvailabilityOverrideListFilter,
    UserAvailabilityOverridePermissionDenied,
    create_override,
    list_overrides,
)
from app.domain.identity.user_leaves import (
    UserLeaveCategory,
    UserLeaveCreate,
    UserLeaveInvariantViolated,
    UserLeavePermissionDenied,
    create_leave,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "MeAvailabilityOverrideCreateRequest",
    "MeBookingResponse",
    "MeLeaveCreateRequest",
    "MeScheduleResponse",
    "MeWeeklySlotResponse",
    "build_me_schedule_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]

_MAX_NOTE_LEN = 20_000
_MAX_REASON_LEN = 20_000


# ---------------------------------------------------------------------------
# Wire-facing shapes — request bodies
# ---------------------------------------------------------------------------


class MeLeaveCreateRequest(BaseModel):
    """Request body for ``POST /me/leaves``.

    ``user_id`` is **deliberately absent**: the router forces
    ``user_id = ctx.actor_id`` before delegating to
    :func:`~app.domain.identity.user_leaves.create_leave`. An explicit
    ``user_id`` in the body lands as a 422 ``unknown_field`` from
    Pydantic ``extra="forbid"`` — the cleanest way to keep the "self
    only" invariant honest at the wire.
    """

    model_config = ConfigDict(extra="forbid")

    starts_on: date
    ends_on: date
    category: UserLeaveCategory
    note_md: str | None = Field(default=None, max_length=_MAX_NOTE_LEN)

    @model_validator(mode="after")
    def _validate_window(self) -> MeLeaveCreateRequest:
        """Reject ``ends_on < starts_on`` at the DTO layer.

        Mirrors :class:`~app.domain.identity.user_leaves.UserLeaveCreate`
        so a malformed window surfaces as a 422 from FastAPI's
        validation envelope rather than as a 500 from the service-
        layer raise.
        """
        if self.ends_on < self.starts_on:
            raise ValueError("ends_on must be on or after starts_on")
        return self


class MeAvailabilityOverrideCreateRequest(BaseModel):
    """Request body for ``POST /me/availability_overrides``.

    ``user_id`` is **deliberately absent** for the same reason as
    :class:`MeLeaveCreateRequest`. Hours pairing + backwards-window
    rejection mirror
    :class:`~app.domain.identity.user_availability_overrides.UserAvailabilityOverrideCreate`.
    """

    model_config = ConfigDict(extra="forbid")

    date: date
    available: bool
    starts_local: time | None = None
    ends_local: time | None = None
    reason: str | None = Field(default=None, max_length=_MAX_REASON_LEN)

    @model_validator(mode="after")
    def _validate_hours(self) -> MeAvailabilityOverrideCreateRequest:
        """Enforce BOTH-OR-NEITHER + ``ends_local > starts_local``."""
        starts = self.starts_local
        ends = self.ends_local
        if (starts is None) != (ends is None):
            raise ValueError(
                "starts_local and ends_local must both be set or both be null"
            )
        if starts is not None and ends is not None and ends <= starts:
            raise ValueError("ends_local must be after starts_local")
        if not self.available and (starts is not None or ends is not None):
            raise ValueError(
                "available=false overrides must not carry hours; clear "
                "starts_local / ends_local"
            )
        return self


# ---------------------------------------------------------------------------
# Wire-facing shapes — schedule response
# ---------------------------------------------------------------------------


class MeWeeklySlotResponse(BaseModel):
    """One row of the caller's standing weekly availability pattern."""

    weekday: int
    starts_local: time | None
    ends_local: time | None


class MeBookingResponse(BaseModel):
    """Wire shape for a worker booking on the §14 calendar.

    Mirrors the §09 frontend ``Booking`` type. ``employee_id`` is
    populated from the booking's ``work_engagement_id`` until a
    dedicated :class:`Employee` table lands; for the v1 partial
    UNIQUE on ``(user_id, workspace_id) WHERE archived_on IS NULL``
    every active worker booking has exactly one engagement, so the
    frontend can treat the two ids as interchangeable for the purpose
    of grouping bookings by worker.
    """

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
    actual_minutes_paid: int
    break_seconds: int
    pending_amend_minutes: int | None
    pending_amend_reason: str | None
    declined_at: datetime | None
    declined_reason: str | None
    notes_md: str | None
    adjusted: bool
    adjustment_reason: str | None


class MeScheduleResponse(BaseModel):
    """Aggregated calendar feed for the caller across ``[from, to]``.

    Wire shape per §14 "Schedule view" — what the SPA's ``/schedule``
    page consumes. Composed by stitching two sources at the router
    layer (see module docstring): the identity-side aggregator owns
    leaves / overrides / bookings / weekly_availability / user_id /
    window; the shared scheduler resolver owns rulesets / slots /
    assignments / tasks / properties.
    """

    window: SchedulerWindowResponse
    user_id: str
    weekly_availability: list[MeWeeklySlotResponse]
    rulesets: list[ScheduleRulesetResponse]
    slots: list[ScheduleRulesetSlotResponse]
    assignments: list[ScheduleAssignmentResponse]
    tasks: list[SchedulerTaskResponse]
    properties: list[SchedulerPropertyResponse]
    leaves: list[UserLeaveResponse]
    overrides: list[UserAvailabilityOverrideResponse]
    bookings: list[MeBookingResponse]


# ---------------------------------------------------------------------------
# Query dependencies
# ---------------------------------------------------------------------------


_FromQuery = Annotated[
    date | None,
    Query(
        alias="from",
        description=(
            "Inclusive lower bound on the schedule window (ISO date). "
            "Defaults to today."
        ),
    ),
]


_ToQuery = Annotated[
    date | None,
    Query(
        alias="to",
        description=(
            "Inclusive upper bound on the schedule window (ISO date). "
            "Defaults to today + 14 days."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _weekly_slot_to_response(slot: WeeklySlotView) -> MeWeeklySlotResponse:
    return MeWeeklySlotResponse(
        weekday=slot.weekday,
        starts_local=slot.starts_local,
        ends_local=slot.ends_local,
    )


def _booking_to_response(row: BookingRefRow) -> MeBookingResponse:
    return MeBookingResponse(
        id=row.id,
        # Until a dedicated ``employee`` table exists, frontend
        # ``Booking.employee_id`` is satisfied by the work_engagement
        # id (one active engagement per (user, workspace)).
        employee_id=row.work_engagement_id,
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
        notes_md=row.notes_md,
        adjusted=row.adjusted,
        adjustment_reason=row.adjustment_reason,
    )


def _resolve_self_calendar(
    session: Session,
    ctx: WorkspaceContext,
    *,
    payload: SchedulePayload,
) -> tuple[
    list[ScheduleRulesetResponse],
    list[ScheduleRulesetSlotResponse],
    list[ScheduleAssignmentResponse],
    list[SchedulerTaskResponse],
    list[SchedulerPropertyResponse],
]:
    """Synthesise the rota / slot / assignment / task / property bag.

    Both the manager ``/scheduler/calendar`` route and this self-view
    use the same synthesiser to project ``PropertyWorkRoleAssignment``
    + ``UserWeeklyAvailability`` rows into the §14 wire shape. Until
    the §06 ``schedule_ruleset`` table ships, this is the single
    source of truth.

    The properties list is the intersection of workspace-visible
    properties with the union of the user's assignment + task +
    booking footprint — the worker only sees the properties they
    actually touch in this window.
    """
    workspace_properties = list_workspace_properties(session, ctx)
    properties_by_id = {prop.id: prop for prop in workspace_properties}
    property_timezones = {prop.id: prop.timezone for prop in workspace_properties}
    workspace_property_ids = {prop.id for prop in workspace_properties}

    assignment_rows = assignment_rows_for_window(
        session,
        ctx,
        visible_property_ids=workspace_property_ids,
        narrow_to_user_id=ctx.actor_id,
    )
    task_rows = task_rows_for_window(
        session,
        ctx,
        from_date=payload.from_date,
        to_date=payload.to_date,
        visible_property_ids=workspace_property_ids,
        property_timezones=property_timezones,
        narrow_to_user_id=ctx.actor_id,
        # /me/schedule is the worker self-view: cancelled tasks are
        # no longer actionable, so they don't surface here. The
        # manager /scheduler/calendar leaves them visible.
        exclude_cancelled=True,
    )

    weekly_by_user = weekly_rows_for_users(session, ctx, user_ids={ctx.actor_id})

    rulesets, slots, assignments = build_rota_blocks(
        workspace_id=ctx.workspace_id,
        assignment_rows=assignment_rows,
        weekly_by_user=weekly_by_user,
    )

    tasks = [
        SchedulerTaskResponse(
            id=task.id,
            title=task.title or "Task",
            property_id=task.property_id,
            user_id=task.assignee_user_id,
            scheduled_start=scheduled_start_text(task),
            estimated_minutes=estimated_minutes(task),
            priority=task.priority,
            status=task.state,
        )
        for task in task_rows
        if task.property_id is not None and task.assignee_user_id is not None
    ]

    # Property footprint for the calendar legend: only the ones the
    # worker actually touches in this window.
    touched_property_ids: set[str] = set()
    for assignment, _uwr, _role, _user in assignment_rows:
        touched_property_ids.add(assignment.property_id)
    for task in task_rows:
        if task.property_id is not None:
            touched_property_ids.add(task.property_id)
    for booking in payload.bookings:
        if booking.property_id is not None:
            touched_property_ids.add(booking.property_id)

    visible_properties = sorted(
        (
            SchedulerPropertyResponse(
                id=prop.id,
                name=property_name(prop),
                timezone=prop.timezone,
            )
            for prop in workspace_properties
            if prop.id in touched_property_ids
        ),
        key=lambda row: (property_name(properties_by_id[row.id]), row.id),
    )

    return rulesets, slots, assignments, tasks, visible_properties


def _http_for_window() -> HTTPException:
    """Return a 422 envelope when the caller sends a backwards window."""
    return HTTPException(
        status_code=422,
        detail={
            "error": "invalid_field",
            "field": "to",
            "message": "to must be on or after from",
        },
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_me_schedule_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for the self-service surface."""
    api = APIRouter(
        prefix="/me",
        tags=["identity", "me"],
        responses=IDENTITY_PROBLEM_RESPONSES,
    )

    @api.get(
        "/schedule",
        response_model=MeScheduleResponse,
        operation_id="me.schedule.get",
        summary="Aggregated calendar feed for the caller",
        openapi_extra={"x-cli": {"group": "me", "verb": "schedule"}},
    )
    def get_schedule(
        ctx: _Ctx,
        session: _Db,
        from_: _FromQuery = None,
        to: _ToQuery = None,
    ) -> MeScheduleResponse:
        """Return the full §14 calendar payload for the caller.

        ``from_`` is the ``?from=`` query alias (Python keyword
        clash). The wire param stays ``from`` — see the
        :data:`_FromQuery` dependency annotation. Defaults are
        ``[today, today+14d]`` per §12 "Self-service shortcuts".
        """
        if from_ is not None and to is not None and to < from_:
            raise _http_for_window()
        repo = SqlAlchemyMeScheduleQueryRepository(session)
        payload = aggregate_schedule(
            repo,
            ctx,
            from_date=from_,
            to_date=to,
        )
        rulesets, slots, assignments, tasks, properties = _resolve_self_calendar(
            session, ctx, payload=payload
        )
        return MeScheduleResponse(
            window=SchedulerWindowResponse(
                from_date=payload.from_date,
                to_date=payload.to_date,
            ),
            user_id=payload.user_id,
            weekly_availability=[
                _weekly_slot_to_response(s) for s in payload.weekly_availability
            ],
            rulesets=rulesets,
            slots=slots,
            assignments=assignments,
            tasks=tasks,
            properties=properties,
            leaves=[_leave_view_to_response(v) for v in payload.leaves],
            overrides=[_override_view_to_response(v) for v in payload.overrides],
            bookings=[_booking_to_response(b) for b in payload.bookings],
        )

    @api.post(
        "/leaves",
        status_code=status.HTTP_201_CREATED,
        response_model=UserLeaveResponse,
        operation_id="me.leaves.create",
        summary="Create a leave for the caller (always self-target)",
    )
    def create_self_leave(
        body: MeLeaveCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> UserLeaveResponse:
        """Forward to :func:`create_leave` with ``user_id = ctx.actor_id``.

        Always lands pending per spec §12 "Self-service shortcuts" —
        ``creates user_leave with approval_required always true``.
        The router passes ``force_pending=True`` so even a manager
        self-submitting through ``/me/leaves`` lands pending; a
        manager wanting to retroactively self-log + auto-approve
        uses the generic ``POST /user_leaves`` endpoint.
        """
        service_body = UserLeaveCreate(
            user_id=ctx.actor_id,
            starts_on=body.starts_on,
            ends_on=body.ends_on,
            category=body.category,
            note_md=body.note_md,
        )
        repo, checker = make_leave_seam_pair(session, ctx)
        try:
            view = create_leave(
                repo, checker, ctx, body=service_body, force_pending=True
            )
        except UserLeavePermissionDenied as exc:
            # ``leaves.create_self`` is auto-allowed to ``all_workers``
            # in the default catalog; a 403 here implies a deployment
            # that revoked it explicitly. Re-raised through the §12
            # 403 envelope so the SPA renders the right banner.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "permission_denied", "action_key": str(exc)},
            ) from exc
        except UserLeaveInvariantViolated as exc:
            raise _http_for_leave_invariant(exc) from exc
        return _leave_view_to_response(view)

    @api.get(
        "/availability_overrides",
        response_model=UserAvailabilityOverrideListResponse,
        operation_id="me.availability_overrides.list",
        summary="List the caller's user_availability_override rows",
        openapi_extra={"x-cli": {"group": "me", "verb": "availability-list"}},
    )
    def list_self_overrides(
        ctx: _Ctx,
        session: _Db,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> UserAvailabilityOverrideListResponse:
        """Cursor-paginated listing keyed to ``ctx.actor_id``.

        Per spec §12 "Self-service shortcuts": ``self-only list of
        every user_availability_override (any approval state)``. The
        domain helper :func:`list_overrides` is invoked with
        ``user_id = ctx.actor_id`` so a worker cannot widen the
        listing to cross-user; the underlying capability check
        ``availability_overrides.create_self`` is permissive for
        self-target reads (a self-keyed listing is always allowed
        by ``_gate_or_self``).
        """
        after_id = decode_cursor(cursor)
        filters = UserAvailabilityOverrideListFilter(user_id=ctx.actor_id)
        repo, checker = make_override_seam_pair(session, ctx)
        views = list_overrides(
            repo,
            checker,
            ctx,
            filters=filters,
            limit=limit,
            after_id=after_id,
        )
        page = paginate(views, limit=limit, key_getter=lambda v: v.id)
        return UserAvailabilityOverrideListResponse(
            data=[_override_view_to_response(v) for v in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @api.post(
        "/availability_overrides",
        status_code=status.HTTP_201_CREATED,
        response_model=UserAvailabilityOverrideResponse,
        operation_id="me.availability_overrides.create",
        summary="Create an availability override for the caller (always self-target)",
        responses={
            status.HTTP_409_CONFLICT: {
                "description": (
                    "An override row already covers this ``(user_id, date)`` "
                    "pair (``override_exists``)."
                ),
                "content": {
                    "application/problem+json": {
                        "schema": {
                            "type": "object",
                            "additionalProperties": True,
                        }
                    }
                },
            },
        },
    )
    def create_self_override(
        body: MeAvailabilityOverrideCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> UserAvailabilityOverrideResponse:
        """Forward to :func:`create_override` with ``user_id = ctx.actor_id``.

        Server computes ``approval_required`` per the §06 "Approval
        logic (hybrid model)" matrix — adding hours auto-approves,
        narrowing or removing requires manager sign-off. The resolved
        state lands on the response so the UI does not need to
        re-derive it. A duplicate ``(user_id, date)`` lands a 409
        ``override_exists`` envelope rather than the previous opaque
        500 from the UNIQUE IntegrityError.
        """
        service_body = UserAvailabilityOverrideCreate(
            user_id=ctx.actor_id,
            date=body.date,
            available=body.available,
            starts_local=body.starts_local,
            ends_local=body.ends_local,
            reason=body.reason,
        )
        repo, checker = make_override_seam_pair(session, ctx)
        try:
            view = create_override(repo, checker, ctx, body=service_body)
        except UserAvailabilityOverridePermissionDenied as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "permission_denied", "action_key": str(exc)},
            ) from exc
        except UserAvailabilityOverrideInvariantViolated as exc:
            raise _http_for_override_invariant(exc) from exc
        except UserAvailabilityOverrideAlreadyExists as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "override_exists", "message": str(exc)},
            ) from exc
        return _override_view_to_response(view)

    return api


router = build_me_schedule_router()
