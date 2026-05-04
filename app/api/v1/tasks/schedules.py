"""Schedule routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.domain.tasks.schedules import (
    InvalidBackupWorkRole,
    InvalidRRule,
    ScheduleCreate,
    ScheduleNotFound,
    ScheduleUpdate,
    list_schedules,
    preview_occurrences,
)
from app.domain.tasks.schedules import create as create_schedule
from app.domain.tasks.schedules import delete as delete_schedule
from app.domain.tasks.schedules import pause as pause_schedule
from app.domain.tasks.schedules import read as read_schedule
from app.domain.tasks.schedules import resume as resume_schedule
from app.domain.tasks.schedules import update as update_schedule
from app.domain.tasks.templates import read_many as read_many_templates

from .deps import _Ctx, _Db
from .errors import _http_for_schedule_mutation, _schedule_not_found
from .payloads import (
    OccurrencePreviewItem,
    ScheduleListResponse,
    SchedulePayload,
    SchedulePreviewResponse,
    TaskTemplatePayload,
)

router = APIRouter()


def _parse_preview_window(value: str) -> int:
    """Parse ``30d`` and ISO-8601 ``P30D`` day windows."""
    normalized = value.strip().lower()
    if normalized.startswith("p"):
        normalized = normalized[1:]
    if not normalized.endswith("d"):
        raise ValueError("preview window must use day notation, e.g. 30d or P30D")
    raw_days = normalized[:-1]
    if not raw_days.isdecimal():
        raise ValueError("preview window must use day notation, e.g. 30d or P30D")
    return int(raw_days)


@router.get(
    "/schedules",
    response_model=ScheduleListResponse,
    operation_id="list_schedules",
    summary="List schedules in the caller's workspace",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "schedules-list"}},
)
def list_schedules_route(
    ctx: _Ctx,
    session: _Db,
    template_id: Annotated[str | None, Query(max_length=64)] = None,
    property_id: Annotated[str | None, Query(max_length=64)] = None,
    paused: Annotated[bool | None, Query()] = None,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> ScheduleListResponse:
    """Return a cursor-paginated page of live schedules.

    Each page also carries a ``templates_by_id`` sidecar holding every
    ``task_template`` the page's schedules reference — bundled in one
    SELECT so the SPA's Schedules page can join template metadata
    (name, role, …) without a second round-trip. The sidecar is
    pagination-scoped (only this page's templates), so payload size
    scales with the page rather than the workspace.
    """
    after_id = decode_cursor(cursor)
    views = list(
        list_schedules(
            session,
            ctx,
            template_id=template_id,
            property_id=property_id,
            paused=paused,
        )
    )
    if after_id is not None:
        views = [v for v in views if v.id > after_id]
    page = paginate(views, limit=limit, key_getter=lambda v: v.id)
    schedule_payloads = [SchedulePayload.from_view(v) for v in page.items]
    template_ids = [s.template_id for s in schedule_payloads]
    templates_by_id = {
        view.id: TaskTemplatePayload.from_view(view)
        for view in read_many_templates(session, ctx, template_ids=template_ids)
    }
    return ScheduleListResponse(
        data=schedule_payloads,
        next_cursor=page.next_cursor,
        has_more=page.has_more,
        templates_by_id=templates_by_id,
    )


@router.post(
    "/schedules",
    status_code=status.HTTP_201_CREATED,
    response_model=SchedulePayload,
    operation_id="create_schedule",
    summary="Create a schedule",
)
def create_schedule_route(
    body: ScheduleCreate,
    ctx: _Ctx,
    session: _Db,
) -> SchedulePayload:
    """Insert a fresh schedule row; validates RRULE + DTSTART."""
    try:
        view = create_schedule(session, ctx, body=body)
    except (InvalidRRule, InvalidBackupWorkRole, ValueError) as exc:
        raise _http_for_schedule_mutation(exc) from exc
    return SchedulePayload.from_view(view)


@router.get(
    "/schedules/{schedule_id}",
    response_model=SchedulePayload,
    operation_id="get_schedule",
    summary="Read a schedule",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "schedule-show"}},
)
def get_schedule_route(
    schedule_id: str,
    ctx: _Ctx,
    session: _Db,
) -> SchedulePayload:
    """Return the schedule identified by ``schedule_id``."""
    try:
        view = read_schedule(session, ctx, schedule_id=schedule_id)
    except ScheduleNotFound as exc:
        raise _schedule_not_found() from exc
    return SchedulePayload.from_view(view)


@router.patch(
    "/schedules/{schedule_id}",
    response_model=SchedulePayload,
    operation_id="update_schedule",
    summary="Replace the mutable body of a schedule",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "schedule-update"}},
)
def patch_schedule_route(
    schedule_id: str,
    body: ScheduleUpdate,
    ctx: _Ctx,
    session: _Db,
    apply_to_existing: Annotated[bool, Query()] = False,
) -> SchedulePayload:
    """PATCH = full-body replace; ``apply_to_existing`` cascades."""
    try:
        view = update_schedule(
            session,
            ctx,
            schedule_id=schedule_id,
            body=body,
            apply_to_existing=apply_to_existing,
        )
    except (ScheduleNotFound, InvalidRRule, InvalidBackupWorkRole, ValueError) as exc:
        raise _http_for_schedule_mutation(exc) from exc
    return SchedulePayload.from_view(view)


@router.delete(
    "/schedules/{schedule_id}",
    response_model=SchedulePayload,
    operation_id="delete_schedule",
    summary="Soft-delete a schedule and cancel scheduled occurrences",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "schedule-delete"}},
)
def delete_schedule_route(
    schedule_id: str,
    ctx: _Ctx,
    session: _Db,
) -> SchedulePayload:
    """Soft-delete the schedule."""
    try:
        view = delete_schedule(session, ctx, schedule_id=schedule_id)
    except ScheduleNotFound as exc:
        raise _schedule_not_found() from exc
    return SchedulePayload.from_view(view)


@router.get(
    "/schedules/{schedule_id}/preview",
    response_model=SchedulePreviewResponse,
    operation_id="preview_schedule",
    summary="Return upcoming occurrences of a schedule",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "schedule-preview"}},
)
def preview_schedule_route(
    schedule_id: str,
    ctx: _Ctx,
    session: _Db,
    n: Annotated[int, Query(ge=1, le=100)] = 5,
    preview_for: Annotated[
        str | None,
        Query(
            alias="for",
            max_length=16,
            description=(
                "Half-open day window to preview, e.g. ``30d`` or ISO-8601 ``P30D``. "
                "When present, the legacy ``n`` count is ignored."
            ),
        ),
    ] = None,
) -> SchedulePreviewResponse:
    """Return ``?for=30d`` / ``?for=P30D`` windows, or legacy next-``n`` preview."""
    try:
        schedule = read_schedule(session, ctx, schedule_id=schedule_id)
    except ScheduleNotFound as exc:
        raise _schedule_not_found() from exc
    try:
        window_days = (
            _parse_preview_window(preview_for) if preview_for is not None else None
        )
        moments = preview_occurrences(
            schedule.rrule,
            schedule.dtstart_local,
            n=n,
            window_days=window_days,
            rdate_local=schedule.rdate_local,
            exdate_local=schedule.exdate_local,
        )
    except InvalidRRule as exc:
        raise _http_for_schedule_mutation(exc) from exc
    except ValueError as exc:
        raise _http_for_schedule_mutation(exc) from exc
    return SchedulePreviewResponse(
        occurrences=[
            OccurrencePreviewItem(starts_local=m.isoformat(timespec="minutes"))
            for m in moments
        ]
    )


@router.post(
    "/schedules/{schedule_id}/pause",
    response_model=SchedulePayload,
    operation_id="pause_schedule",
    summary="Pause a schedule without cancelling materialised tasks",
)
def pause_schedule_route(
    schedule_id: str,
    ctx: _Ctx,
    session: _Db,
) -> SchedulePayload:
    """Set ``paused_at``; no cascade."""
    try:
        view = pause_schedule(session, ctx, schedule_id=schedule_id)
    except ScheduleNotFound as exc:
        raise _schedule_not_found() from exc
    return SchedulePayload.from_view(view)


@router.post(
    "/schedules/{schedule_id}/resume",
    response_model=SchedulePayload,
    operation_id="resume_schedule",
    summary="Resume a paused schedule",
)
def resume_schedule_route(
    schedule_id: str,
    ctx: _Ctx,
    session: _Db,
) -> SchedulePayload:
    """Clear ``paused_at``."""
    try:
        view = resume_schedule(session, ctx, schedule_id=schedule_id)
    except ScheduleNotFound as exc:
        raise _schedule_not_found() from exc
    return SchedulePayload.from_view(view)
