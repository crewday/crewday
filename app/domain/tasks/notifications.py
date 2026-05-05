"""Task notification fanout helpers."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from app.adapters.notifications.ports import NotificationKind
from app.events.bus import EventBus
from app.tenancy import WorkspaceContext
from app.util.clock import Clock

__all__ = [
    "CommentMentionNotification",
    "LegacyTaskOptions",
    "TaskNotificationRuntime",
    "TaskNotificationSink",
    "TaskOverdueNotification",
    "notify_comment_mentions",
    "notify_task_assigned",
    "notify_task_overdue",
]

_log = logging.getLogger(__name__)


@runtime_checkable
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


@dataclass(slots=True)
class LegacyTaskOptions:
    owner: str
    values: dict[str, object]

    def reject_if_combined(self, has_options: bool) -> None:
        if has_options and self.values:
            raise TypeError(f"{self.owner} received options plus legacy keywords")

    def reject_unknown(self) -> None:
        if self.values:
            unknown = ", ".join(sorted(self.values))
            raise TypeError(f"unexpected {self.owner} keyword(s): {unknown}")

    def pop_session(self) -> Session:
        value = self.values.pop("session", None)
        if isinstance(value, Session):
            return value
        raise TypeError("session must be Session")

    def pop_clock(self, key: str) -> Clock | None:
        value = self.values.pop(key, None)
        if value is None or isinstance(value, Clock):
            return value
        raise TypeError(f"{key} must implement Clock")

    def pop_event_bus(self, key: str) -> EventBus | None:
        value = self.values.pop(key, None)
        if value is None or isinstance(value, EventBus):
            return value
        raise TypeError(f"{key} must be EventBus")

    def pop_notifications(self, key: str) -> TaskNotificationSink | None:
        value = self.values.pop(key, None)
        if value is None or isinstance(value, TaskNotificationSink):
            return value
        raise TypeError(f"{key} must implement TaskNotificationSink")

    def pop_datetime(self, key: str) -> datetime | None:
        value = self.values.pop(key, None)
        if value is None or isinstance(value, datetime):
            return value
        raise TypeError(f"{key} must be datetime")

    def pop_int(self, key: str) -> int | None:
        value = self.values.pop(key, None)
        if value is None:
            return None
        if isinstance(value, int):
            return value
        raise TypeError(f"{key} must be int")


@dataclass(frozen=True, slots=True)
class TaskNotificationRuntime:
    session: Session
    ctx: WorkspaceContext
    clock: Clock
    bus: EventBus
    sink: TaskNotificationSink | None = None


@dataclass(frozen=True, slots=True)
class _TaskNotificationContext:
    task_id: str
    task_title: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class TaskOverdueNotification:
    task: TaskNotificationSubject
    overdue_since: datetime
    slipped_minutes: int
    recipient_user_ids: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class CommentMentionNotification:
    task_id: str
    task_title: str
    comment_id: str
    comment_body_md: str
    mentioned_user_ids: Iterable[str]


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


def notify_task_assigned(  # code-health: ignore[params] Legacy keyword API.
    runtime: TaskNotificationRuntime | Session,
    ctx: WorkspaceContext | None = None,
    *,
    task: TaskNotificationSubject,
    recipient_user_id: str,
    clock: Clock | None = None,
    bus: EventBus | None = None,
    sink: TaskNotificationSink | None = None,
) -> None:
    resolved_runtime = _notification_runtime(
        "notify_task_assigned",
        runtime,
        ctx,
        clock=clock,
        bus=bus,
        sink=sink,
    )
    service = _sink_for(resolved_runtime)
    if service is None:
        return
    _notify(
        service,
        recipient_user_id=recipient_user_id,
        kind=NotificationKind.TASK_ASSIGNED,
        payload=_task_payload(_task_context(task)),
    )


def notify_task_overdue(  # code-health: ignore[params] Legacy keyword API.
    runtime: TaskNotificationRuntime | Session,
    ctx: WorkspaceContext | None = None,
    *,
    notification: TaskOverdueNotification | None = None,
    task: TaskNotificationSubject | None = None,
    overdue_since: datetime | None = None,
    slipped_minutes: int | None = None,
    clock: Clock | None = None,
    bus: EventBus | None = None,
    recipient_user_ids: Sequence[str] = (),
    sink: TaskNotificationSink | None = None,
) -> None:
    resolved_runtime = _notification_runtime(
        "notify_task_overdue",
        runtime,
        ctx,
        clock=clock,
        bus=bus,
        sink=sink,
    )
    resolved_notification = _overdue_notification(
        notification,
        task=task,
        overdue_since=overdue_since,
        slipped_minutes=slipped_minutes,
        recipient_user_ids=recipient_user_ids,
    )
    service = _sink_for(resolved_runtime)
    if service is None:
        return
    task = resolved_notification.task
    payload = {
        **_task_payload(_task_context(task)),
        "overdue_since": resolved_notification.overdue_since.isoformat(),
        "slipped_minutes": resolved_notification.slipped_minutes,
    }
    recipients = set(resolved_notification.recipient_user_ids)
    if task.assignee_user_id is not None:
        recipients.add(task.assignee_user_id)
    for user_id in sorted(recipients):
        _notify(
            service,
            recipient_user_id=user_id,
            kind=NotificationKind.TASK_OVERDUE,
            payload=payload,
        )


def notify_comment_mentions(  # code-health: ignore[params] Legacy keyword API.
    runtime: TaskNotificationRuntime | Session,
    ctx: WorkspaceContext | None = None,
    *,
    notification: CommentMentionNotification | None = None,
    task_id: str | None = None,
    task_title: str | None = None,
    comment_id: str | None = None,
    comment_body_md: str | None = None,
    mentioned_user_ids: Iterable[str] | None = None,
    clock: Clock | None = None,
    bus: EventBus | None = None,
    sink: TaskNotificationSink | None = None,
) -> None:
    resolved_runtime = _notification_runtime(
        "notify_comment_mentions",
        runtime,
        ctx,
        clock=clock,
        bus=bus,
        sink=sink,
    )
    resolved_notification = _comment_mention_notification(
        notification,
        task_id=task_id,
        task_title=task_title,
        comment_id=comment_id,
        comment_body_md=comment_body_md,
        mentioned_user_ids=mentioned_user_ids,
    )
    service = _sink_for(resolved_runtime)
    if service is None:
        return
    payload = {
        "task_id": resolved_notification.task_id,
        "task_title": resolved_notification.task_title or "Task",
        "comment_id": resolved_notification.comment_id,
        "comment_body_md": resolved_notification.comment_body_md,
        "actor_user_id": resolved_runtime.ctx.actor_id,
    }
    for user_id in sorted(set(resolved_notification.mentioned_user_ids)):
        _notify(
            service,
            recipient_user_id=user_id,
            kind=NotificationKind.COMMENT_MENTION,
            payload=payload,
        )


def _notification_runtime(
    owner: str,
    runtime: TaskNotificationRuntime | Session,
    ctx: WorkspaceContext | None,
    *,
    clock: Clock | None,
    bus: EventBus | None,
    sink: TaskNotificationSink | None,
) -> TaskNotificationRuntime:
    if isinstance(runtime, TaskNotificationRuntime):
        if ctx is not None or clock is not None or bus is not None or sink is not None:
            raise TypeError(f"{owner} received runtime plus legacy keywords")
        return runtime
    if ctx is None or clock is None or bus is None:
        raise TypeError(f"{owner} legacy call requires ctx, clock, and bus")
    return TaskNotificationRuntime(runtime, ctx, clock, bus, sink)


def _overdue_notification(
    notification: TaskOverdueNotification | None,
    *,
    task: TaskNotificationSubject | None,
    overdue_since: datetime | None,
    slipped_minutes: int | None,
    recipient_user_ids: Sequence[str],
) -> TaskOverdueNotification:
    if notification is not None:
        if (
            task is not None
            or overdue_since is not None
            or slipped_minutes is not None
            or recipient_user_ids
        ):
            raise TypeError(
                "notify_task_overdue received notification plus legacy keywords"
            )
        return notification
    if task is None or overdue_since is None or slipped_minutes is None:
        raise TypeError("notify_task_overdue requires overdue notification details")
    return TaskOverdueNotification(
        task=task,
        overdue_since=overdue_since,
        slipped_minutes=slipped_minutes,
        recipient_user_ids=recipient_user_ids,
    )


def _comment_mention_notification(
    notification: CommentMentionNotification | None,
    *,
    task_id: str | None,
    task_title: str | None,
    comment_id: str | None,
    comment_body_md: str | None,
    mentioned_user_ids: Iterable[str] | None,
) -> CommentMentionNotification:
    if notification is not None:
        if (
            task_id is not None
            or task_title is not None
            or comment_id is not None
            or comment_body_md is not None
            or mentioned_user_ids is not None
        ):
            raise TypeError(
                "notify_comment_mentions received notification plus legacy keywords"
            )
        return notification
    if (
        task_id is None
        or task_title is None
        or comment_id is None
        or comment_body_md is None
        or mentioned_user_ids is None
    ):
        raise TypeError("notify_comment_mentions requires mention notification details")
    return CommentMentionNotification(
        task_id=task_id,
        task_title=task_title,
        comment_id=comment_id,
        comment_body_md=comment_body_md,
        mentioned_user_ids=mentioned_user_ids,
    )


def _sink_for(runtime: TaskNotificationRuntime) -> TaskNotificationSink | None:
    if runtime.sink is not None:
        return runtime.sink
    return _default_sink(
        runtime.session,
        runtime.ctx,
        clock=runtime.clock,
        bus=runtime.bus,
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
