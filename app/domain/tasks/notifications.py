"""Task notification fanout helpers."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.messaging.repositories import SqlAlchemyEmailDeliveryRepository
from app.adapters.db.tasks.models import Occurrence
from app.adapters.mail.null import NullMailer
from app.domain.messaging.notifications import NotificationKind, NotificationService
from app.events.bus import EventBus
from app.tenancy import WorkspaceContext
from app.util.clock import Clock

__all__ = [
    "TaskNotificationSink",
    "notify_comment_mentions",
    "notify_task_assigned",
    "notify_task_overdue",
    "owner_manager_recipient_ids",
]

_log = logging.getLogger(__name__)


class TaskNotificationSink(Protocol):
    def notify(
        self,
        *,
        recipient_user_id: str,
        kind: NotificationKind,
        payload: Mapping[str, object],
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class _TaskNotificationContext:
    task_id: str
    task_title: str
    starts_at: datetime
    ends_at: datetime


def _default_sink(
    session: Session,
    ctx: WorkspaceContext,
    *,
    clock: Clock,
    bus: EventBus,
) -> NotificationService:
    return NotificationService(
        session=session,
        ctx=ctx,
        mailer=NullMailer(),
        clock=clock,
        bus=bus,
        email_deliveries=SqlAlchemyEmailDeliveryRepository(session),
    )


def _task_context(task: Occurrence) -> _TaskNotificationContext:
    return _TaskNotificationContext(
        task_id=task.id,
        task_title=task.title or "Task",
        starts_at=task.starts_at,
        ends_at=task.ends_at,
    )


def notify_task_assigned(
    session: Session,
    ctx: WorkspaceContext,
    *,
    task: Occurrence,
    recipient_user_id: str,
    clock: Clock,
    bus: EventBus,
    sink: TaskNotificationSink | None = None,
) -> None:
    _notify(
        sink or _default_sink(session, ctx, clock=clock, bus=bus),
        recipient_user_id=recipient_user_id,
        kind=NotificationKind.TASK_ASSIGNED,
        payload=_task_payload(_task_context(task)),
    )


def notify_task_overdue(
    session: Session,
    ctx: WorkspaceContext,
    *,
    task: Occurrence,
    overdue_since: datetime,
    slipped_minutes: int,
    clock: Clock,
    bus: EventBus,
    sink: TaskNotificationSink | None = None,
) -> None:
    service = sink or _default_sink(session, ctx, clock=clock, bus=bus)
    payload = {
        **_task_payload(_task_context(task)),
        "overdue_since": overdue_since.isoformat(),
        "slipped_minutes": slipped_minutes,
    }
    recipients = set(owner_recipient_ids(session, ctx.workspace_id))
    if not task.is_personal:
        recipients.update(manager_recipient_ids(session, ctx.workspace_id))
    if task.assignee_user_id is not None:
        recipients.add(task.assignee_user_id)
    for user_id in sorted(recipients):
        _notify(
            service,
            recipient_user_id=user_id,
            kind=NotificationKind.TASK_OVERDUE,
            payload=payload,
        )


def notify_comment_mentions(
    session: Session,
    ctx: WorkspaceContext,
    *,
    task_id: str,
    task_title: str,
    comment_id: str,
    comment_body_md: str,
    mentioned_user_ids: Iterable[str],
    clock: Clock,
    bus: EventBus,
    sink: TaskNotificationSink | None = None,
) -> None:
    service = sink or _default_sink(session, ctx, clock=clock, bus=bus)
    payload = {
        "task_id": task_id,
        "task_title": task_title or "Task",
        "comment_id": comment_id,
        "comment_body_md": comment_body_md,
        "actor_user_id": ctx.actor_id,
    }
    for user_id in sorted(set(mentioned_user_ids)):
        _notify(
            service,
            recipient_user_id=user_id,
            kind=NotificationKind.COMMENT_MENTION,
            payload=payload,
        )


def owner_manager_recipient_ids(session: Session, workspace_id: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            set(owner_recipient_ids(session, workspace_id)).union(
                manager_recipient_ids(session, workspace_id)
            )
        )
    )


def owner_recipient_ids(session: Session, workspace_id: str) -> tuple[str, ...]:
    owner_ids = session.scalars(
        select(PermissionGroupMember.user_id)
        .join(PermissionGroup, PermissionGroup.id == PermissionGroupMember.group_id)
        .where(PermissionGroup.workspace_id == workspace_id)
        .where(PermissionGroup.slug == "owners")
    ).all()
    return tuple(sorted(set(owner_ids)))


def manager_recipient_ids(session: Session, workspace_id: str) -> tuple[str, ...]:
    manager_ids = session.scalars(
        select(RoleGrant.user_id)
        .where(RoleGrant.workspace_id == workspace_id)
        .where(RoleGrant.scope_kind == "workspace")
        .where(RoleGrant.grant_role == "manager")
        .where(RoleGrant.revoked_at.is_(None))
    ).all()
    return tuple(sorted(set(manager_ids)))


def _task_payload(task: _TaskNotificationContext) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "task_title": task.task_title,
        "starts_at": task.starts_at.isoformat(),
        "ends_at": task.ends_at.isoformat(),
    }


def _notify(
    sink: TaskNotificationSink,
    *,
    recipient_user_id: str,
    kind: NotificationKind,
    payload: Mapping[str, object],
) -> None:
    try:
        sink.notify(
            recipient_user_id=recipient_user_id,
            kind=kind,
            payload=payload,
        )
    except Exception:
        _log.exception(
            "task notification fanout failed",
            extra={
                "event": "tasks.notification.failed",
                "kind": kind.value,
                "recipient_user_id": recipient_user_id,
            },
        )
