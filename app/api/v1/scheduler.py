"""Workspace scheduler calendar feed.

The landed schema does not yet have ``schedule_ruleset`` /
``schedule_ruleset_slot`` tables. The feed still returns the stable
frontend calendar shape by resolving live property-role assignments
against each user's weekly availability rows. Persisted rulesets can
replace the synthetic ids here once that storage lands.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.availability.models import UserWeeklyAvailability
from app.adapters.db.identity.models import User
from app.adapters.db.places.models import (
    Property,
    PropertyWorkRoleAssignment,
    PropertyWorkspace,
)
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import UserWorkRole, WorkRole
from app.api.deps import current_workspace_context, db_session
from app.tenancy import WorkspaceContext

__all__ = ["build_scheduler_router", "router"]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


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


class SchedulerCalendarResponse(BaseModel):
    window: SchedulerWindowResponse
    rulesets: list[ScheduleRulesetResponse]
    slots: list[ScheduleRulesetSlotResponse]
    assignments: list[ScheduleAssignmentResponse]
    tasks: list[SchedulerTaskResponse]
    stay_bundles: list[dict[str, object]] = Field(default_factory=list)
    users: list[SchedulerUserResponse]
    properties: list[SchedulerPropertyResponse]


_FromQuery = Annotated[date, Query(alias="from")]
_ToQuery = Annotated[date, Query(alias="to")]
_UserFilterQuery = Annotated[str | None, Query(alias="user")]
_PropertyFilterQuery = Annotated[str | None, Query(alias="property")]
_RoleFilterQuery = Annotated[str | None, Query(alias="role")]


def _http_for_window() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": "invalid_field",
            "field": "to",
            "message": "to must be on or after from",
        },
    )


def _http_for_forbidden() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "error": "permission_denied",
            "action_key": "scheduler.calendar",
        },
    )


def _time_text(value: time) -> str:
    return value.strftime("%H:%M")


def _first_name(display_name: str) -> str:
    stripped = display_name.strip()
    if not stripped:
        return ""
    return stripped.split()[0]


def _property_name(row: Property) -> str:
    return row.name if row.name is not None else row.address


def _scheduled_start(row: Occurrence) -> str:
    if row.scheduled_for_local:
        return row.scheduled_for_local
    return row.starts_at.isoformat()


def _estimated_minutes(row: Occurrence) -> int:
    if row.duration_minutes is not None:
        return row.duration_minutes
    delta = row.ends_at - row.starts_at
    return max(1, int(delta.total_seconds() // 60))


def _local_date_for_task(
    row: Occurrence,
    *,
    property_timezones: dict[str, str],
) -> date:
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


def _ruleset_id_for(assignment_id: str) -> str:
    return f"assignment:{assignment_id}"


def _list_workspace_properties(
    session: Session,
    ctx: WorkspaceContext,
) -> list[Property]:
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


def _client_visible_property_ids(
    session: Session,
    ctx: WorkspaceContext,
    *,
    properties: list[Property],
) -> set[str]:
    grants = session.execute(
        select(RoleGrant.scope_property_id, RoleGrant.binding_org_id).where(
            RoleGrant.workspace_id == ctx.workspace_id,
            RoleGrant.user_id == ctx.actor_id,
            RoleGrant.scope_kind == "workspace",
            RoleGrant.grant_role == "client",
            # cd-x1xh: live grants only — a soft-retired client
            # grant must not widen the portal's property visibility.
            RoleGrant.revoked_at.is_(None),
        )
    ).all()
    property_ids = {pid for pid, _org_id in grants if pid is not None}
    org_ids = {org_id for pid, org_id in grants if pid is None and org_id is not None}
    return {
        prop.id
        for prop in properties
        if prop.id in property_ids
        or (prop.client_org_id is not None and prop.client_org_id in org_ids)
    }


def _assignment_rows(
    session: Session,
    ctx: WorkspaceContext,
    *,
    visible_property_ids: set[str],
    user_filter: str | None,
    role_filter: str | None,
) -> list[tuple[PropertyWorkRoleAssignment, UserWorkRole, WorkRole, User]]:
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
    if ctx.actor_grant_role == "worker":
        stmt = stmt.where(UserWorkRole.user_id == ctx.actor_id)
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


def _task_rows(
    session: Session,
    ctx: WorkspaceContext,
    *,
    from_date: date,
    to_date: date,
    visible_property_ids: set[str],
    property_timezones: dict[str, str],
    user_filter: str | None,
) -> list[Occurrence]:
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
    if ctx.actor_grant_role == "worker":
        stmt = stmt.where(Occurrence.assignee_user_id == ctx.actor_id)
    if user_filter is not None:
        stmt = stmt.where(Occurrence.assignee_user_id == user_filter)
    return [
        row
        for row in session.scalars(stmt).all()
        if from_date
        <= _local_date_for_task(row, property_timezones=property_timezones)
        <= to_date
    ]


def _weekly_rows(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_ids: set[str],
) -> dict[str, list[UserWeeklyAvailability]]:
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


def _users_by_id(session: Session, *, user_ids: set[str]) -> dict[str, User]:
    if not user_ids:
        return {}
    stmt = select(User).where(User.id.in_(user_ids)).order_by(User.display_name.asc())
    return {row.id: row for row in session.scalars(stmt).all()}


def _role_names_by_user(
    assignment_rows: list[
        tuple[PropertyWorkRoleAssignment, UserWorkRole, WorkRole, User]
    ],
) -> dict[str, str]:
    names: dict[str, str] = {}
    for _assignment, user_work_role, role, _user in assignment_rows:
        names.setdefault(user_work_role.user_id, role.name)
    return names


def _public_user_ids(*, ctx: WorkspaceContext, user_ids: set[str]) -> dict[str, str]:
    if ctx.actor_grant_role != "client":
        return {user_id: user_id for user_id in user_ids}
    return {
        user_id: f"staff:{index}"
        for index, user_id in enumerate(sorted(user_ids), start=1)
    }


def _build_payload(
    session: Session,
    ctx: WorkspaceContext,
    *,
    from_date: date,
    to_date: date,
    user_filter: str | None,
    property_filter: str | None,
    role_filter: str | None,
) -> SchedulerCalendarResponse:
    workspace_properties = _list_workspace_properties(session, ctx)
    properties_by_id = {prop.id: prop for prop in workspace_properties}
    property_timezones = {prop.id: prop.timezone for prop in workspace_properties}
    all_property_ids = {prop.id for prop in workspace_properties}
    if ctx.actor_grant_role == "client":
        visible_property_ids = _client_visible_property_ids(
            session, ctx, properties=workspace_properties
        )
    elif ctx.actor_grant_role in {"worker", "manager"} or ctx.actor_was_owner_member:
        visible_property_ids = set(all_property_ids)
    else:
        raise _http_for_forbidden()
    if property_filter is not None:
        visible_property_ids &= {property_filter}

    assignments_source = _assignment_rows(
        session,
        ctx,
        visible_property_ids=visible_property_ids,
        user_filter=user_filter,
        role_filter=role_filter,
    )
    role_filtered_user_ids = {
        user_work_role.user_id
        for _assignment, user_work_role, _role, _user in assignments_source
    }
    tasks_source = _task_rows(
        session,
        ctx,
        from_date=from_date,
        to_date=to_date,
        visible_property_ids=visible_property_ids,
        property_timezones=property_timezones,
        user_filter=user_filter,
    )
    if role_filter is not None:
        tasks_source = [
            task
            for task in tasks_source
            if task.assignee_user_id in role_filtered_user_ids
            or task.expected_role_id == role_filter
        ]

    if ctx.actor_grant_role == "worker":
        visible_property_ids = {
            row.property_id for row, _uwr, _role, _user in assignments_source
        } | {task.property_id for task in tasks_source if task.property_id is not None}
    if user_filter is not None or role_filter is not None:
        visible_property_ids = {
            row.property_id for row, _uwr, _role, _user in assignments_source
        } | {task.property_id for task in tasks_source if task.property_id is not None}

    properties = [
        SchedulerPropertyResponse(
            id=prop.id,
            name=_property_name(prop),
            timezone=prop.timezone,
        )
        for prop in workspace_properties
        if prop.id in visible_property_ids
    ]

    user_ids = {
        user_work_role.user_id
        for _assignment, user_work_role, _role, _user in assignments_source
    } | {
        task.assignee_user_id
        for task in tasks_source
        if task.assignee_user_id is not None
    }
    weekly_by_user = _weekly_rows(session, ctx, user_ids=user_ids)
    public_user_ids = _public_user_ids(ctx=ctx, user_ids=user_ids)

    rulesets_by_id: dict[str, ScheduleRulesetResponse] = {}
    slots: list[ScheduleRulesetSlotResponse] = []
    assignments: list[ScheduleAssignmentResponse] = []
    for assignment, user_work_role, role, _user in assignments_source:
        public_user_id = public_user_ids[user_work_role.user_id]
        assignment_id = (
            f"assignment:{public_user_id}:{assignment.property_id}"
            if ctx.actor_grant_role == "client"
            else assignment.id
        )
        ruleset_id = _ruleset_id_for(assignment_id)
        rulesets_by_id.setdefault(
            ruleset_id,
            ScheduleRulesetResponse(
                id=ruleset_id,
                workspace_id=ctx.workspace_id,
                name=f"{role.name} rota",
            ),
        )
        assignments.append(
            ScheduleAssignmentResponse(
                id=assignment_id,
                user_id=public_user_id,
                work_role_id=(
                    None
                    if ctx.actor_grant_role == "client"
                    else user_work_role.work_role_id
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
                    starts_local=_time_text(weekly.starts_local),
                    ends_local=_time_text(weekly.ends_local),
                )
            )

    tasks = [
        SchedulerTaskResponse(
            id=task.id,
            title=task.title or "Task",
            property_id=task.property_id,
            user_id=public_user_ids[task.assignee_user_id],
            scheduled_start=_scheduled_start(task),
            estimated_minutes=_estimated_minutes(task),
            priority=task.priority,
            status=task.state,
        )
        for task in tasks_source
        if task.property_id is not None and task.assignee_user_id is not None
    ]

    users = _users_by_id(session, user_ids=user_ids)
    role_names = _role_names_by_user(assignments_source)
    user_responses = [
        SchedulerUserResponse(
            id=public_user_ids[user.id],
            first_name=_first_name(user.display_name),
            display_name=(
                None if ctx.actor_grant_role == "client" else user.display_name
            ),
            work_role=role_names.get(user.id),
        )
        for user in sorted(users.values(), key=lambda u: (u.display_name, u.id))
    ]

    return SchedulerCalendarResponse(
        window=SchedulerWindowResponse(from_date=from_date, to_date=to_date),
        rulesets=sorted(rulesets_by_id.values(), key=lambda row: row.id),
        slots=slots,
        assignments=assignments,
        tasks=tasks,
        stay_bundles=[],
        users=user_responses,
        properties=sorted(
            properties,
            key=lambda row: (_property_name(properties_by_id[row.id]), row.id),
        ),
    )


def build_scheduler_router() -> APIRouter:
    api = APIRouter(prefix="/scheduler", tags=["tasks", "scheduler"])

    @api.get(
        "/calendar",
        response_model=SchedulerCalendarResponse,
        operation_id="scheduler.calendar.get",
        summary="Who is booked where calendar feed",
    )
    def calendar(
        ctx: _Ctx,
        session: _Db,
        from_: _FromQuery,
        to: _ToQuery,
        user: _UserFilterQuery = None,
        property_: _PropertyFilterQuery = None,
        role: _RoleFilterQuery = None,
    ) -> SchedulerCalendarResponse:
        if to < from_:
            raise _http_for_window()
        return _build_payload(
            session,
            ctx,
            from_date=from_,
            to_date=to,
            user_filter=user,
            property_filter=property_,
            role_filter=role,
        )

    return api


router = build_scheduler_router()
