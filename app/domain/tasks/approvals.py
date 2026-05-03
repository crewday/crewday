"""Manager approval service for completed tasks (cd-z2py)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import Property
from app.adapters.db.tasks.models import (
    Evidence,
    Occurrence,
    TaskApproval,
    TaskTemplate,
)
from app.audit import write_audit
from app.authz import (
    ApprovalRequired,
    EmptyPermissionRuleRepository,
    InvalidScope,
    PermissionRuleRepository,
    UnknownActionKey,
    require,
)
from app.authz import PermissionDenied as AuthzPermissionDenied
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import (
    TaskApprovalRequested,
    TaskApproved,
    TaskChangesRequested,
    TaskRejected,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "ApprovalNotFound",
    "ApprovalNotOpen",
    "ApprovalPermissionDenied",
    "ApprovalState",
    "ApprovalView",
    "TaskNotCompleted",
    "TaskNotFound",
    "approve",
    "list_pending",
    "reject",
    "request_changes",
    "request_review",
]


ApprovalState = Literal["pending", "approved", "rejected", "changes_requested"]
_OPEN_STATES: frozenset[str] = frozenset({"pending", "changes_requested"})


@dataclass(frozen=True, slots=True)
class ApprovalView:
    approval_id: str
    task_id: str
    state: ApprovalState
    title: str
    property_id: str | None
    property_name: str | None
    completed_by_user_id: str | None
    completed_at: datetime | None
    evidence_count: int
    requested_at: datetime
    note_md: str | None = None


class TaskNotFound(LookupError):
    """The task id is unknown in the caller's workspace."""


class ApprovalNotFound(LookupError):
    """The approval id is unknown in the caller's workspace."""


class TaskNotCompleted(ValueError):
    """A review can only be requested after task completion."""


class ApprovalNotOpen(ValueError):
    """The approval row is already terminal."""


class ApprovalPermissionDenied(PermissionError):
    """The caller cannot decide task approvals."""


