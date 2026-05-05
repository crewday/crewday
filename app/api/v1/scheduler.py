"""Workspace scheduler calendar feed.

The landed schema does not yet have ``schedule_ruleset`` /
``schedule_ruleset_slot`` tables. The feed still returns the stable
frontend calendar shape by resolving live property-role assignments
against each user's weekly availability rows. Persisted rulesets can
replace the synthetic ids here once that storage lands.

The rota / slot / assignment / property / task synthesis lives in
:mod:`app.api.v1._scheduler_resolver` so that ``/me/schedule`` shares
exactly one source of truth for the rota shape (otherwise the worker
self-feed and the manager calendar would drift on every column edit).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.places.models import Property
from app.adapters.db.tasks.models import Occurrence
from app.api.deps import current_workspace_context, db_session
from app.api.v1._problem_json import IDENTITY_PROBLEM_RESPONSES
from app.api.v1._scheduler_resolver import (
    AssignmentJoinRow,
    ScheduleAssignmentResponse,
    SchedulerPropertyResponse,
    SchedulerTaskResponse,
    ScheduleRulesetResponse,
    ScheduleRulesetSlotResponse,
    SchedulerUserResponse,
    SchedulerWindowResponse,
    assignment_rows_for_window,
    build_rota_blocks,
    estimated_minutes,
    list_workspace_properties,
    property_name,
    role_names_by_user,
    scheduled_start_text,
    task_rows_for_window,
    users_by_id,
    weekly_rows_for_users,
)
from app.domain.errors import Forbidden
from app.domain.errors import Validation as DomainValidation
from app.tenancy import WorkspaceContext

__all__ = ["build_scheduler_router", "router"]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


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


@dataclass(frozen=True)
class _SchedulerWindow:
    from_date: date
    to_date: date


@dataclass(frozen=True)
class _SchedulerFilters:
    user_id: str | None
    property_id: str | None
    role_id: str | None


@dataclass(frozen=True)
class _SchedulerSources:
    assignments: list[AssignmentJoinRow]
    tasks: list[Occurrence]
    visible_property_ids: set[str]


def _invalid_window_error() -> DomainValidation:
    message = "to must be on or after from"
    return DomainValidation(
        message,
        extra={
            "error": "invalid_field",
            "field": "to",
            "message": message,
        },
    )


def _forbidden_error() -> Forbidden:
    return Forbidden(
        extra={"error": "permission_denied", "action_key": "scheduler.calendar"}
    )


def _first_name(display_name: str) -> str:
    stripped = display_name.strip()
    if not stripped:
        return ""
    return stripped.split()[0]


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


def _visible_property_ids(
    session: Session,
    ctx: WorkspaceContext,
    *,
    properties: list[Property],
    property_filter: str | None,
) -> set[str]:
    all_property_ids = {prop.id for prop in properties}
    if ctx.actor_grant_role == "client":
        visible_property_ids = _client_visible_property_ids(
            session, ctx, properties=properties
        )
    elif ctx.actor_grant_role in {"worker", "manager"} or ctx.actor_was_owner_member:
        visible_property_ids = set(all_property_ids)
    else:
        raise _forbidden_error()
    if property_filter is not None:
        visible_property_ids &= {property_filter}
    return visible_property_ids


def _public_user_ids(*, ctx: WorkspaceContext, user_ids: set[str]) -> dict[str, str]:
    if ctx.actor_grant_role != "client":
        return {user_id: user_id for user_id in user_ids}
    return {
        user_id: f"staff:{index}"
        for index, user_id in enumerate(sorted(user_ids), start=1)
    }


def _load_scheduler_sources(
    session: Session,
    ctx: WorkspaceContext,
    *,
    window: _SchedulerWindow,
    filters: _SchedulerFilters,
    visible_property_ids: set[str],
    property_timezones: dict[str, str],
) -> _SchedulerSources:
    # code-health: ignore[params] Fans out scheduler filters.
    narrow_to_user_id = ctx.actor_id if ctx.actor_grant_role == "worker" else None
    assignments_source = assignment_rows_for_window(
        session,
        ctx,
        visible_property_ids=visible_property_ids,
        user_filter=filters.user_id,
        role_filter=filters.role_id,
        narrow_to_user_id=narrow_to_user_id,
    )
    role_filtered_user_ids = {
        user_work_role.user_id
        for _assignment, user_work_role, _role, _user in assignments_source
    }
    tasks_source = task_rows_for_window(
        session,
        ctx,
        from_date=window.from_date,
        to_date=window.to_date,
        visible_property_ids=visible_property_ids,
        property_timezones=property_timezones,
        user_filter=filters.user_id,
        narrow_to_user_id=narrow_to_user_id,
    )
    if filters.role_id is not None:
        tasks_source = [
            task
            for task in tasks_source
            if task.assignee_user_id in role_filtered_user_ids
            or task.expected_role_id == filters.role_id
        ]

    if (
        ctx.actor_grant_role == "worker"
        or filters.user_id is not None
        or filters.role_id is not None
    ):
        visible_property_ids = {
            row.property_id for row, _uwr, _role, _user in assignments_source
        } | {task.property_id for task in tasks_source if task.property_id is not None}

    return _SchedulerSources(
        assignments=assignments_source,
        tasks=tasks_source,
        visible_property_ids=visible_property_ids,
    )


def _property_responses(
    *,
    properties: list[Property],
    visible_property_ids: set[str],
) -> list[SchedulerPropertyResponse]:
    return [
        SchedulerPropertyResponse(
            id=prop.id,
            name=property_name(prop),
            timezone=prop.timezone,
        )
        for prop in properties
        if prop.id in visible_property_ids
    ]


def _task_responses(
    *,
    tasks_source: list[Occurrence],
    public_user_ids: dict[str, str],
) -> list[SchedulerTaskResponse]:
    return [
        SchedulerTaskResponse(
            id=task.id,
            title=task.title or "Task",
            property_id=task.property_id,
            user_id=public_user_ids[task.assignee_user_id],
            scheduled_start=scheduled_start_text(task),
            estimated_minutes=estimated_minutes(task),
            priority=task.priority,
            status=task.state,
        )
        for task in tasks_source
        if task.property_id is not None and task.assignee_user_id is not None
    ]


def _user_responses(
    session: Session,
    *,
    user_ids: set[str],
    public_user_ids: dict[str, str],
    assignments_source: list[AssignmentJoinRow],
    expose_display_name: bool,
) -> list[SchedulerUserResponse]:
    users = users_by_id(session, user_ids=user_ids)
    role_names = role_names_by_user(assignments_source)
    return [
        SchedulerUserResponse(
            id=public_user_ids[user.id],
            first_name=_first_name(user.display_name),
            display_name=user.display_name if expose_display_name else None,
            work_role=role_names.get(user.id),
        )
        for user in sorted(users.values(), key=lambda u: (u.display_name, u.id))
    ]


def _build_payload(
    session: Session,
    ctx: WorkspaceContext,
    *,
    window: _SchedulerWindow,
    filters: _SchedulerFilters,
) -> SchedulerCalendarResponse:
    # code-health: ignore[nloc] Router composition stays explicit.
    workspace_properties = list_workspace_properties(session, ctx)
    properties_by_id = {prop.id: prop for prop in workspace_properties}
    property_timezones = {prop.id: prop.timezone for prop in workspace_properties}
    visible_property_ids = _visible_property_ids(
        session,
        ctx,
        properties=workspace_properties,
        property_filter=filters.property_id,
    )
    sources = _load_scheduler_sources(
        session,
        ctx,
        window=window,
        filters=filters,
        visible_property_ids=visible_property_ids,
        property_timezones=property_timezones,
    )

    properties = _property_responses(
        properties=workspace_properties,
        visible_property_ids=sources.visible_property_ids,
    )

    user_ids = {
        user_work_role.user_id
        for _assignment, user_work_role, _role, _user in sources.assignments
    } | {
        task.assignee_user_id
        for task in sources.tasks
        if task.assignee_user_id is not None
    }
    weekly_by_user = weekly_rows_for_users(session, ctx, user_ids=user_ids)
    public_user_ids = _public_user_ids(ctx=ctx, user_ids=user_ids)
    public_assignment_id: dict[str, str] | None
    if ctx.actor_grant_role == "client":
        public_assignment_id = {
            assignment.id: (
                f"assignment:{public_user_ids[user_work_role.user_id]}"
                f":{assignment.property_id}"
            )
            for assignment, user_work_role, _role, _user in sources.assignments
        }
    else:
        public_assignment_id = None

    rulesets, slots, assignments = build_rota_blocks(
        workspace_id=ctx.workspace_id,
        assignment_rows=sources.assignments,
        weekly_by_user=weekly_by_user,
        public_user_ids=(public_user_ids if ctx.actor_grant_role == "client" else None),
        public_assignment_id=public_assignment_id,
        expose_work_role_id=ctx.actor_grant_role != "client",
    )

    return SchedulerCalendarResponse(
        window=SchedulerWindowResponse(
            from_date=window.from_date,
            to_date=window.to_date,
        ),
        rulesets=rulesets,
        slots=slots,
        assignments=assignments,
        tasks=_task_responses(
            tasks_source=sources.tasks,
            public_user_ids=public_user_ids,
        ),
        stay_bundles=[],
        users=_user_responses(
            session,
            user_ids=user_ids,
            public_user_ids=public_user_ids,
            assignments_source=sources.assignments,
            expose_display_name=ctx.actor_grant_role != "client",
        ),
        properties=sorted(
            properties,
            key=lambda row: (property_name(properties_by_id[row.id]), row.id),
        ),
    )


def build_scheduler_router() -> APIRouter:
    api = APIRouter(
        prefix="/scheduler",
        tags=["tasks", "scheduler"],
        responses=IDENTITY_PROBLEM_RESPONSES,
    )

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
        # code-health: ignore[params] Preserves OpenAPI query aliases.
        if to < from_:
            raise _invalid_window_error()
        return _build_payload(
            session,
            ctx,
            window=_SchedulerWindow(from_date=from_, to_date=to),
            filters=_SchedulerFilters(
                user_id=user,
                property_id=property_,
                role_id=role,
            ),
        )

    return api


router = build_scheduler_router()
