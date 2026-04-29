"""Worker issue reporting service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.issues.models import IssueReport
from app.adapters.db.places.models import Area, Property, PropertyWorkspace
from app.adapters.db.tasks.models import Occurrence
from app.audit import write_audit
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import IssueReported
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "ISSUE_CATEGORIES",
    "ISSUE_SEVERITIES",
    "ISSUE_STATES",
    "IssueAccessDenied",
    "IssueCreate",
    "IssueNotFound",
    "IssueUpdate",
    "IssueValidationError",
    "IssueView",
    "create_issue",
    "get_issue",
    "list_issues",
    "update_issue",
]


ISSUE_SEVERITIES: tuple[str, ...] = ("low", "normal", "high", "urgent")
ISSUE_CATEGORIES: tuple[str, ...] = ("damage", "broken", "supplies", "safety", "other")
ISSUE_STATES: tuple[str, ...] = ("open", "in_progress", "resolved", "wont_fix")
IssueSeverity = Literal["low", "normal", "high", "urgent"]
IssueCategory = Literal["damage", "broken", "supplies", "safety", "other"]
IssueState = Literal["open", "in_progress", "resolved", "wont_fix"]


class IssueNotFound(LookupError):
    """No issue matched the caller's workspace."""


class IssueAccessDenied(PermissionError):
    """The caller cannot report or mutate this issue."""


class IssueValidationError(ValueError):
    """Submitted issue data failed validation."""

    __slots__ = ("error", "field")

    def __init__(self, field: str, error: str) -> None:
        super().__init__(f"{field}: {error}")
        self.field = field
        self.error = error


class IssueCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=200)
    severity: IssueSeverity = "normal"
    category: IssueCategory = "other"
    property_id: str
    area_id: str | None = None
    area: str | None = Field(default=None, max_length=200)
    body: str = Field(default="", max_length=20_000)
    task_id: str | None = None
    attachment_file_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _normalise(self) -> IssueCreate:
        if not self.title.strip():
            raise ValueError("title must be non-blank")
        return self


class IssueUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    severity: IssueSeverity | None = None
    category: IssueCategory | None = None
    state: IssueState | None = None
    body: str | None = Field(default=None, max_length=20_000)
    resolution_note: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def _validate_sparse(self) -> IssueUpdate:
        if not self.model_fields_set:
            raise ValueError("PATCH body must include at least one field")
        return self


@dataclass(frozen=True, slots=True)
class IssueView:
    id: str
    workspace_id: str
    reported_by_user_id: str
    reported_by: str
    property_id: str
    area_id: str | None
    area: str
    task_id: str | None
    title: str
    description_md: str
    body: str
    severity: str
    category: str
    state: str
    status: str
    attachment_file_ids: list[str]
    converted_to_task_id: str | None
    resolution_note: str | None
    resolved_at: datetime | None
    resolved_by: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    reported_at: datetime


