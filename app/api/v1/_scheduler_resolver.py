"""Shared rota synthesiser for the scheduler + me-schedule wire feeds.

Both ``GET /scheduler/calendar`` (manager + client + worker views) and
``GET /me/schedule`` (worker self-view) project the same backing rows
into the rota / slot / assignment shape the SPA renders. Until the
``schedule_ruleset`` / ``schedule_ruleset_slot`` tables ship
(§06 "Schedule ruleset (per-property rota)"), both routes synthesise
rulesets from :class:`PropertyWorkRoleAssignment` +
:class:`UserWeeklyAvailability` rows with synthetic
``assignment:<id>`` ruleset ids. When the real tables land, only
this module changes — the routes keep their wire shapes.

The wire DTOs live here so ``scheduler.py`` and ``me_schedule.py``
both share one source of truth and a future column edit lands in one
place.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.adapters.db.availability.models import UserWeeklyAvailability
from app.adapters.db.identity.models import User
from app.adapters.db.places.models import (
    Property,
    PropertyWorkRoleAssignment,
    PropertyWorkspace,
)
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import UserWorkRole, WorkRole
from app.tenancy import WorkspaceContext

__all__ = [
    "AssignmentJoinRow",
    "ScheduleAssignmentResponse",
    "ScheduleRulesetResponse",
    "ScheduleRulesetSlotResponse",
    "SchedulerPropertyResponse",
    "SchedulerTaskResponse",
    "SchedulerUserResponse",
    "SchedulerWindowResponse",
    "assignment_rows_for_window",
    "build_rota_blocks",
    "estimated_minutes",
    "list_workspace_properties",
    "local_date_for_task",
    "property_name",
    "role_names_by_user",
    "ruleset_id_for",
    "scheduled_start_text",
    "task_rows_for_window",
    "time_text",
    "users_by_id",
    "weekly_rows_for_users",
]


# ---------------------------------------------------------------------------
# Response shapes (shared)
# ---------------------------------------------------------------------------


class SchedulerWindowResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_date: date = Field(serialization_alias="from", validation_alias="from")
    to_date: date = Field(serialization_alias="to", validation_alias="to")


class ScheduleRulesetResponse(BaseModel):
    id: str
    workspace_id: str
    name: str


class ScheduleRulesetSlotResponse(BaseModel):
    id: str
    schedule_ruleset_id: str
    weekday: int
    starts_local: str
    ends_local: str


class ScheduleAssignmentResponse(BaseModel):
    id: str
    user_id: str | None
    work_role_id: str | None
    property_id: str
    schedule_ruleset_id: str | None


class SchedulerTaskResponse(BaseModel):
    id: str
    title: str
    property_id: str
    user_id: str
    scheduled_start: str
    estimated_minutes: int
    priority: str
    status: str


class SchedulerUserResponse(BaseModel):
    id: str
    first_name: str
    display_name: str | None = None
    work_role: str | None = None


class SchedulerPropertyResponse(BaseModel):
    id: str
    name: str
    timezone: str


# Loose alias for the four-tuple returned by :func:`assignment_rows_for_window`;
# spelled out so call sites don't have to repeat the four ORM types.
AssignmentJoinRow = tuple[PropertyWorkRoleAssignment, UserWorkRole, WorkRole, User]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def time_text(value: time) -> str:
    """Format a :class:`~datetime.time` as ``HH:MM`` for the wire."""
    return value.strftime("%H:%M")


def property_name(row: Property) -> str:
    """Return the human-facing label for a property (name or address)."""
    return row.name if row.name is not None else row.address


def scheduled_start_text(row: Occurrence) -> str:
    """Property-local ISO start string, falling back to UTC ISO."""
    if row.scheduled_for_local:
        return row.scheduled_for_local
    return row.starts_at.isoformat()


def estimated_minutes(row: Occurrence) -> int:
    """Stored ``duration_minutes`` or derived from ``ends_at - starts_at``."""
    if row.duration_minutes is not None:
        return row.duration_minutes
    delta = row.ends_at - row.starts_at
    return max(1, int(delta.total_seconds() // 60))


def local_date_for_task(
    row: Occurrence,
    *,
    property_timezones: dict[str, str],
) -> date:
    """Compute the property-local date for a task in the window."""
    if row.scheduled_for_local:
        return datetime.fromisoformat(row.scheduled_for_local).date()
    starts_at = row.starts_at
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=UTC)
    tz_name = property_timezones.get(row.property_id or "", "UTC")
    zone: tzinfo
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        zone = UTC
    return starts_at.astimezone(zone).date()


def ruleset_id_for(assignment_id: str) -> str:
    """Synthetic ``assignment:<id>`` ruleset id (until §06 storage lands)."""
    return f"assignment:{assignment_id}"


# ---------------------------------------------------------------------------
# Repository reads
# ---------------------------------------------------------------------------


def list_workspace_properties(
    session: Session,
    ctx: WorkspaceContext,
) -> list[Property]:
    """Return live properties belonging to the workspace, ordered by display name."""
    stmt = (
        select(Property)
        .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
        .where(
            PropertyWorkspace.workspace_id == ctx.workspace_id,
            PropertyWorkspace.status == "active",
            Property.deleted_at.is_(None),
        )
        .order_by(
            Property.name.asc().nulls_last(),
            Property.address.asc(),
            Property.id.asc(),
        )
    )
    return list(session.scalars(stmt).all())


def assignment_rows_for_window(
    session: Session,
    ctx: WorkspaceContext,
    *,
    visible_property_ids: set[str],
    user_filter: str | None = None,
    role_filter: str | None = None,
    narrow_to_user_id: str | None = None,
) -> list[AssignmentJoinRow]:
    """Walk ``property_work_role_assignment`` joined with role + user.

    ``narrow_to_user_id`` short-circuits the worker / self-view to a
    single user without piggy-backing on ``ctx.actor_grant_role`` —
    callers that already know the user (``/me/schedule``) pass it
    explicitly. ``user_filter`` and ``role_filter`` mirror the
    ``/scheduler/calendar`` query knobs.
    """
    if not visible_property_ids:
        return []

    stmt = (
        select(PropertyWorkRoleAssignment, UserWorkRole, WorkRole, User)
        .join(
            UserWorkRole,
            UserWorkRole.id == PropertyWorkRoleAssignment.user_work_role_id,
        )
        .join(WorkRole, WorkRole.id == UserWorkRole.work_role_id)
        .join(User, User.id == UserWorkRole.user_id)
        .where(
            PropertyWorkRoleAssignment.workspace_id == ctx.workspace_id,
            PropertyWorkRoleAssignment.deleted_at.is_(None),
            PropertyWorkRoleAssignment.property_id.in_(visible_property_ids),
            UserWorkRole.workspace_id == ctx.workspace_id,
            UserWorkRole.deleted_at.is_(None),
            WorkRole.workspace_id == ctx.workspace_id,
            WorkRole.deleted_at.is_(None),
        )
        .order_by(
            PropertyWorkRoleAssignment.property_id.asc(),
            User.display_name.asc(),
            WorkRole.name.asc(),
            PropertyWorkRoleAssignment.id.asc(),
        )
    )
    if narrow_to_user_id is not None:
        stmt = stmt.where(UserWorkRole.user_id == narrow_to_user_id)
    if user_filter is not None:
        stmt = stmt.where(UserWorkRole.user_id == user_filter)
    if role_filter is not None:
        stmt = stmt.where(
            or_(
                WorkRole.id == role_filter,
                WorkRole.key == role_filter,
                WorkRole.name == role_filter,
            )
        )
    return [
        (assignment, user_work_role, role, user)
        for assignment, user_work_role, role, user in session.execute(stmt).all()
    ]


def task_rows_for_window(
    session: Session,
    ctx: WorkspaceContext,
    *,
    from_date: date,
    to_date: date,
    visible_property_ids: set[str],
    property_timezones: dict[str, str],
    user_filter: str | None = None,
    narrow_to_user_id: str | None = None,
    exclude_cancelled: bool = False,
) -> list[Occurrence]:
    """Return assigned occurrences whose property-local date is in window.

    ``exclude_cancelled`` drops ``state='cancelled'`` rows — used by
    ``/me/schedule`` (the worker self-view), where a cancelled task
    is no longer actionable. The manager ``/scheduler/calendar``
    leaves it off so cancelled rows still surface (managers reviewing
    the rota need to see what got cancelled).
    """
    if not visible_property_ids:
        return []
    window_start = datetime.combine(from_date - timedelta(days=1), time.min, tzinfo=UTC)
    window_end = datetime.combine(to_date + timedelta(days=1), time.max, tzinfo=UTC)
    stmt = (
        select(Occurrence)
        .where(
            Occurrence.workspace_id == ctx.workspace_id,
            Occurrence.property_id.in_(visible_property_ids),
            Occurrence.property_id.is_not(None),
            Occurrence.assignee_user_id.is_not(None),
            Occurrence.starts_at >= window_start,
            Occurrence.starts_at <= window_end,
        )
        .order_by(Occurrence.starts_at.asc(), Occurrence.id.asc())
    )
    if narrow_to_user_id is not None:
        stmt = stmt.where(Occurrence.assignee_user_id == narrow_to_user_id)
    if user_filter is not None:
        stmt = stmt.where(Occurrence.assignee_user_id == user_filter)
    if exclude_cancelled:
        stmt = stmt.where(Occurrence.state != "cancelled")
    return [
        row
        for row in session.scalars(stmt).all()
        if from_date
        <= local_date_for_task(row, property_timezones=property_timezones)
        <= to_date
    ]


def weekly_rows_for_users(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_ids: set[str],
) -> dict[str, list[UserWeeklyAvailability]]:
    """Group ``user_weekly_availability`` rows by ``user_id``."""
    if not user_ids:
        return {}
    stmt = (
        select(UserWeeklyAvailability)
        .where(
            UserWeeklyAvailability.workspace_id == ctx.workspace_id,
            UserWeeklyAvailability.user_id.in_(user_ids),
        )
        .order_by(
            UserWeeklyAvailability.user_id.asc(),
            UserWeeklyAvailability.weekday.asc(),
        )
    )
    out: dict[str, list[UserWeeklyAvailability]] = defaultdict(list)
    for row in session.scalars(stmt).all():
        out[row.user_id].append(row)
    return dict(out)


def users_by_id(session: Session, *, user_ids: set[str]) -> dict[str, User]:
    """Load :class:`User` rows for a set of ids, keyed by id."""
    if not user_ids:
        return {}
    stmt = select(User).where(User.id.in_(user_ids)).order_by(User.display_name.asc())
    return {row.id: row for row in session.scalars(stmt).all()}


def role_names_by_user(assignment_rows: list[AssignmentJoinRow]) -> dict[str, str]:
    """First role name per user — used for the user list's ``work_role`` column."""
    names: dict[str, str] = {}
    for _assignment, user_work_role, role, _user in assignment_rows:
        names.setdefault(user_work_role.user_id, role.name)
    return names


