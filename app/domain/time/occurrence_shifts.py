"""Occurrence lifecycle bridge for shift clock state."""

from __future__ import annotations

import threading
from collections.abc import Callable
from weakref import WeakSet

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import PropertyWorkspace
from app.adapters.db.tasks.models import Occurrence, TaskTemplate
from app.adapters.db.time.models import Shift
from app.audit import write_audit
from app.domain.time.shifts import (
    close_shift,
    find_shift_by_source_occurrence,
    open_shift,
)
from app.events import EventBus, TaskOccurrenceCompleted, TaskOccurrenceStarted
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock

__all__ = [
    "SessionContextProvider",
    "handle_occurrence_completed",
    "handle_occurrence_started",
    "register_occurrence_shift_subscription",
]


type OccurrenceShiftEvent = TaskOccurrenceStarted | TaskOccurrenceCompleted
type SessionContextProvider = Callable[
    [OccurrenceShiftEvent], tuple[Session, WorkspaceContext] | None
]

_SUBSCRIBED_BUSES: WeakSet[EventBus] = WeakSet()
_SUBSCRIBED_BUSES_LOCK = threading.Lock()


def register_occurrence_shift_subscription(
    event_bus: EventBus,
    *,
    session_provider: SessionContextProvider,
) -> None:
    """Subscribe occurrence start/complete events to shift auto-open/close."""
    with _SUBSCRIBED_BUSES_LOCK:
        if event_bus in _SUBSCRIBED_BUSES:
            return
        _SUBSCRIBED_BUSES.add(event_bus)

    @event_bus.subscribe(TaskOccurrenceStarted)
    def _on_occurrence_started(event: TaskOccurrenceStarted) -> None:
        bound = session_provider(event)
        if bound is None:
            return
        session, ctx = bound
        handle_occurrence_started(event, session=session, ctx=ctx)

    @event_bus.subscribe(TaskOccurrenceCompleted)
    def _on_occurrence_completed(event: TaskOccurrenceCompleted) -> None:
        bound = session_provider(event)
        if bound is None:
            return
        session, ctx = bound
        handle_occurrence_completed(event, session=session, ctx=ctx)


def handle_occurrence_started(
    event: TaskOccurrenceStarted,
    *,
    session: Session,
    ctx: WorkspaceContext,
) -> None:
    """Open an occurrence-sourced shift when template/property config enables it."""
    occurrence = _load_occurrence(session, ctx, occurrence_id=event.task_id)
    if occurrence is None or not _auto_shift_enabled(session, ctx, occurrence):
        return
    if (
        find_shift_by_source_occurrence(session, ctx, occurrence_id=occurrence.id)
        is not None
    ):
        return

    target_user_id = occurrence.assignee_user_id or event.started_by
    existing_shift = _find_open_shift(session, ctx, user_id=target_user_id)
    event_clock = FrozenClock(event.occurred_at)
    if existing_shift is not None:
        write_audit(
            session,
            ctx,
            entity_kind="shift",
            entity_id=existing_shift.id,
            action="shift.auto_open_skipped",
            diff={
                "occurrence_id": occurrence.id,
                "user_id": target_user_id,
                "property_id": occurrence.property_id,
                "existing_shift_id": existing_shift.id,
                "existing_source": existing_shift.source,
                "reason": "existing_shift_open",
            },
            clock=event_clock,
        )
        return

    open_shift(
        session,
        ctx,
        user_id=target_user_id,
        property_id=occurrence.property_id,
        source="occurrence",
        source_occurrence_id=occurrence.id,
        clock=event_clock,
    )


def handle_occurrence_completed(
    event: TaskOccurrenceCompleted,
    *,
    session: Session,
    ctx: WorkspaceContext,
) -> None:
    """Close the open shift that was derived from the completed occurrence."""
    occurrence = _load_occurrence(session, ctx, occurrence_id=event.task_id)
    if occurrence is None:
        return
    shift = find_shift_by_source_occurrence(session, ctx, occurrence_id=occurrence.id)
    if shift is None or shift.ends_at is not None:
        return
    event_clock = FrozenClock(event.occurred_at)
    close_shift(
        session,
        ctx,
        shift_id=shift.id,
        ends_at=event.occurred_at,
        clock=event_clock,
    )


def _load_occurrence(
    session: Session,
    ctx: WorkspaceContext,
    *,
    occurrence_id: str,
) -> Occurrence | None:
    stmt = select(Occurrence).where(
        Occurrence.workspace_id == ctx.workspace_id,
        Occurrence.id == occurrence_id,
    )
    return session.scalars(stmt).one_or_none()


def _auto_shift_enabled(
    session: Session,
    ctx: WorkspaceContext,
    occurrence: Occurrence,
) -> bool:
    template_enabled = False
    if occurrence.template_id is not None:
        template_enabled = bool(
            session.scalar(
                select(TaskTemplate.auto_shift_from_occurrence).where(
                    TaskTemplate.workspace_id == ctx.workspace_id,
                    TaskTemplate.id == occurrence.template_id,
                )
            )
        )
    property_enabled = False
    if occurrence.property_id is not None:
        property_enabled = bool(
            session.scalar(
                select(PropertyWorkspace.auto_shift_from_occurrence).where(
                    PropertyWorkspace.workspace_id == ctx.workspace_id,
                    PropertyWorkspace.property_id == occurrence.property_id,
                )
            )
        )
    return template_enabled or property_enabled


def _find_open_shift(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
) -> Shift | None:
    stmt = select(Shift).where(
        Shift.workspace_id == ctx.workspace_id,
        Shift.user_id == user_id,
        Shift.ends_at.is_(None),
    )
    return session.scalars(stmt).first()
