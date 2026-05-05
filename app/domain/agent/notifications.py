"""Approval notification fanout helpers."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from app.domain.messaging.notifications import NotificationKind

__all__ = [
    "AgentMessageNotificationSink",
    "ApprovalNotificationSink",
    "approval_notification_view_from_row",
    "notify_agent_message_fallback",
    "notify_approval_decided",
    "notify_approval_needed",
]

_log = logging.getLogger(__name__)


class ApprovalNotificationSink(Protocol):
    def notify(
        self,
        *,
        recipient_user_id: str,
        kind: NotificationKind,
        payload: Mapping[str, object],
    ) -> str: ...


class AgentMessageNotificationSink(Protocol):
    def notify(
        self,
        *,
        recipient_user_id: str,
        kind: NotificationKind,
        payload: Mapping[str, object],
    ) -> str: ...


class ApprovalNotificationView(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def requester_actor_id(self) -> str | None: ...

    @property
    def for_user_id(self) -> str | None: ...

    @property
    def status(self) -> Literal["pending", "approved", "rejected", "timed_out"]: ...

    @property
    def decided_by(self) -> str | None: ...

    @property
    def decided_at(self) -> datetime | None: ...

    @property
    def decision_note_md(self) -> str | None: ...

    @property
    def expires_at(self) -> datetime | None: ...

    @property
    def created_at(self) -> datetime: ...

    @property
    def action_json(self) -> Mapping[str, Any]: ...


class ApprovalNotificationRow(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def requester_actor_id(self) -> str | None: ...

    @property
    def for_user_id(self) -> str | None: ...

    @property
    def status(self) -> str: ...

    @property
    def decided_by(self) -> str | None: ...

    @property
    def decided_at(self) -> datetime | None: ...

    @property
    def decision_note_md(self) -> str | None: ...

    @property
    def rationale_md(self) -> str | None: ...

    @property
    def expires_at(self) -> datetime | None: ...

    @property
    def created_at(self) -> datetime: ...

    @property
    def action_json(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ApprovalNotificationSnapshot:
    id: str
    requester_actor_id: str | None
    for_user_id: str | None
    status: Literal["pending", "approved", "rejected", "timed_out"]
    decided_by: str | None
    decided_at: datetime | None
    decision_note_md: str | None
    expires_at: datetime | None
    created_at: datetime
    action_json: Mapping[str, Any]


def approval_notification_view_from_row(
    row: ApprovalNotificationRow,
) -> ApprovalNotificationSnapshot:
    status: Literal["pending", "approved", "rejected", "timed_out"]
    if row.status == "pending":
        status = "pending"
    elif row.status == "approved":
        status = "approved"
    elif row.status == "rejected":
        status = "rejected"
    elif row.status == "timed_out":
        status = "timed_out"
    else:
        raise ValueError(
            f"approval row {row.id!r} carries unknown status {row.status!r}"
        )
    return ApprovalNotificationSnapshot(
        id=row.id,
        requester_actor_id=row.requester_actor_id,
        for_user_id=row.for_user_id,
        status=status,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        decision_note_md=row.decision_note_md or row.rationale_md,
        expires_at=row.expires_at,
        created_at=row.created_at,
        action_json=dict(row.action_json),
    )


def notify_approval_needed(
    *,
    approval: ApprovalNotificationView,
    recipient_user_ids: Sequence[str],
    sink: ApprovalNotificationSink,
) -> None:
    payload = _approval_payload(approval)
    for user_id in recipient_user_ids:
        _notify(
            sink,
            recipient_user_id=user_id,
            kind=NotificationKind.APPROVAL_NEEDED,
            payload=payload,
        )


def notify_approval_decided(
    *,
    approval: ApprovalNotificationView,
    sink: ApprovalNotificationSink,
) -> None:
    recipient_user_id = approval.for_user_id or approval.requester_actor_id
    if recipient_user_id is None:
        return
    _notify(
        sink,
        recipient_user_id=recipient_user_id,
        kind=NotificationKind.APPROVAL_DECIDED,
        payload=_approval_payload(approval),
    )


def notify_agent_message_fallback(
    *,
    recipient_user_id: str,
    message_body: str,
    workspace_slug: str,
    chat_thread_ref: str | None,
    message_id: str | None,
    sink: AgentMessageNotificationSink,
) -> None:
    _notify(
        sink,
        recipient_user_id=recipient_user_id,
        kind=NotificationKind.AGENT_MESSAGE,
        payload={
            "preview": _preview(message_body),
            "message_body": message_body,
            "deep_link": _agent_message_deep_link(
                workspace_slug=workspace_slug,
                message_id=message_id,
            ),
            "workspace_slug": workspace_slug,
            "chat_thread_ref": chat_thread_ref,
            "message_id": message_id,
        },
    )


def _approval_payload(approval: ApprovalNotificationView) -> dict[str, object | None]:
    return {
        "approval_request_id": approval.id,
        "requester_actor_id": approval.requester_actor_id,
        "for_user_id": approval.for_user_id,
        "status": approval.status,
        "decided_by": approval.decided_by,
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "decision_note_md": approval.decision_note_md,
        "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
        "created_at": approval.created_at.isoformat(),
        "tool_name": approval.action_json.get("tool_name"),
        "card_summary": approval.action_json.get("card_summary"),
        "card_risk": approval.action_json.get("card_risk"),
        "pre_approval_source": approval.action_json.get("pre_approval_source"),
    }


def _preview(message_body: str) -> str:
    compact = " ".join(message_body.split())
    if len(compact) <= 140:
        return compact or "new message"
    return compact[:137].rstrip() + "..."


def _agent_message_deep_link(*, workspace_slug: str, message_id: str | None) -> str:
    target = f"/w/{workspace_slug}/chat"
    if message_id:
        return f"{target}#{message_id}"
    return target


def _notify(
    sink: ApprovalNotificationSink | AgentMessageNotificationSink,
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
            "notification fanout failed",
            extra={
                "event": "notification.failed",
                "kind": kind.value,
                "recipient_user_id": recipient_user_id,
            },
        )