def request_review(
    session: Session,
    ctx: WorkspaceContext,
    task_id: str,
    *,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> ApprovalView:
    """Create a pending review row for a completed task."""
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus

    task = _load_task(session, ctx, task_id)
    if task.state != "completed" or task.completed_at is None:
        raise TaskNotCompleted(f"task {task_id!r} is not completed")

    now = resolved_clock.now()
    row = TaskApproval(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        task_id=task.id,
        requested_at=now,
        requested_by_user_id=ctx.actor_id,
        state="pending",
        decided_at=None,
        decided_by_user_id=None,
        note_md=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()

    _audit(
        session,
        ctx,
        resolved_clock,
        row=row,
        action="task.approval_requested",
        diff={"after": _approval_diff(row)},
    )
    resolved_bus.publish(
        TaskApprovalRequested(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=resolved_clock.now(),
            task_id=task.id,
            approval_id=row.id,
            decided_by_user_id=None,
            state="pending",
            note_md=None,
        )
    )
    return _row_to_view(session, row, task)


def approve(
    session: Session,
    ctx: WorkspaceContext,
    approval_id: str,
    *,
    note_md: str | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
    rule_repo: PermissionRuleRepository | None = None,
) -> ApprovalView:
    return _decide(
        session,
        ctx,
        approval_id,
        target_state="approved",
        note_md=note_md,
        clock=clock,
        event_bus=event_bus,
        rule_repo=rule_repo,
    )


def reject(
    session: Session,
    ctx: WorkspaceContext,
    approval_id: str,
    *,
    note_md: str | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
    rule_repo: PermissionRuleRepository | None = None,
) -> ApprovalView:
    return _decide(
        session,
        ctx,
        approval_id,
        target_state="rejected",
        note_md=note_md,
        clock=clock,
        event_bus=event_bus,
        rule_repo=rule_repo,
    )


def request_changes(
    session: Session,
    ctx: WorkspaceContext,
    approval_id: str,
    *,
    note_md: str | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
    rule_repo: PermissionRuleRepository | None = None,
) -> ApprovalView:
    return _decide(
        session,
        ctx,
        approval_id,
        target_state="changes_requested",
        note_md=note_md,
        clock=clock,
        event_bus=event_bus,
        rule_repo=rule_repo,
    )


def list_pending(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str | None = None,
) -> list[ApprovalView]:
    """Return open task approvals for the manager queue."""
    stmt = (
        select(TaskApproval, Occurrence, TaskTemplate, Property)
        .join(
            Occurrence,
            (Occurrence.id == TaskApproval.task_id)
            & (Occurrence.workspace_id == TaskApproval.workspace_id),
        )
        .outerjoin(
            TaskTemplate,
            (TaskTemplate.id == Occurrence.template_id)
            & (TaskTemplate.workspace_id == Occurrence.workspace_id),
        )
        .outerjoin(Property, Property.id == Occurrence.property_id)
        .where(
            TaskApproval.workspace_id == ctx.workspace_id,
            TaskApproval.state.in_(tuple(_OPEN_STATES)),
        )
        .order_by(TaskApproval.requested_at.asc(), TaskApproval.id.asc())
    )
    if property_id is not None:
        stmt = stmt.where(Occurrence.property_id == property_id)

    rows = session.execute(stmt).all()
    task_ids = [task.id for _approval, task, _template, _property in rows]
    counts = _evidence_counts(session, task_ids)
    return [
        _row_to_view(
            session,
            approval,
            task,
            template_row,
            property_row,
            counts.get(task.id, 0),
        )
        for approval, task, template_row, property_row in rows
    ]


def _decide(
    session: Session,
    ctx: WorkspaceContext,
    approval_id: str,
    *,
    target_state: ApprovalState,
    note_md: str | None,
    clock: Clock | None,
    event_bus: EventBus | None,
    rule_repo: PermissionRuleRepository | None,
) -> ApprovalView:
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus

    row, task = _load_approval_and_task(session, ctx, approval_id)
    _assert_can_decide(session, ctx, task, rule_repo=rule_repo)
    if row.state not in _OPEN_STATES:
        raise ApprovalNotOpen(f"approval {approval_id!r} is already {row.state}")

    before = _approval_diff(row)
    now = resolved_clock.now()
    row.state = target_state
    row.decided_at = now
    row.decided_by_user_id = ctx.actor_id
    row.note_md = _clean_note(note_md)
    row.updated_at = now
    session.flush()

    action = _action_for_state(target_state)
    _audit(
        session,
        ctx,
        resolved_clock,
        row=row,
        action=action,
        diff={"before": before, "after": _approval_diff(row)},
    )
    _publish_decision(
        resolved_bus,
        ctx,
        resolved_clock,
        row=row,
        task=task,
        target_state=target_state,
        action=action,
    )
    return _row_to_view(session, row, task)


def _load_task(session: Session, ctx: WorkspaceContext, task_id: str) -> Occurrence:
    row = session.scalar(
        select(Occurrence).where(
            Occurrence.id == task_id,
            Occurrence.workspace_id == ctx.workspace_id,
        )
    )
    if row is None:
        raise TaskNotFound(f"task {task_id!r} not visible in workspace")
    return row


def _load_approval_and_task(
    session: Session, ctx: WorkspaceContext, approval_id: str
) -> tuple[TaskApproval, Occurrence]:
    row = session.scalar(
        select(TaskApproval).where(
            TaskApproval.id == approval_id,
            TaskApproval.workspace_id == ctx.workspace_id,
        )
    )
    if row is None:
        raise ApprovalNotFound(f"approval {approval_id!r} not visible in workspace")
    task = _load_task(session, ctx, row.task_id)
    return row, task


def _assert_can_decide(
    session: Session,
    ctx: WorkspaceContext,
    task: Occurrence,
    *,
    rule_repo: PermissionRuleRepository | None,
) -> None:
    repo = rule_repo if rule_repo is not None else EmptyPermissionRuleRepository()
    scope_kind = "property" if task.property_id is not None else "workspace"
    scope_id = task.property_id if task.property_id is not None else ctx.workspace_id
    try:
        require(
            session,
            ctx,
            action_key="tasks.review.decide",
            scope_kind=scope_kind,
            scope_id=scope_id,
            rule_repo=repo,
        )
    except (UnknownActionKey, InvalidScope) as exc:
        raise RuntimeError(
            f"authz catalog misconfigured for 'tasks.review.decide': {exc!s}"
        ) from exc
    except ApprovalRequired as exc:
        # The decide verb is itself the HITL surface — flagging it
        # ``requires_approval=True`` would create a recursive gate. Fail
        # loudly so the catalog mistake is visible during boot or tests
        # rather than silently 500-ing the operator's decision.
        raise RuntimeError(
            "authz catalog misconfigured: 'tasks.review.decide' must not be "
            f"requires_approval=True (would loop the HITL gate): {exc!s}"
        ) from exc
    except AuthzPermissionDenied as exc:
        raise ApprovalPermissionDenied(str(exc)) from exc


def _evidence_counts(session: Session, task_ids: Sequence[str]) -> dict[str, int]:
    if not task_ids:
        return {}
    rows = session.execute(
        select(Evidence.occurrence_id, Evidence.id).where(
            Evidence.occurrence_id.in_(task_ids),
            Evidence.deleted_at.is_(None),
        )
    ).all()
    counts: dict[str, int] = {}
    for task_id, _evidence_id in rows:
        counts[task_id] = counts.get(task_id, 0) + 1
    return counts


def _row_to_view(
    session: Session,
    row: TaskApproval,
    task: Occurrence,
    template_row: TaskTemplate | None = None,
    property_row: Property | None = None,
    evidence_count: int | None = None,
) -> ApprovalView:
    resolved_evidence_count = (
        evidence_count
        if evidence_count is not None
        else _evidence_counts(session, [task.id]).get(task.id, 0)
    )
    title = _task_title(task, template_row)
    return ApprovalView(
        approval_id=row.id,
        task_id=task.id,
        state=_approval_state(row.state),
        title=title,
        property_id=task.property_id,
        property_name=_property_name(property_row),
        completed_by_user_id=task.completed_by_user_id,
        completed_at=task.completed_at,
        evidence_count=resolved_evidence_count,
        requested_at=row.requested_at,
        note_md=row.note_md,
    )


def _property_name(row: Property | None) -> str | None:
    if row is None:
        return None
    return row.name if row.name is not None else row.address


def _task_title(task: Occurrence, template: TaskTemplate | None) -> str:
    if task.title:
        return task.title
    if template is not None and template.name:
        return template.name
    if template is not None:
        return template.title
    return "Untitled task"


def _approval_state(value: str) -> ApprovalState:
    if value == "pending":
        return "pending"
    if value == "approved":
        return "approved"
    if value == "rejected":
        return "rejected"
    if value == "changes_requested":
        return "changes_requested"
    raise RuntimeError(f"unknown task approval state {value!r}")


def _approval_diff(row: TaskApproval) -> dict[str, str | None]:
    return {
        "approval_id": row.id,
        "task_id": row.task_id,
        "state": row.state,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "decided_by_user_id": row.decided_by_user_id,
        "note_md": row.note_md,
    }


def _clean_note(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _action_for_state(state: ApprovalState) -> str:
    if state == "approved":
        return "task.approved"
    if state == "rejected":
        return "task.rejected"
    if state == "changes_requested":
        return "task.changes_requested"
    raise RuntimeError(f"cannot decide approval to state {state!r}")


def _publish_decision(
    event_bus: EventBus,
    ctx: WorkspaceContext,
    clock: Clock,
    *,
    row: TaskApproval,
    task: Occurrence,
    target_state: ApprovalState,
    action: str,
) -> None:
    payload = {
        "workspace_id": ctx.workspace_id,
        "actor_id": ctx.actor_id,
        "correlation_id": ctx.audit_correlation_id,
        "occurred_at": clock.now(),
        "task_id": task.id,
        "approval_id": row.id,
        "decided_by_user_id": ctx.actor_id,
        "state": target_state,
        "note_md": row.note_md,
    }
    if action == "task.approved":
        event_bus.publish(TaskApproved(**payload))
    elif action == "task.rejected":
        event_bus.publish(TaskRejected(**payload))
    elif action == "task.changes_requested":
        event_bus.publish(TaskChangesRequested(**payload))
    else:
        raise RuntimeError(f"unknown task approval action {action!r}")


def _audit(
    session: Session,
    ctx: WorkspaceContext,
    clock: Clock,
    *,
    row: TaskApproval,
    action: str,
    diff: dict[str, object],
) -> None:
    write_audit(
        session,
        ctx,
        entity_kind="task_approval",
        entity_id=row.id,
        action=action,
        diff=diff,
        clock=clock,
    )
