"""Issue notification fanout helpers."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Protocol

from app.domain.issues.service import IssueView
from app.domain.messaging.notifications import NotificationKind

__all__ = [
    "IssueNotificationSink",
    "notify_issue_reported",
    "notify_issue_resolved",
]

_log = logging.getLogger(__name__)


class IssueNotificationSink(Protocol):
    def notify(
        self,
        *,
        recipient_user_id: str,
        kind: NotificationKind,
        payload: Mapping[str, object],
    ) -> str: ...


def notify_issue_reported(
    *,
    issue: IssueView,
    recipient_user_ids: Sequence[str],
    sink: IssueNotificationSink,
) -> None:
    payload = _issue_payload(issue)
    for user_id in recipient_user_ids:
        _notify(
            sink,
            recipient_user_id=user_id,
            kind=NotificationKind.ISSUE_REPORTED,
            payload=payload,
        )


def notify_issue_resolved(
    *,
    issue: IssueView,
    sink: IssueNotificationSink,
) -> None:
    _notify(
        sink,
        recipient_user_id=issue.reported_by_user_id,
        kind=NotificationKind.ISSUE_RESOLVED,
        payload=_issue_payload(issue),
    )


def _issue_payload(issue: IssueView) -> dict[str, object | None]:
    return {
        "issue_id": issue.id,
        "reporter_user_id": issue.reported_by_user_id,
        "property_id": issue.property_id,
        "area_id": issue.area_id,
        "area": issue.area,
        "task_id": issue.task_id,
        "title": issue.title,
        "severity": issue.severity,
        "category": issue.category,
        "state": issue.state,
        "resolution_note": issue.resolution_note,
        "resolved_at": issue.resolved_at.isoformat() if issue.resolved_at else None,
        "resolved_by": issue.resolved_by,
        "reported_at": issue.reported_at.isoformat(),
    }


def _notify(
    sink: IssueNotificationSink,
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
            "issue notification fanout failed",
            extra={
                "event": "issues.notification.failed",
                "kind": kind.value,
                "recipient_user_id": recipient_user_id,
            },
        )
