"""Task notification fanout helpers."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

from app.adapters.notifications.ports import NotificationKind
from app.events.bus import EventBus
from app.tenancy import WorkspaceContext
from app.util.clock import Clock

__all__ = [
    "TaskNotificationSink",
    "notify_comment_mentions",
    "notify_task_assigned",
    "notify_task_overdue",
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


class TaskNotificationSubject(Protocol):
    id: str
    title: str | None
    starts_at: datetime
    ends_at: datetime
    assignee_user_id: str | None


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
) -> TaskNotificationSink | None:
    _ = session, ctx, clock, bus
    return None


def _task_context(task: TaskNotificationSubject) -> _TaskNotificationContext:
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
    task: TaskNotificationSubject,
    recipient_user_id: str,
    clock: Clock,
    bus: EventBus,
    sink: TaskNotificationSink | None = None,
) -> None:
    service = sink or _default_sink(session, ctx, clock=clock, bus=bus)
    if service is None:
        return
    _notify(
        service,
        recipient_user_id=recipient_user_id,
        kind=NotificationKind.TASK_ASSIGNED,
        payload=_task_payload(_task_context(task)),
    )


def notify_task_overdue(
    session: Session,
    ctx: WorkspaceContext,
    *,
    task: TaskNotificationSubject,
    overdue_since: datetime,
    slipped_minutes: int,
    clock: Clock,
    bus: EventBus,
    recipient_user_ids: Sequence[str] = (),
    sink: TaskNotificationSink | None = None,
) -> None:
    service = sink or _default_sink(session, ctx, clock=clock, bus=bus)
    if service is None:
        return
    payload = {
        **_task_payload(_task_context(task)),
        "overdue_since": overdue_since.isoformat(),
        "slipped_minutes": slipped_minutes,
    }
    recipients = set(recipient_user_ids)
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
    if service is None:
        return
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
