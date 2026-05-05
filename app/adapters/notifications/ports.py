"""Ports shared by notification-producing domains."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Protocol

__all__ = ["NotificationKind", "NotificationSink"]


class NotificationKind(enum.StrEnum):
    TASK_ASSIGNED = "task_assigned"
    TASK_OVERDUE = "task_overdue"
    EXPENSE_APPROVED = "expense_approved"
    EXPENSE_REJECTED = "expense_rejected"
    EXPENSE_SUBMITTED = "expense_submitted"
    APPROVAL_NEEDED = "approval_needed"
    APPROVAL_DECIDED = "approval_decided"
    ISSUE_REPORTED = "issue_reported"
    ISSUE_RESOLVED = "issue_resolved"
    COMMENT_MENTION = "comment_mention"
    PAYSLIP_ISSUED = "payslip_issued"
    STAY_UPCOMING = "stay_upcoming"
    ANOMALY_DETECTED = "anomaly_detected"
    AGENT_MESSAGE = "agent_message"
    DAILY_DIGEST = "daily_digest"
    PRIVACY_EXPORT_READY = "privacy_export_ready"
    WEBHOOK_AUTO_PAUSED = "webhook_auto_paused"


class NotificationSink(Protocol):
    def notify(
        self,
        *,
        recipient_user_id: str,
        kind: NotificationKind,
        payload: Mapping[str, object],
    ) -> str: ...