# ---------------------------------------------------------------------------
# Rota block synthesis
# ---------------------------------------------------------------------------


def build_rota_blocks(
    *,
    workspace_id: str,
    assignment_rows: list[AssignmentJoinRow],
    weekly_by_user: dict[str, list[UserWeeklyAvailability]],
    public_user_ids: dict[str, str] | None = None,
    public_assignment_id: dict[str, str] | None = None,
    expose_work_role_id: bool = True,
) -> tuple[
    list[ScheduleRulesetResponse],
    list[ScheduleRulesetSlotResponse],
    list[ScheduleAssignmentResponse],
]:
    """Synthesise rulesets / slots / assignment rows from join + weekly bag.

    ``public_user_ids`` and ``public_assignment_id`` opt into the
    client-portal anonymisation (``/scheduler/calendar`` for client
    actors). ``expose_work_role_id`` is paired with the same toggle.
    Default args produce the unanonymised shape ``/me/schedule`` and
    the manager / worker ``/scheduler/calendar`` views consume.
    """
    rulesets_by_id: dict[str, ScheduleRulesetResponse] = {}
    slots: list[ScheduleRulesetSlotResponse] = []
    assignments: list[ScheduleAssignmentResponse] = []
    for assignment, user_work_role, role, _user in assignment_rows:
        public_user_id = (
            public_user_ids[user_work_role.user_id]
            if public_user_ids is not None
            else user_work_role.user_id
        )
        assignment_id = (
            public_assignment_id[assignment.id]
            if public_assignment_id is not None
            else assignment.id
        )
        ruleset_id = ruleset_id_for(assignment_id)
        rulesets_by_id.setdefault(
            ruleset_id,
            ScheduleRulesetResponse(
                id=ruleset_id,
                workspace_id=workspace_id,
                name=f"{role.name} rota",
            ),
        )
        assignments.append(
            ScheduleAssignmentResponse(
                id=assignment_id,
                user_id=public_user_id,
                work_role_id=(
                    user_work_role.work_role_id if expose_work_role_id else None
                ),
                property_id=assignment.property_id,
                schedule_ruleset_id=ruleset_id,
            )
        )
        for weekly in weekly_by_user.get(user_work_role.user_id, []):
            if weekly.starts_local is None or weekly.ends_local is None:
                continue
            slots.append(
                ScheduleRulesetSlotResponse(
                    id=f"{assignment.id}:{weekly.weekday}",
                    schedule_ruleset_id=ruleset_id,
                    weekday=weekly.weekday,
                    starts_local=time_text(weekly.starts_local),
                    ends_local=time_text(weekly.ends_local),
                )
            )
    return (
        sorted(rulesets_by_id.values(), key=lambda row: row.id),
        slots,
        assignments,
    )
