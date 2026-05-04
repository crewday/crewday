"""Manager dashboard aggregate API."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.adapters.db.issues.models import IssueReport
from app.adapters.db.llm.models import ApprovalRequest
from app.adapters.db.places.models import Area
from app.adapters.db.stays.models import Reservation
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.time.models import Leave, Shift
from app.api.deps import current_workspace_context, db_session
from app.api.v1.employees import (
    EmployeeResponse,
    _list_workspace_users,
    _load_active_engagements,
    _load_property_ids_by_user,
    _load_role_keys_by_user,
    _load_users,
    _project_employee,
)
from app.api.v1.places import (
    PropertyResponse,
    _list_workspace_properties,
    _load_areas_by_property,
    _project_property,
)
from app.authz.dep import Permission
from app.domain.errors import Conflict, NotFound
from app.services.leave import (
    LeaveDecision,
    LeaveDecisionRequest,
    LeaveNotFound,
    LeavePermissionDenied,
    LeaveTransitionForbidden,
    LeaveView,
    decide_leave,
)
from app.tenancy import WorkspaceContext

__all__ = ["build_dashboard_router"]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


class DashboardTask(BaseModel):
    id: str
    title: str
    property_id: str
    area: str
    assignee_id: str
    scheduled_start: datetime
    estimated_minutes: int
    priority: Literal["low", "normal", "high", "urgent"]
    status: Literal[
        "scheduled",
        "pending",
        "in_progress",
        "completed",
        "skipped",
        "cancelled",
        "overdue",
    ]
    checklist: list[dict[str, object]]
    photo_evidence: Literal["disabled", "optional", "required"]
    evidence_policy: Literal["inherit", "require", "optional", "forbid"]
    instructions_ids: list[str]
    template_id: str | None
    schedule_id: str | None
    turnover_bundle_id: str | None
    asset_id: str | None
    settings_override: dict[str, object]
    assigned_user_id: str
    workspace_id: str
    created_by: str
    is_personal: bool


class DashboardTaskBuckets(BaseModel):
    completed: list[DashboardTask]
    in_progress: list[DashboardTask]
    pending: list[DashboardTask]


class DashboardApproval(BaseModel):
    id: str
    agent: str
    action: str
    target: str
    reason: str
    requested_at: datetime
    risk: Literal["low", "medium", "high"]
    diff: list[str]
    gate_source: str
    gate_destination: Literal["desk", "inline_chat"]
    inline_channel: str
    card_summary: str
    card_fields: list[tuple[str, str]]
    for_user_id: str | None
    resolved_user_mode: str | None


class DashboardLeave(BaseModel):
    id: str
    employee_id: str
    starts_on: date
    ends_on: date
    category: Literal["vacation", "sick", "personal", "bereavement", "other"]
    note: str
    approved_at: datetime | None


class DashboardIssue(BaseModel):
    id: str
    reported_by: str
    property_id: str
    area: str
    severity: Literal["low", "normal", "high", "urgent"]
    category: Literal["damage", "broken", "supplies", "safety", "other"]
    title: str
    body: str
    reported_at: datetime
    status: Literal["open", "in_progress", "resolved", "wont_fix"]


class DashboardStay(BaseModel):
    id: str
    property_id: str
    guest_name: str
    source: Literal["manual", "airbnb", "vrbo", "booking", "google_calendar", "ical"]
    check_in: datetime
    check_out: datetime
    guests: int
    status: Literal["tentative", "confirmed", "in_house", "checked_out", "cancelled"]


class DashboardPayload(BaseModel):
    on_booking: list[EmployeeResponse]
    by_status: DashboardTaskBuckets
    pending_approvals: list[DashboardApproval]
    pending_expenses: list[dict[str, object]]
    pending_leaves: list[DashboardLeave]
    open_issues: list[DashboardIssue]
    stays_today: list[DashboardStay]
    properties: list[PropertyResponse]
    employees: list[EmployeeResponse]


class LeavesInboxPayload(BaseModel):
    pending: list[DashboardLeave]
    approved: list[DashboardLeave]


def build_dashboard_router() -> APIRouter:
    api = APIRouter(tags=["dashboard"])
    dashboard_gate = Depends(Permission("employees.read", scope_kind="workspace"))
    leave_view_gate = Depends(Permission("leaves.view_others", scope_kind="workspace"))
    leave_edit_gate = Depends(Permission("leaves.edit_others", scope_kind="workspace"))

    @api.get(
        "/dashboard",
        response_model=DashboardPayload,
        operation_id="dashboard.get",
        summary="Read the manager dashboard aggregate",
        dependencies=[dashboard_gate],
    )
    def dashboard(ctx: _Ctx, session: _Db) -> DashboardPayload:
        employees = _employees(session, ctx)
        properties = _properties(session, ctx)
        now = datetime.now(tz=UTC)
        tasks = _tasks_today(session, ctx, now)
        return DashboardPayload(
            on_booking=_on_booking(session, ctx, employees),
            by_status=DashboardTaskBuckets(
                completed=[task for task in tasks if task.status == "completed"],
                in_progress=[task for task in tasks if task.status == "in_progress"],
                pending=[
                    task
                    for task in tasks
                    if task.status not in {"completed", "in_progress"}
                ],
            ),
            pending_approvals=_pending_approvals(session, ctx),
            pending_expenses=[],
            pending_leaves=_pending_leaves(session, ctx),
            open_issues=_open_issues(session, ctx),
            stays_today=_stays_today(session, ctx, now),
            properties=properties,
            employees=employees,
        )

    @api.get(
        "/leaves",
        response_model=LeavesInboxPayload,
        operation_id="dashboard.leaves.list",
        summary="Read the manager leave inbox aggregate",
        dependencies=[leave_view_gate],
    )
    def leaves(ctx: _Ctx, session: _Db) -> LeavesInboxPayload:
        return _leaves_inbox(session, ctx, now=datetime.now(tz=UTC))

    @api.post(
        "/leaves/{leave_id}/approve",
        response_model=DashboardLeave,
        operation_id="dashboard.leaves.approve",
        summary="Approve a pending leave from manager dashboard",
        dependencies=[leave_edit_gate],
    )
    def approve_leave(leave_id: str, ctx: _Ctx, session: _Db) -> DashboardLeave:
        return _decide_leave(session, ctx, leave_id=leave_id, decision="approved")

    @api.post(
        "/leaves/{leave_id}/reject",
        response_model=DashboardLeave,
        operation_id="dashboard.leaves.reject",
        summary="Reject a pending leave from manager dashboard",
        dependencies=[leave_edit_gate],
    )
    def reject_leave(leave_id: str, ctx: _Ctx, session: _Db) -> DashboardLeave:
        return _decide_leave(session, ctx, leave_id=leave_id, decision="rejected")

    return api


def _employees(session: Session, ctx: WorkspaceContext) -> list[EmployeeResponse]:
    user_ids = _list_workspace_users(session, ctx)
    users = _load_users(session, user_ids=user_ids)
    engagements = _load_active_engagements(session, ctx, user_ids=user_ids)
    role_keys = _load_role_keys_by_user(session, ctx, user_ids=user_ids)
    property_ids = _load_property_ids_by_user(session, ctx, user_ids=user_ids)
    out: list[EmployeeResponse] = []
    for user_id in user_ids:
        user = users.get(user_id)
        if user is None:
            continue
        out.append(
            _project_employee(
                user,
                workspace_id=ctx.workspace_id,
                engagement=engagements.get(user_id),
                role_keys=role_keys.get(user_id, []),
                property_ids=property_ids.get(user_id, []),
            )
        )
    return out


def _properties(session: Session, ctx: WorkspaceContext) -> list[PropertyResponse]:
    rows = _list_workspace_properties(session, ctx)
    areas = _load_areas_by_property(session, property_ids=[row.id for row in rows])
    return [
        _project_property(
            row,
            areas=areas.get(row.id, []),
            mask_governance=False,
        )
        for row in rows
    ]


def _on_booking(
    session: Session,
    ctx: WorkspaceContext,
    employees: list[EmployeeResponse],
) -> list[EmployeeResponse]:
    open_user_ids = set(
        session.scalars(
            select(Shift.user_id).where(
                Shift.workspace_id == ctx.workspace_id,
                Shift.ends_at.is_(None),
            )
        ).all()
    )
    return [employee for employee in employees if employee.id in open_user_ids]


def _tasks_today(
    session: Session,
    ctx: WorkspaceContext,
    now: datetime,
) -> list[DashboardTask]:
    start = datetime.combine(now.date(), time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    rows = session.scalars(
        select(Occurrence)
        .where(
            Occurrence.workspace_id == ctx.workspace_id,
            Occurrence.starts_at >= start,
            Occurrence.starts_at < end,
        )
        .order_by(Occurrence.starts_at.asc(), Occurrence.id.asc())
    ).all()
    area_labels = _area_labels(session, [row.area_id for row in rows if row.area_id])
    return [_task_from_row(row, area_labels=area_labels) for row in rows]


def _task_from_row(
    row: Occurrence,
    *,
    area_labels: dict[str, str],
) -> DashboardTask:
    return DashboardTask(
        id=row.id,
        title=row.title or "Untitled task",
        property_id=row.property_id or "",
        area=area_labels.get(row.area_id or "", ""),
        assignee_id=row.assignee_user_id or "",
        scheduled_start=row.starts_at,
        estimated_minutes=row.duration_minutes
        or max(1, int((row.ends_at - row.starts_at).total_seconds() // 60)),
        priority=_task_priority(row.priority),
        status=_task_status(row.state),
        checklist=[],
        photo_evidence=_task_photo_evidence(row.photo_evidence),
        evidence_policy="inherit",
        instructions_ids=list(row.linked_instruction_ids),
        template_id=row.template_id,
        schedule_id=row.schedule_id,
        turnover_bundle_id=None,
        asset_id=None,
        settings_override={},
        assigned_user_id=row.assignee_user_id or "",
        workspace_id=row.workspace_id,
        created_by=row.created_by_user_id or "",
        is_personal=row.is_personal,
    )


def _task_status(
    state: str,
) -> Literal[
    "scheduled",
    "pending",
    "in_progress",
    "completed",
    "skipped",
    "cancelled",
    "overdue",
]:
    if state in {"completed", "approved"}:
        return "completed"
    if state == "scheduled":
        return "scheduled"
    if state == "in_progress":
        return "in_progress"
    if state == "skipped":
        return "skipped"
    if state == "cancelled":
        return "cancelled"
    if state == "overdue":
        return "overdue"
    return "pending"


def _task_priority(value: str) -> Literal["low", "normal", "high", "urgent"]:
    if value == "low":
        return "low"
    if value == "high":
        return "high"
    if value == "urgent":
        return "urgent"
    return "normal"


def _task_photo_evidence(value: str) -> Literal["disabled", "optional", "required"]:
    if value == "optional":
        return "optional"
    if value == "required":
        return "required"
    return "disabled"


def _area_labels(session: Session, area_ids: list[str]) -> dict[str, str]:
    if not area_ids:
        return {}
    rows = session.execute(
        select(Area.id, Area.label).where(Area.id.in_(area_ids))
    ).all()
    return {area_id: label for area_id, label in rows}


def _pending_approvals(
    session: Session,
    ctx: WorkspaceContext,
) -> list[DashboardApproval]:
    rows = session.scalars(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.workspace_id == ctx.workspace_id,
            ApprovalRequest.status == "pending",
        )
        .order_by(ApprovalRequest.created_at.asc(), ApprovalRequest.id.asc())
        .limit(20)
    ).all()
    return [_approval_from_row(row) for row in rows]


def _approval_from_row(row: ApprovalRequest) -> DashboardApproval:
    action = row.action_json
    inline_channel = (
        _string(action, "inline_channel") or row.inline_channel or "desk_only"
    )
    summary = _string(action, "card_summary") or "Review proposed agent action"
    return DashboardApproval(
        id=row.id,
        agent="Agent",
        action=_string(action, "tool_name") or "agent action",
        target=_approval_target(action),
        reason=summary,
        requested_at=row.created_at,
        risk=_approval_risk(action),
        diff=[],
        gate_source=_string(action, "pre_approval_source") or "workspace_configurable",
        gate_destination="desk" if inline_channel == "desk_only" else "inline_chat",
        inline_channel=inline_channel,
        card_summary=summary,
        card_fields=[],
        for_user_id=row.for_user_id,
        resolved_user_mode=row.resolved_user_mode,
    )


def _approval_target(action: dict[str, object]) -> str:
    tool_input = action.get("tool_input")
    if isinstance(tool_input, dict):
        for key, value in tool_input.items():
            if isinstance(value, (str, int, float)):
                return f"{key}: {value}"
    return _string(action, "card_summary") or "Approval request"


def _approval_risk(action: dict[str, object]) -> Literal["low", "medium", "high"]:
    raw = action.get("card_risk")
    if raw == "medium" or raw == "high":
        return raw
    return "low"


def _string(source: dict[str, object], key: str) -> str | None:
    value = source.get(key)
    return value if isinstance(value, str) and value else None


def _pending_leaves(session: Session, ctx: WorkspaceContext) -> list[DashboardLeave]:
    rows = session.scalars(
        select(Leave)
        .where(
            Leave.workspace_id == ctx.workspace_id,
            Leave.status == "pending",
        )
        .order_by(Leave.starts_at.asc(), Leave.id.asc())
        .limit(20)
    ).all()
    return [_leave_from_row(row) for row in rows]


def _leaves_inbox(
    session: Session,
    ctx: WorkspaceContext,
    *,
    now: datetime,
) -> LeavesInboxPayload:
    rows = session.scalars(
        select(Leave)
        .where(
            Leave.workspace_id == ctx.workspace_id,
            or_(
                Leave.status == "pending",
                (Leave.status == "approved") & (Leave.ends_at >= now),
            ),
        )
        .order_by(Leave.starts_at.asc(), Leave.id.asc())
        .limit(100)
    ).all()
    pending: list[DashboardLeave] = []
    approved: list[DashboardLeave] = []
    for row in rows:
        item = _leave_from_row(row)
        if row.status == "pending":
            pending.append(item)
        elif row.status == "approved":
            approved.append(item)
    return LeavesInboxPayload(pending=pending, approved=approved)


def _leave_from_row(row: Leave) -> DashboardLeave:
    return DashboardLeave(
        id=row.id,
        employee_id=row.user_id,
        starts_on=row.starts_at.date(),
        ends_on=row.ends_at.date(),
        category=_leave_category(row.kind),
        note=row.reason_md or "",
        approved_at=row.decided_at if row.status == "approved" else None,
    )


def _leave_from_view(view: LeaveView) -> DashboardLeave:
    return DashboardLeave(
        id=view.id,
        employee_id=view.user_id,
        starts_on=view.starts_at.date(),
        ends_on=view.ends_at.date(),
        category=_leave_category(view.kind),
        note=view.reason_md or "",
        approved_at=view.decided_at if view.status == "approved" else None,
    )


def _leave_category(
    kind: str,
) -> Literal["vacation", "sick", "personal", "bereavement", "other"]:
    if kind == "vacation":
        return "vacation"
    if kind == "sick":
        return "sick"
    if kind == "comp":
        return "personal"
    return "other"


def _decide_leave(
    session: Session,
    ctx: WorkspaceContext,
    *,
    leave_id: str,
    decision: LeaveDecision,
) -> DashboardLeave:
    try:
        view = decide_leave(
            session,
            ctx,
            leave_id=leave_id,
            body=LeaveDecisionRequest(decision=decision),
        )
    except (LeaveNotFound, LeavePermissionDenied) as exc:
        raise NotFound(extra={"error": "leave_not_found"}) from exc
    except LeaveTransitionForbidden as exc:
        raise Conflict(extra={"error": "leave_transition_forbidden"}) from exc
    return _leave_from_view(view)


def _open_issues(session: Session, ctx: WorkspaceContext) -> list[DashboardIssue]:
    rows = session.scalars(
        select(IssueReport)
        .where(
            IssueReport.workspace_id == ctx.workspace_id,
            IssueReport.state != "resolved",
            IssueReport.deleted_at.is_(None),
        )
        .order_by(IssueReport.created_at.desc(), IssueReport.id.desc())
        .limit(20)
    ).all()
    return [
        DashboardIssue(
            id=row.id,
            reported_by=row.reported_by_user_id,
            property_id=row.property_id,
            area=row.area_label or "",
            severity=_issue_severity(row.severity),
            category=_issue_category(row.category),
            title=row.title,
            body=row.description_md,
            reported_at=row.created_at,
            status=_issue_status(row.state),
        )
        for row in rows
    ]


def _issue_severity(value: str) -> Literal["low", "normal", "high", "urgent"]:
    if value == "low":
        return "low"
    if value == "high":
        return "high"
    if value == "urgent":
        return "urgent"
    return "normal"


def _issue_category(
    value: str,
) -> Literal["damage", "broken", "supplies", "safety", "other"]:
    if value == "damage":
        return "damage"
    if value == "broken":
        return "broken"
    if value == "supplies":
        return "supplies"
    if value == "safety":
        return "safety"
    return "other"


def _issue_status(
    value: str,
) -> Literal["open", "in_progress", "resolved", "wont_fix"]:
    if value == "in_progress":
        return "in_progress"
    if value == "resolved":
        return "resolved"
    if value == "wont_fix":
        return "wont_fix"
    return "open"


def _stays_today(
    session: Session,
    ctx: WorkspaceContext,
    now: datetime,
) -> list[DashboardStay]:
    rows = session.scalars(
        select(Reservation)
        .where(
            Reservation.workspace_id == ctx.workspace_id,
            Reservation.check_in <= now,
            Reservation.check_out >= now,
            Reservation.status != "cancelled",
        )
        .order_by(Reservation.check_in.asc(), Reservation.id.asc())
    ).all()
    return [
        DashboardStay(
            id=row.id,
            property_id=row.property_id,
            guest_name=row.guest_name or "Guest",
            source=_stay_source(row.source),
            check_in=row.check_in,
            check_out=row.check_out,
            guests=row.guest_count or 1,
            status=_stay_status(row.status),
        )
        for row in rows
    ]


def _stay_source(
    value: str,
) -> Literal["manual", "airbnb", "vrbo", "booking", "google_calendar", "ical"]:
    if value == "manual":
        return "manual"
    if value == "ical":
        return "google_calendar"
    return "booking"


def _stay_status(
    value: str,
) -> Literal["tentative", "confirmed", "in_house", "checked_out", "cancelled"]:
    if value == "checked_in":
        return "in_house"
    if value == "completed":
        return "checked_out"
    if value == "cancelled":
        return "cancelled"
    return "confirmed"
