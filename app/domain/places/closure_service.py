"""``property_closure`` CRUD and clash detection.

Closures are blackout windows on a property. The table is not directly
workspace-scoped, so every read and write reaches tenancy by joining
``property_closure`` -> ``property`` -> ``property_workspace``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import or_, select, true
from sqlalchemy.orm import Session

from app.adapters.db.places.models import (
    Area,
    Property,
    PropertyClosure,
    PropertyWorkspace,
    Unit,
)
from app.adapters.db.stays.models import IcalFeed, Reservation
from app.adapters.db.tasks.models import Schedule
from app.audit import write_audit
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import PropertyClosureCreated, PropertyClosureUpdated
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "ClosureClashes",
    "ClosureNotFound",
    "ClosureReason",
    "ClosureScheduleClash",
    "ClosureStayClash",
    "PropertyClosureCreate",
    "PropertyClosureUpdate",
    "PropertyClosureView",
    "create_closure",
    "delete_closure",
    "detect_clashes",
    "get_closure",
    "list_closures",
    "update_closure",
]


ClosureReason = Literal[
    "renovation",
    "owner_stay",
    "seasonal",
    "ical_unavailable",
    "other",
]

_MAX_ID_LEN = 64


class ClosureNotFound(LookupError):
    """The requested closure or parent property is invisible to the caller."""


class _ClosureBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starts_at: datetime
    ends_at: datetime
    reason: ClosureReason
    unit_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    source_ical_feed_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _datetime_must_be_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("closure datetimes must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def _ends_after_starts(self) -> _ClosureBody:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class PropertyClosureCreate(_ClosureBody):
    """Request body for :func:`create_closure`."""


class PropertyClosureUpdate(_ClosureBody):
    """Full replacement body for :func:`update_closure`."""


@dataclass(frozen=True, slots=True)
class PropertyClosureView:
    id: str
    property_id: str
    unit_id: str | None
    starts_at: datetime
    ends_at: datetime
    reason: ClosureReason
    source_ical_feed_id: str | None
    created_by_user_id: str | None
    created_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class ClosureStayClash:
    id: str
    property_id: str
    unit_id: str | None
    check_in: datetime
    check_out: datetime
    status: str
    source: str


@dataclass(frozen=True, slots=True)
class ClosureScheduleClash:
    id: str
    property_id: str | None
    unit_id: str | None
    name: str
    active_from: date | None
    active_until: date | None
    paused_at: datetime | None


@dataclass(frozen=True, slots=True)
class ClosureClashes:
    stays: tuple[ClosureStayClash, ...]
    schedules: tuple[ClosureScheduleClash, ...]


def create_closure(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    body: PropertyClosureCreate,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> PropertyClosureView:
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()

    _load_property_in_workspace(session, ctx, property_id=property_id)
    _assert_unit_in_property(session, property_id=property_id, unit_id=body.unit_id)
    _assert_feed_in_property(
        session,
        ctx,
        property_id=property_id,
        source_ical_feed_id=body.source_ical_feed_id,
    )

    row = PropertyClosure(
        id=new_ulid(),
        property_id=property_id,
        unit_id=body.unit_id,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        reason=body.reason,
        source_ical_feed_id=body.source_ical_feed_id,
        created_by_user_id=ctx.actor_id if ctx.actor_kind == "user" else None,
        created_at=now,
    )
    session.add(row)
    session.flush()
    view = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="property_closure",
        entity_id=row.id,
        action="create",
        diff={"after": _view_to_diff_dict(view)},
        clock=resolved_clock,
    )
    resolved_bus.publish(
        PropertyClosureCreated(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=now,
            closure_id=row.id,
            property_id=property_id,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            reason=body.reason,
            source_ical_feed_id=body.source_ical_feed_id,
        )
    )
    return view


def update_closure(
    session: Session,
    ctx: WorkspaceContext,
    *,
    closure_id: str,
    body: PropertyClosureUpdate,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> PropertyClosureView:
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()

    row = _load_closure_row(session, ctx, closure_id=closure_id)
    before = _row_to_view(row)
    _assert_feed_in_property(
        session,
        ctx,
        property_id=row.property_id,
        source_ical_feed_id=body.source_ical_feed_id,
    )
    _assert_unit_in_property(session, property_id=row.property_id, unit_id=body.unit_id)

    row.starts_at = body.starts_at
    row.ends_at = body.ends_at
    row.reason = body.reason
    row.unit_id = body.unit_id
    row.source_ical_feed_id = body.source_ical_feed_id
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="property_closure",
        entity_id=row.id,
        action="update",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    resolved_bus.publish(
        PropertyClosureUpdated(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=now,
            closure_id=row.id,
            property_id=row.property_id,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            reason=body.reason,
            source_ical_feed_id=body.source_ical_feed_id,
        )
    )
    return after


def delete_closure(
    session: Session,
    ctx: WorkspaceContext,
    *,
    closure_id: str,
    clock: Clock | None = None,
) -> PropertyClosureView:
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_closure_row(session, ctx, closure_id=closure_id)
    before = _row_to_view(row)
    row.deleted_at = now
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="property_closure",
        entity_id=row.id,
        action="delete",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    session.flush()
    return after


def list_closures(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    unit_id: str | None = None,
) -> Sequence[PropertyClosureView]:
    _load_property_in_workspace(session, ctx, property_id=property_id)
    _assert_unit_in_property(session, property_id=property_id, unit_id=unit_id)
    unit_scope = _optional_unit_applicability_filter(
        PropertyClosure.unit_id, unit_id=unit_id
    )
    rows = session.scalars(
        select(PropertyClosure)
        .where(
            PropertyClosure.property_id == property_id,
            PropertyClosure.deleted_at.is_(None),
            unit_scope,
        )
        .order_by(PropertyClosure.starts_at.asc(), PropertyClosure.id.asc())
    ).all()
    return [_row_to_view(row) for row in rows]


def get_closure(
    session: Session,
    ctx: WorkspaceContext,
    *,
    closure_id: str,
) -> PropertyClosureView:
    row = _load_closure_row(session, ctx, closure_id=closure_id)
    return _row_to_view(row)


def detect_clashes(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    unit_id: str | None = None,
    starts_at: datetime,
    ends_at: datetime,
) -> ClosureClashes:
    if starts_at.tzinfo is None or starts_at.utcoffset() != timedelta(0):
        raise ValueError("starts_at must be timezone-aware UTC")
    if ends_at.tzinfo is None or ends_at.utcoffset() != timedelta(0):
        raise ValueError("ends_at must be timezone-aware UTC")
    if ends_at <= starts_at:
        raise ValueError("ends_at must be after starts_at")

    property_row = _load_property_in_workspace(session, ctx, property_id=property_id)
    _assert_unit_in_property(session, property_id=property_id, unit_id=unit_id)
    closure_start_date, closure_end_date = _covered_dates(
        starts_at, ends_at, timezone=property_row.timezone
    )
    closure_start_iso = closure_start_date.isoformat()
    closure_end_iso = closure_end_date.isoformat()

    reservation_unit_id = (
        select(IcalFeed.unit_id)
        .where(IcalFeed.id == Reservation.ical_feed_id)
        .scalar_subquery()
    )
    stay_rows = session.execute(
        select(Reservation, reservation_unit_id)
        .where(
            Reservation.workspace_id == ctx.workspace_id,
            Reservation.property_id == property_id,
            Reservation.status != "cancelled",
            Reservation.check_in < ends_at,
            Reservation.check_out > starts_at,
            _unit_match_filter(reservation_unit_id, unit_id=unit_id),
        )
        .order_by(Reservation.check_in.asc(), Reservation.id.asc())
    ).all()

    schedule_unit_id = (
        select(Area.unit_id).where(Area.id == Schedule.area_id).scalar_subquery()
    )
    schedule_rows = session.execute(
        select(Schedule, schedule_unit_id)
        .where(
            Schedule.workspace_id == ctx.workspace_id,
            _schedule_property_scope_filter(property_id=property_id, unit_id=unit_id),
            _unit_match_filter(schedule_unit_id, unit_id=unit_id),
            Schedule.deleted_at.is_(None),
            Schedule.paused_at.is_(None),
            Schedule.enabled.is_(True),
            or_(
                Schedule.active_from.is_(None),
                Schedule.active_from <= closure_end_iso,
            ),
            or_(
                Schedule.active_until.is_(None),
                Schedule.active_until >= closure_start_iso,
            ),
        )
        .order_by(Schedule.created_at.asc(), Schedule.id.asc())
    ).all()

    return ClosureClashes(
        stays=tuple(
            _stay_to_clash(row, unit_id=row_unit_id) for row, row_unit_id in stay_rows
        ),
        schedules=tuple(
            _schedule_to_clash(row, unit_id=row_unit_id)
            for row, row_unit_id in schedule_rows
        ),
    )


def _assert_property_in_workspace(
    session: Session, ctx: WorkspaceContext, *, property_id: str
) -> None:
    _load_property_in_workspace(session, ctx, property_id=property_id)


def _load_property_in_workspace(
    session: Session, ctx: WorkspaceContext, *, property_id: str
) -> Property:
    stmt = (
        select(Property)
        .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
        .where(
            Property.id == property_id,
            Property.deleted_at.is_(None),
            PropertyWorkspace.workspace_id == ctx.workspace_id,
        )
    )
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise ClosureNotFound(property_id)
    return row


def _load_closure_row(
    session: Session, ctx: WorkspaceContext, *, closure_id: str
) -> PropertyClosure:
    stmt = (
        select(PropertyClosure)
        .join(Property, Property.id == PropertyClosure.property_id)
        .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
        .where(
            PropertyClosure.id == closure_id,
            PropertyClosure.deleted_at.is_(None),
            Property.deleted_at.is_(None),
            PropertyWorkspace.workspace_id == ctx.workspace_id,
        )
    )
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise ClosureNotFound(closure_id)
    return row


def _assert_feed_in_property(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    source_ical_feed_id: str | None,
) -> None:
    if source_ical_feed_id is None:
        return
    stmt = select(IcalFeed.id).where(
        IcalFeed.id == source_ical_feed_id,
        IcalFeed.workspace_id == ctx.workspace_id,
        IcalFeed.property_id == property_id,
    )
    if session.scalars(stmt).one_or_none() is None:
        raise ClosureNotFound(source_ical_feed_id)


def _assert_unit_in_property(
    session: Session, *, property_id: str, unit_id: str | None
) -> None:
    if unit_id is None:
        return
    stmt = select(Unit.id).where(
        Unit.id == unit_id,
        Unit.property_id == property_id,
        Unit.deleted_at.is_(None),
    )
    if session.scalars(stmt).one_or_none() is None:
        raise ClosureNotFound(unit_id)


def _optional_unit_applicability_filter(column: Any, *, unit_id: str | None) -> Any:
    if unit_id is None:
        return true()
    return or_(column.is_(None), column == unit_id)


def _unit_match_filter(column: Any, *, unit_id: str | None) -> Any:
    if unit_id is None:
        return true()
    return column == unit_id


def _schedule_property_scope_filter(*, property_id: str, unit_id: str | None) -> Any:
    if unit_id is None:
        return or_(Schedule.property_id == property_id, Schedule.property_id.is_(None))
    return Schedule.property_id == property_id


def _row_to_view(row: PropertyClosure) -> PropertyClosureView:
    return PropertyClosureView(
        id=row.id,
        property_id=row.property_id,
        unit_id=row.unit_id,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        reason=_narrow_reason(row.reason),
        source_ical_feed_id=row.source_ical_feed_id,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
        deleted_at=row.deleted_at,
    )


def _narrow_reason(value: str) -> ClosureReason:
    if value == "renovation":
        return "renovation"
    if value == "owner_stay":
        return "owner_stay"
    if value == "seasonal":
        return "seasonal"
    if value == "ical_unavailable":
        return "ical_unavailable"
    if value == "other":
        return "other"
    raise ValueError(f"unknown property_closure.reason {value!r} on loaded row")


def _view_to_diff_dict(view: PropertyClosureView) -> dict[str, Any]:
    return {
        "id": view.id,
        "property_id": view.property_id,
        "unit_id": view.unit_id,
        "starts_at": _iso(view.starts_at),
        "ends_at": _iso(view.ends_at),
        "reason": view.reason,
        "source_ical_feed_id": view.source_ical_feed_id,
        "created_by_user_id": view.created_by_user_id,
        "created_at": _iso(view.created_at),
        "deleted_at": _iso(view.deleted_at) if view.deleted_at is not None else None,
    }


def _stay_to_clash(row: Reservation, *, unit_id: str | None) -> ClosureStayClash:
    return ClosureStayClash(
        id=row.id,
        property_id=row.property_id,
        unit_id=unit_id,
        check_in=row.check_in,
        check_out=row.check_out,
        status=row.status,
        source=row.source,
    )


def _schedule_to_clash(row: Schedule, *, unit_id: str | None) -> ClosureScheduleClash:
    return ClosureScheduleClash(
        id=row.id,
        property_id=row.property_id,
        unit_id=unit_id,
        name=row.name if row.name is not None else "",
        active_from=(
            date.fromisoformat(row.active_from) if row.active_from is not None else None
        ),
        active_until=(
            date.fromisoformat(row.active_until)
            if row.active_until is not None
            else None
        ),
        paused_at=row.paused_at,
    )


def _covered_dates(
    starts_at: datetime, ends_at: datetime, *, timezone: str
) -> tuple[date, date]:
    property_timezone = ZoneInfo(timezone)
    starts_local = starts_at.astimezone(property_timezone)
    ends_local = ends_at.astimezone(property_timezone)
    return starts_local.date(), (ends_local - timedelta(microseconds=1)).date()


def _iso(value: datetime) -> str:
    return value.isoformat()