def create_issue(
    session: Session,
    ctx: WorkspaceContext,
    *,
    body: IssueCreate,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> IssueView:
    """Create a worker/manager-reported property issue."""
    _assert_can_create(ctx)
    _validate_property_visible(session, ctx, body.property_id)
    if ctx.actor_grant_role == "worker":
        _assert_worker_property_grant(session, ctx, body.property_id)
    if body.area_id is not None:
        _validate_area(session, property_id=body.property_id, area_id=body.area_id)
    if body.task_id is not None:
        _validate_task(session, ctx, task_id=body.task_id, property_id=body.property_id)

    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = IssueReport(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        reported_by_user_id=ctx.actor_id,
        property_id=body.property_id,
        area_id=body.area_id,
        area_label=_clean_text(body.area),
        task_id=body.task_id,
        title=body.title.strip(),
        description_md=body.body.strip(),
        severity=body.severity,
        category=body.category,
        state="open",
        attachment_file_ids_json=list(body.attachment_file_ids),
        converted_to_task_id=None,
        resolution_note=None,
        resolved_at=None,
        resolved_by=None,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(row)
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="issue",
        entity_id=row.id,
        action="issue.create",
        diff={"after": _audit_dict(row)},
        clock=resolved_clock,
    )
    (event_bus if event_bus is not None else default_event_bus).publish(
        IssueReported(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=_as_utc(now),
            issue_id=row.id,
            property_id=row.property_id,
            severity=cast(IssueSeverity, row.severity),
        )
    )
    return _row_to_view(row)


def list_issues(
    session: Session,
    ctx: WorkspaceContext,
    *,
    state: str | None = None,
    property_id: str | None = None,
    limit: int = 100,
) -> list[IssueView]:
    """List issues visible to the caller."""
    if ctx.actor_grant_role in {"client", "guest"}:
        raise IssueAccessDenied("clients and guests cannot list issues")
    stmt = select(IssueReport).where(IssueReport.workspace_id == ctx.workspace_id)
    stmt = stmt.where(IssueReport.deleted_at.is_(None))
    if state is not None:
        stmt = stmt.where(IssueReport.state == _validate_state(state))
    if property_id is not None:
        _validate_property_visible(session, ctx, property_id)
        stmt = stmt.where(IssueReport.property_id == property_id)
    if ctx.actor_grant_role == "worker":
        stmt = stmt.where(IssueReport.reported_by_user_id == ctx.actor_id)
    stmt = stmt.order_by(IssueReport.created_at.desc(), IssueReport.id.desc()).limit(
        limit
    )
    with tenant_agnostic():
        rows = session.scalars(stmt).all()
    return [_row_to_view(row) for row in rows]


def get_issue(session: Session, ctx: WorkspaceContext, issue_id: str) -> IssueView:
    """Return one issue visible to the caller."""
    row = _load_issue(session, ctx, issue_id)
    if ctx.actor_grant_role == "worker" and row.reported_by_user_id != ctx.actor_id:
        raise IssueNotFound(issue_id)
    if ctx.actor_grant_role in {"client", "guest"}:
        raise IssueNotFound(issue_id)
    return _row_to_view(row)


def update_issue(
    session: Session,
    ctx: WorkspaceContext,
    issue_id: str,
    *,
    body: IssueUpdate,
    clock: Clock | None = None,
) -> IssueView:
    """Patch an issue. Managers can mutate; workers can edit their open issues."""
    row = _load_issue(session, ctx, issue_id)
    if not _can_update(ctx, row):
        raise IssueAccessDenied(issue_id)
    before = _audit_dict(row)
    changed: list[str] = []
    if body.title is not None and body.title.strip() != row.title:
        row.title = body.title.strip()
        changed.append("title")
    if body.severity is not None and body.severity != row.severity:
        row.severity = body.severity
        changed.append("severity")
    if body.category is not None and body.category != row.category:
        row.category = body.category
        changed.append("category")
    if body.body is not None and body.body.strip() != row.description_md:
        row.description_md = body.body.strip()
        changed.append("description_md")
    if body.resolution_note is not None and body.resolution_note != row.resolution_note:
        row.resolution_note = body.resolution_note
        changed.append("resolution_note")
    if body.state is not None and body.state != row.state:
        row.state = body.state
        changed.append("state")
        if body.state in {"resolved", "wont_fix"}:
            now_for_resolution = (clock if clock is not None else SystemClock()).now()
            row.resolved_at = now_for_resolution
            row.resolved_by = ctx.actor_id
            changed.extend(["resolved_at", "resolved_by"])
        else:
            row.resolved_at = None
            row.resolved_by = None
            changed.extend(["resolved_at", "resolved_by"])
    if not changed:
        return _row_to_view(row)

    resolved_clock = clock if clock is not None else SystemClock()
    row.updated_at = resolved_clock.now()
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="issue",
        entity_id=row.id,
        action="issue.update",
        diff={"before": before, "after": _audit_dict(row)},
        clock=resolved_clock,
    )
    return _row_to_view(row)


def _load_issue(session: Session, ctx: WorkspaceContext, issue_id: str) -> IssueReport:
    with tenant_agnostic():
        row = session.scalars(
            select(IssueReport).where(
                IssueReport.workspace_id == ctx.workspace_id,
                IssueReport.id == issue_id,
                IssueReport.deleted_at.is_(None),
            )
        ).one_or_none()
    if row is None:
        raise IssueNotFound(issue_id)
    return row


