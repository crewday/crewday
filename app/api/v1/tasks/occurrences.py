"""Occurrence routes exposed under the product label tasks."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, status
from sqlalchemy import or_, select

from app.adapters.db.tasks.models import ChecklistItem, Occurrence
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.domain.tasks.assignment import TaskNotFound as AssignTaskNotFound
from app.domain.tasks.assignment import assign_task
from app.domain.tasks.completion import (
    EvidenceRequired,
    InvalidStateTransition,
    PhotoForbidden,
    RequiredChecklistIncomplete,
    SkipNotPermitted,
)
from app.domain.tasks.completion import PermissionDenied as CompletionPermissionDenied
from app.domain.tasks.completion import TaskNotFound as CompletionTaskNotFound
from app.domain.tasks.completion import cancel as cancel_task
from app.domain.tasks.completion import complete as complete_task
from app.domain.tasks.completion import skip as skip_task
from app.domain.tasks.completion import start as start_task
from app.domain.tasks.oneoff import (
    InvalidLocalDatetime,
    PersonalAssignmentError,
    TaskCreate,
    TaskFieldInvalid,
    TaskPatch,
    create_oneoff,
    read_task,
    update_task,
)
from app.domain.tasks.oneoff import TaskNotFound as OneOffTaskNotFound
from app.domain.tasks.templates import TaskTemplateNotFound

from .deps import _Ctx, _Db, _task_lifecycle_bus
from .derived import _TERMINAL_STATES
from .detail import _property_timezone, _resolve_zones_for_views, _task_detail_payload
from .errors import _http, _http_for_task_mutation, _task_not_found
from .payloads import (
    AssignmentPayload,
    AssignRequest,
    ChecklistPatchRequest,
    CompleteRequest,
    ReasonRequest,
    TaskChecklistItemPayload,
    TaskDetailPayload,
    TaskListResponse,
    TaskPayload,
    TaskStatePayload,
)

router = APIRouter()

_OccurrenceState = Literal[
    "scheduled", "pending", "in_progress", "done", "skipped", "cancelled", "overdue"
]


@router.get(
    "/tasks",
    response_model=TaskListResponse,
    operation_id="list_tasks",
    summary="List occurrences (tasks) with filters",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "list"}},
)
def list_tasks_route(
    ctx: _Ctx,
    session: _Db,
    state: Annotated[_OccurrenceState | None, Query()] = None,
    assignee_user_id: Annotated[str | None, Query(max_length=64)] = None,
    property_id: Annotated[str | None, Query(max_length=64)] = None,
    scheduled_for_utc_gte: Annotated[datetime | None, Query()] = None,
    scheduled_for_utc_lt: Annotated[datetime | None, Query()] = None,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> TaskListResponse:
    """Cursor-paginated list with workspace-scoped filters.

    Personal tasks (``is_personal=True``) are visible to their creator
    and to workspace owners only — the §15 read layer's personal-task
    gate is applied inline so the §12 listing surface honours the same
    rule.
    """
    after_id = decode_cursor(cursor)
    now = datetime.now(tz=ZoneInfo("UTC"))
    stmt = select(Occurrence).where(Occurrence.workspace_id == ctx.workspace_id)
    if state is not None:
        if state == "overdue":
            # cd-hurw: ``overdue`` is now a real DB state, but the
            # sweeper only flips a row at most every 5 minutes — a
            # task that slipped 30 seconds ago is still
            # ``state='pending'`` until the next tick. Cover both:
            # rows the sweeper has already visited (``state='overdue'``
            # OR ``overdue_since IS NOT NULL``) and rows the sweeper
            # has not reached yet (``state IN (pending, in_progress)``
            # AND ``starts_at < now``). Mirror of
            # :func:`_compute_overdue`'s prefer-column-then-time logic.
            stmt = stmt.where(
                or_(
                    Occurrence.state == "overdue",
                    Occurrence.overdue_since.is_not(None),
                    (Occurrence.state.in_(("pending", "in_progress")))
                    & (Occurrence.starts_at < now),
                )
            )
        else:
            stmt = stmt.where(Occurrence.state == state)
    if assignee_user_id is not None:
        stmt = stmt.where(Occurrence.assignee_user_id == assignee_user_id)
    if property_id is not None:
        stmt = stmt.where(Occurrence.property_id == property_id)
    if scheduled_for_utc_gte is not None:
        stmt = stmt.where(Occurrence.starts_at >= scheduled_for_utc_gte)
    if scheduled_for_utc_lt is not None:
        stmt = stmt.where(Occurrence.starts_at < scheduled_for_utc_lt)
    if after_id is not None:
        stmt = stmt.where(Occurrence.id > after_id)
    stmt = stmt.order_by(Occurrence.id.asc()).limit(limit + 1)
    rows = list(session.scalars(stmt).all())
    # Personal-task visibility. Owners see everything; every other
    # caller sees only the tasks they created.
    if not ctx.actor_was_owner_member:
        rows = [
            r for r in rows if not r.is_personal or r.created_by_user_id == ctx.actor_id
        ]
    # Project and paginate. The domain read helper
    # :func:`app.domain.tasks.oneoff.read_task` builds the view shape
    # we want; re-using it keeps the projection path single-sourced.
    views = [read_task(session, ctx, task_id=row.id) for row in rows]
    zones = _resolve_zones_for_views(session, views)
    page = paginate(views, limit=limit, key_getter=lambda v: v.id)
    return TaskListResponse(
        data=[
            TaskPayload.from_view(
                v,
                property_timezone=zones.get(v.property_id)
                if v.property_id is not None
                else None,
                now_utc=now,
            )
            for v in page.items
        ],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.post(
    "/tasks",
    status_code=status.HTTP_201_CREATED,
    response_model=TaskPayload,
    operation_id="create_task",
    summary="Create a one-off task",
)
def create_task_route(
    body: TaskCreate,
    ctx: _Ctx,
    session: _Db,
) -> TaskPayload:
    """Ad-hoc create — see :func:`app.domain.tasks.oneoff.create_oneoff`."""
    try:
        view = create_oneoff(session, ctx, payload=body)
    except (
        TaskTemplateNotFound,
        PersonalAssignmentError,
    ) as exc:
        raise _http_for_task_mutation(exc) from exc
    zone = _property_timezone(session, view.property_id)
    return TaskPayload.from_view(view, property_timezone=zone)


@router.get(
    "/tasks/{task_id}",
    response_model=TaskPayload,
    operation_id="get_task",
    summary="Read a single task",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "show"}},
)
def get_task_route(
    task_id: str,
    ctx: _Ctx,
    session: _Db,
) -> TaskPayload:
    """Return the task identified by ``task_id``; 404 cross-tenant."""
    try:
        view = read_task(session, ctx, task_id=task_id)
    except OneOffTaskNotFound as exc:
        raise _task_not_found() from exc
    zone = _property_timezone(session, view.property_id)
    return TaskPayload.from_view(view, property_timezone=zone)


@router.get(
    "/tasks/{task_id}/detail",
    response_model=TaskDetailPayload,
    operation_id="get_task_detail",
    summary="Read worker task detail with property, instructions, and checklist",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "detail"}},
)
def get_task_detail_route(
    task_id: str,
    ctx: _Ctx,
    session: _Db,
) -> TaskDetailPayload:
    """Return the worker detail envelope; 404 on invisible tasks."""
    try:
        view = read_task(session, ctx, task_id=task_id)
    except OneOffTaskNotFound as exc:
        raise _task_not_found() from exc
    return _task_detail_payload(session, ctx, view)


@router.patch(
    "/tasks/{task_id}",
    response_model=TaskPayload,
    operation_id="patch_task",
    summary="Partial update of a task (full §06 mutable set)",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "update"}},
)
def patch_task_route(
    task_id: str,
    body: TaskPatch,
    ctx: _Ctx,
    session: _Db,
) -> TaskPayload:
    """PATCH — see :class:`app.domain.tasks.oneoff.TaskPatch`.

    Carries the full §06 "Task row" mutable set (cd-43wv): title,
    description_md, scheduled_for_local, property_id, area_id,
    unit_id, expected_role_id, priority, duration_minutes,
    photo_evidence. Each field is independently validated; the
    relationship checks (area / unit must belong to the resolved
    property; property must belong to the workspace; role must be a
    live workspace row) surface as ``422 invalid_task_field``. A
    malformed ``scheduled_for_local`` lands as ``422 invalid_field``.

    Reassignment / availability re-resolution after a property or
    schedule change lives on the dedicated reschedule + reassign
    verbs (``/scheduler/tasks/{id}/reschedule``,
    ``/scheduler/tasks/{id}/reassign``). PATCH only writes through
    and emits :class:`~app.events.types.TaskUpdated`; the SPA's SSE
    reducer invalidates the affected caches.
    """
    try:
        view = update_task(session, ctx, task_id=task_id, body=body)
    except (OneOffTaskNotFound, TaskFieldInvalid) as exc:
        # ``_http_for_task_mutation`` is the single mapping table for
        # task-domain exceptions; routing through it keeps the
        # ``invalid_task_field`` envelope identical to every other
        # task verb (start / complete / skip / cancel) so the SPA's
        # error-toast renderer doesn't have to special-case PATCH.
        raise _http_for_task_mutation(exc) from exc
    except InvalidLocalDatetime as exc:
        # ``_parse_local_datetime`` raises ``InvalidLocalDatetime`` on a
        # malformed or tz-aware ``scheduled_for_local``. Distinct from
        # other ``ValueError``s the service may raise (e.g. clock
        # contract violations) so we don't accidentally squash an
        # internal bug under a 422.
        raise _http(422, "invalid_field", message=str(exc)) from exc
    zone = _property_timezone(session, view.property_id)
    return TaskPayload.from_view(view, property_timezone=zone)


@router.patch(
    "/tasks/{task_id}/checklist/{item_id}",
    response_model=TaskChecklistItemPayload,
    operation_id="patch_task_checklist_item",
    summary="Idempotently tick or untick a task checklist item",
    openapi_extra={"x-cli": {"group": "tasks", "verb": "checklist"}},
)
def patch_task_checklist_item_route(
    task_id: str,
    item_id: str,
    body: ChecklistPatchRequest,
    ctx: _Ctx,
    session: _Db,
) -> TaskChecklistItemPayload:
    """Tick/untick a checklist row visible through the parent task."""
    try:
        view = read_task(session, ctx, task_id=task_id)
    except OneOffTaskNotFound as exc:
        raise _task_not_found() from exc
    row = session.scalar(
        select(ChecklistItem).where(
            ChecklistItem.id == item_id,
            ChecklistItem.workspace_id == ctx.workspace_id,
            ChecklistItem.occurrence_id == task_id,
        )
    )
    if row is None:
        raise _task_not_found()
    if view.state in _TERMINAL_STATES:
        raise _http(
            status.HTTP_409_CONFLICT,
            "task_terminal",
            message="Checklist items cannot be changed after the task is terminal.",
            state=view.state,
        )
    if body.checked and not row.checked:
        row.checked = True
        row.checked_at = datetime.now(tz=UTC)
    elif not body.checked and row.checked:
        row.checked = False
        row.checked_at = None
    session.flush()
    return TaskChecklistItemPayload.from_row(row)


@router.post(
    "/tasks/{task_id}/assign",
    response_model=AssignmentPayload,
    operation_id="assign_task",
    summary="Assign a task to a specific user",
)
def assign_task_route(
    task_id: str,
    body: AssignRequest,
    ctx: _Ctx,
    session: _Db,
) -> AssignmentPayload:
    """Write ``assigned_user_id=body.assignee_user_id`` through the algorithm.

    Delegates to :func:`app.domain.tasks.assignment.assign_task` with
    the override path (no auto-pool walk). The response echoes the
    :class:`AssignmentResult` shape — ``assigned_user_id``,
    ``assignment_source``, ``candidate_count``, ``backup_index``, and
    the task's current ``state`` so the SPA can refresh the chip
    without a follow-up GET.
    """
    try:
        result = assign_task(
            session, ctx, task_id, override_user_id=body.assignee_user_id
        )
    except AssignTaskNotFound as exc:
        raise _task_not_found() from exc
    current_state = session.scalar(
        select(Occurrence.state).where(
            Occurrence.id == result.task_id,
            Occurrence.workspace_id == ctx.workspace_id,
        )
    )
    return AssignmentPayload(
        task_id=result.task_id,
        assigned_user_id=result.assigned_user_id,
        assignment_source=result.source,
        candidate_count=result.candidate_count,
        backup_index=result.backup_index,
        state=current_state or "",
    )


@router.post(
    "/tasks/{task_id}/start",
    response_model=TaskStatePayload,
    operation_id="start_task",
    summary="Drive a task from pending to in_progress",
)
def start_task_route(
    task_id: str,
    ctx: _Ctx,
    session: _Db,
) -> TaskStatePayload:
    """Delegate to :func:`app.domain.tasks.completion.start`."""
    try:
        view = start_task(
            session,
            ctx,
            task_id,
            event_bus=_task_lifecycle_bus(session, ctx),
        )
    except (
        CompletionTaskNotFound,
        InvalidStateTransition,
        CompletionPermissionDenied,
    ) as exc:
        raise _http_for_task_mutation(exc) from exc
    return TaskStatePayload.from_view(view)


@router.post(
    "/tasks/{task_id}/complete",
    response_model=TaskStatePayload,
    operation_id="complete_task",
    summary="Mark a task done — gated by evidence + checklist policy",
)
def complete_task_route(
    task_id: str,
    body: CompleteRequest,
    ctx: _Ctx,
    session: _Db,
) -> TaskStatePayload:
    """Delegate to :func:`app.domain.tasks.completion.complete`.

    ``Idempotency-Key`` replay is handled by the process-wide
    middleware; no per-route logic needed.
    """
    try:
        event_bus = _task_lifecycle_bus(session, ctx)
        view = complete_task(
            session,
            ctx,
            task_id,
            note_md=body.note_md,
            photo_evidence_ids=body.photo_evidence_ids,
            event_bus=event_bus,
        )
    except (
        CompletionTaskNotFound,
        InvalidStateTransition,
        PhotoForbidden,
        EvidenceRequired,
        RequiredChecklistIncomplete,
        CompletionPermissionDenied,
    ) as exc:
        raise _http_for_task_mutation(exc) from exc
    return TaskStatePayload.from_view(view)


@router.post(
    "/tasks/{task_id}/skip",
    response_model=TaskStatePayload,
    operation_id="skip_task",
    summary="Skip a task with a reason",
)
def skip_task_route(
    task_id: str,
    body: ReasonRequest,
    ctx: _Ctx,
    session: _Db,
) -> TaskStatePayload:
    """Delegate to :func:`app.domain.tasks.completion.skip`."""
    try:
        view = skip_task(session, ctx, task_id, reason=body.reason_md)
    except (
        CompletionTaskNotFound,
        InvalidStateTransition,
        SkipNotPermitted,
        CompletionPermissionDenied,
    ) as exc:
        raise _http_for_task_mutation(exc) from exc
    return TaskStatePayload.from_view(view)


@router.post(
    "/tasks/{task_id}/cancel",
    response_model=TaskStatePayload,
    operation_id="cancel_task",
    summary="Cancel a task with a reason (manager / owner only)",
)
def cancel_task_route(
    task_id: str,
    body: ReasonRequest,
    ctx: _Ctx,
    session: _Db,
) -> TaskStatePayload:
    """Delegate to :func:`app.domain.tasks.completion.cancel`."""
    try:
        view = cancel_task(session, ctx, task_id, reason=body.reason_md)
    except (
        CompletionTaskNotFound,
        InvalidStateTransition,
        CompletionPermissionDenied,
    ) as exc:
        raise _http_for_task_mutation(exc) from exc
    return TaskStatePayload.from_view(view)
