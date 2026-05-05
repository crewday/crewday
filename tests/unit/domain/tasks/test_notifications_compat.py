"""Compatibility tests for task notification helper signatures."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.adapters.notifications.ports import NotificationKind
from app.domain.tasks.notifications import (
    notify_comment_mentions,
    notify_task_assigned,
    notify_task_overdue,
)
from app.events.bus import EventBus
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


class _FakeSession:
    pass


class _FakeSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, NotificationKind, dict[str, object]]] = []

    def notify(
        self,
        *,
        recipient_user_id: str,
        kind: NotificationKind,
        payload: Mapping[str, object],
    ) -> str:
        self.calls.append((recipient_user_id, kind, dict(payload)))
        return f"notification-{len(self.calls)}"


@dataclass(frozen=True, slots=True)
class _Task:
    id: str
    title: str | None
    starts_at: datetime
    ends_at: datetime
    assignee_user_id: str | None


def _ctx() -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id="ws-1",
        workspace_slug="ws",
        actor_id="actor-1",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="corr-1",
    )


def _task(*, assignee_user_id: str | None = "assignee-1") -> _Task:
    return _Task(
        id="task-1",
        title="Pool clean",
        starts_at=_PINNED,
        ends_at=_PINNED + timedelta(minutes=30),
        assignee_user_id=assignee_user_id,
    )


def test_notify_task_assigned_accepts_legacy_keywords() -> None:
    sink = _FakeSink()

    notify_task_assigned(
        _FakeSession(),
        _ctx(),
        task=_task(),
        recipient_user_id="user-2",
        clock=FrozenClock(_PINNED),
        bus=EventBus(),
        sink=sink,
    )

    assert sink.calls == [
        (
            "user-2",
            NotificationKind.TASK_ASSIGNED,
            {
                "task_id": "task-1",
                "task_title": "Pool clean",
                "starts_at": _PINNED.isoformat(),
                "ends_at": (_PINNED + timedelta(minutes=30)).isoformat(),
            },
        )
    ]


def test_notify_task_overdue_accepts_legacy_keywords() -> None:
    sink = _FakeSink()

    notify_task_overdue(
        _FakeSession(),
        _ctx(),
        task=_task(),
        overdue_since=_PINNED,
        slipped_minutes=17,
        clock=FrozenClock(_PINNED),
        bus=EventBus(),
        recipient_user_ids=("owner-1", "assignee-1"),
        sink=sink,
    )

    assert [(recipient, kind) for recipient, kind, _payload in sink.calls] == [
        ("assignee-1", NotificationKind.TASK_OVERDUE),
        ("owner-1", NotificationKind.TASK_OVERDUE),
    ]
    assert sink.calls[0][2]["overdue_since"] == _PINNED.isoformat()
    assert sink.calls[0][2]["slipped_minutes"] == 17


def test_notify_comment_mentions_accepts_legacy_keywords() -> None:
    sink = _FakeSink()

    notify_comment_mentions(
        _FakeSession(),
        _ctx(),
        task_id="task-1",
        task_title="Pool clean",
        comment_id="comment-1",
        comment_body_md="@Maya please check",
        mentioned_user_ids=("user-2", "user-1", "user-2"),
        clock=FrozenClock(_PINNED),
        bus=EventBus(),
        sink=sink,
    )

    assert [(recipient, kind) for recipient, kind, _payload in sink.calls] == [
        ("user-1", NotificationKind.COMMENT_MENTION),
        ("user-2", NotificationKind.COMMENT_MENTION),
    ]
    assert sink.calls[0][2] == {
        "task_id": "task-1",
        "task_title": "Pool clean",
        "comment_id": "comment-1",
        "comment_body_md": "@Maya please check",
        "actor_user_id": "actor-1",
    }