def _assert_can_create(ctx: WorkspaceContext) -> None:
    if ctx.actor_grant_role not in {"manager", "worker"}:
        raise IssueAccessDenied("only managers and workers can report issues")


def _validate_property_visible(
    session: Session, ctx: WorkspaceContext, property_id: str
) -> None:
    with tenant_agnostic():
        exists = session.scalar(
            select(Property.id)
            .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
            .where(
                Property.id == property_id,
                Property.deleted_at.is_(None),
                PropertyWorkspace.workspace_id == ctx.workspace_id,
                PropertyWorkspace.status == "active",
            )
            .limit(1)
        )
    if exists is None:
        raise IssueValidationError("property_id", "not_visible")


def _assert_worker_property_grant(
    session: Session, ctx: WorkspaceContext, property_id: str
) -> None:
    with tenant_agnostic():
        grant = session.scalar(
            select(RoleGrant.id)
            .where(
                RoleGrant.workspace_id == ctx.workspace_id,
                RoleGrant.user_id == ctx.actor_id,
                RoleGrant.grant_role == "worker",
                RoleGrant.scope_kind == "workspace",
                or_(
                    RoleGrant.scope_property_id == property_id,
                    RoleGrant.scope_property_id.is_(None),
                ),
            )
            .limit(1)
        )
    if grant is None:
        raise IssueValidationError("property_id", "not_visible")


def _validate_area(session: Session, *, property_id: str, area_id: str) -> None:
    with tenant_agnostic():
        exists = session.scalar(
            select(Area.id)
            .where(
                Area.id == area_id,
                Area.property_id == property_id,
                Area.deleted_at.is_(None),
            )
            .limit(1)
        )
    if exists is None:
        raise IssueValidationError("area_id", "not_visible")


def _validate_task(
    session: Session, ctx: WorkspaceContext, *, task_id: str, property_id: str
) -> None:
    with tenant_agnostic():
        exists = session.scalar(
            select(Occurrence.id)
            .where(
                Occurrence.id == task_id,
                Occurrence.workspace_id == ctx.workspace_id,
                Occurrence.property_id == property_id,
            )
            .limit(1)
        )
    if exists is None:
        raise IssueValidationError("task_id", "not_visible")


def _can_update(ctx: WorkspaceContext, row: IssueReport) -> bool:
    if ctx.actor_grant_role == "manager":
        return True
    return (
        ctx.actor_grant_role == "worker"
        and row.reported_by_user_id == ctx.actor_id
        and row.state == "open"
    )


def _validate_state(state: str) -> IssueState:
    if state not in ISSUE_STATES:
        raise IssueValidationError("state", "invalid")
    return cast(IssueState, state)


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _audit_dict(row: IssueReport) -> dict[str, object | None]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "reported_by_user_id": row.reported_by_user_id,
        "property_id": row.property_id,
        "area_id": row.area_id,
        "title": row.title,
        "severity": row.severity,
        "category": row.category,
        "state": row.state,
        "converted_to_task_id": row.converted_to_task_id,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "resolved_by": row.resolved_by,
        "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
    }


def _row_to_view(row: IssueReport) -> IssueView:
    area = row.area_label or row.area_id or ""
    attachments = row.attachment_file_ids_json
    return IssueView(
        id=row.id,
        workspace_id=row.workspace_id,
        reported_by_user_id=row.reported_by_user_id,
        reported_by=row.reported_by_user_id,
        property_id=row.property_id,
        area_id=row.area_id,
        area=area,
        task_id=row.task_id,
        title=row.title,
        description_md=row.description_md,
        body=row.description_md,
        severity=row.severity,
        category=row.category,
        state=row.state,
        status=row.state,
        attachment_file_ids=list(attachments) if isinstance(attachments, list) else [],
        converted_to_task_id=row.converted_to_task_id,
        resolution_note=row.resolution_note,
        resolved_at=row.resolved_at,
        resolved_by=row.resolved_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
        reported_at=row.created_at,
    )
