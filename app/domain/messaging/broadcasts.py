"""Manager-authored workspace broadcast messages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy.orm import Session

from app.adapters.notifications.ports import NotificationKind, NotificationSink
from app.audit import write_audit
from app.domain.errors import Conflict, Validation
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "BROADCAST_TOOL_NAME",
    "BroadcastApprovalDraft",
    "BroadcastApprovalOutcome",
    "BroadcastApprovalQueue",
    "BroadcastAudience",
    "BroadcastRecipient",
    "BroadcastSendOutcome",
    "broadcast_tool_input",
    "execute_broadcast",
    "list_broadcast_recipients",
    "send_or_queue_broadcast",
]


BROADCAST_TOOL_NAME = "messaging.broadcast"
_MAX_SUBJECT_LEN = 160
_MAX_BODY_LEN = 20_000
_MAX_RECIPIENTS = 500

type BroadcastTarget = Literal["all_staff", "selected"]


@dataclass(frozen=True, slots=True)
class BroadcastRecipient:
    user_id: str
    display_name: str
    email: str | None


@dataclass(frozen=True, slots=True)
class BroadcastApprovalDraft:
    broadcast_id: str
    recipient_count: int
    tool_call_id: str
    tool_input: dict[str, object]
    card_summary: str
    card_risk: Literal["medium"]
    card_fields: dict[str, object]
    pre_approval_source: str


@dataclass(frozen=True, slots=True)
class BroadcastApprovalOutcome:
    approval_request_id: str
    expires_at_iso: str | None


@dataclass(frozen=True, slots=True)
class BroadcastSendOutcome:
    status: Literal["sent", "pending_approval"]
    recipient_count: int
    notification_ids: tuple[str, ...]
    approval_request_id: str | None
    expires_at_iso: str | None


class BroadcastAudience(Protocol):
    """Read seam for broadcast recipients and idempotency checks."""

    def list_recipients(self, ctx: WorkspaceContext) -> Sequence[BroadcastRecipient]:
        """Return current-workspace staff/users eligible for broadcasts."""
        ...

    def existing_notification_ids_by_recipient(
        self,
        ctx: WorkspaceContext,
        *,
        broadcast_id: str,
    ) -> Mapping[str, str]:
        """Return already-created notification ids keyed by recipient id."""
        ...


class BroadcastApprovalQueue(Protocol):
    """Write seam for multi-recipient broadcast approval requests."""

    def queue_broadcast_approval(
        self,
        ctx: WorkspaceContext,
        *,
        draft: BroadcastApprovalDraft,
        clock: Clock,
    ) -> BroadcastApprovalOutcome:
        """Persist a replayable approval request for ``draft``."""
        ...


def list_broadcast_recipients(
    audience: BroadcastAudience,
    ctx: WorkspaceContext,
) -> tuple[BroadcastRecipient, ...]:
    """Return live current-workspace staff/users eligible for broadcasts."""
    return tuple(audience.list_recipients(ctx))


def send_or_queue_broadcast(
    session: Session,
    ctx: WorkspaceContext,
    *,
    audience: BroadcastAudience,
    target: BroadcastTarget,
    selected_recipient_user_ids: Sequence[str],
    confirmed_recipient_count: int,
    subject: str,
    body_md: str,
    notification_sink: NotificationSink,
    approval_queue: BroadcastApprovalQueue | None = None,
    clock: Clock | None = None,
) -> BroadcastSendOutcome:
    """Send a single-recipient broadcast or queue multi-recipient approval."""
    eff_clock = clock if clock is not None else SystemClock()
    clean_subject, clean_body = _validate_content(subject=subject, body_md=body_md)
    recipients = _resolve_recipients(
        audience,
        ctx,
        target=target,
        selected_recipient_user_ids=selected_recipient_user_ids,
    )
    if confirmed_recipient_count != len(recipients):
        raise Conflict(
            "confirmed_recipient_count does not match the resolved recipient count",
            extra={
                "error": "recipient_count_mismatch",
                "confirmed_recipient_count": confirmed_recipient_count,
                "resolved_recipient_count": len(recipients),
            },
        )

    broadcast_id = new_ulid(clock=eff_clock)
    if len(recipients) == 1:
        ids = execute_broadcast(
            session,
            ctx,
            audience=audience,
            subject=clean_subject,
            body_md=clean_body,
            recipient_user_ids=(recipients[0].user_id,),
            notification_sink=notification_sink,
            broadcast_id=broadcast_id,
            clock=eff_clock,
        )
        return BroadcastSendOutcome(
            status="sent",
            recipient_count=1,
            notification_ids=ids,
            approval_request_id=None,
            expires_at_iso=None,
        )

    if approval_queue is None:
        raise Validation(
            "approval queue is required for multi-recipient broadcasts",
            extra={"error": "approval_queue_required"},
        )
    approval = approval_queue.queue_broadcast_approval(
        ctx,
        draft=_approval_draft(
            ctx,
            subject=clean_subject,
            body_md=clean_body,
            recipient_user_ids=tuple(r.user_id for r in recipients),
            broadcast_id=broadcast_id,
            clock=eff_clock,
        ),
        clock=eff_clock,
    )
    return BroadcastSendOutcome(
        status="pending_approval",
        recipient_count=len(recipients),
        notification_ids=(),
        approval_request_id=approval.approval_request_id,
        expires_at_iso=approval.expires_at_iso,
    )


def execute_broadcast(
    session: Session,
    ctx: WorkspaceContext,
    *,
    audience: BroadcastAudience,
    subject: str,
    body_md: str,
    recipient_user_ids: Sequence[str],
    notification_sink: NotificationSink,
    broadcast_id: str | None = None,
    clock: Clock | None = None,
) -> tuple[str, ...]:
    """Create one auditable notification row per broadcast recipient."""
    eff_clock = clock if clock is not None else SystemClock()
    clean_subject, clean_body = _validate_content(subject=subject, body_md=body_md)
    if not recipient_user_ids:
        raise Validation(
            "broadcast requires at least one recipient",
            extra={"error": "no_recipients"},
        )
    recipients = _validate_recipient_scope(audience, ctx, recipient_user_ids)
    resolved_broadcast_id = broadcast_id or new_ulid(clock=eff_clock)
    existing_by_recipient = audience.existing_notification_ids_by_recipient(
        ctx,
        broadcast_id=resolved_broadcast_id,
    )
    notification_ids: list[str] = []
    created_notification_ids: list[str] = []
    for user_id in recipients:
        existing_id = existing_by_recipient.get(user_id)
        if existing_id is not None:
            notification_ids.append(existing_id)
            continue
        notification_id = notification_sink.notify(
            recipient_user_id=user_id,
            kind=NotificationKind.AGENT_MESSAGE,
            payload={
                "broadcast_id": resolved_broadcast_id,
                "broadcast_subject": clean_subject,
                "sender_user_id": ctx.actor_id,
                "preview": clean_subject,
                "message_body": clean_body,
                "deep_link": f"/w/{ctx.workspace_slug}/notifications",
            },
        )
        notification_ids.append(notification_id)
        created_notification_ids.append(notification_id)
    if created_notification_ids:
        write_audit(
            session,
            ctx,
            entity_kind="messaging_broadcast",
            entity_id=resolved_broadcast_id,
            action="messaging.broadcast.sent",
            diff={
                "recipient_count": len(created_notification_ids),
                "notification_ids": created_notification_ids,
                "subject_length": len(clean_subject),
            },
            via="api",
            clock=eff_clock,
        )
    return tuple(notification_ids)


def _validate_content(*, subject: str, body_md: str) -> tuple[str, str]:
    clean_subject = subject.strip()
    clean_body = body_md.strip()
    if not clean_subject:
        raise Validation("subject is required", extra={"error": "subject_required"})
    if len(clean_subject) > _MAX_SUBJECT_LEN:
        raise Validation(
            f"subject must be at most {_MAX_SUBJECT_LEN} characters",
            extra={"error": "subject_too_long"},
        )
    if not clean_body:
        raise Validation("body_md is required", extra={"error": "body_required"})
    if len(clean_body) > _MAX_BODY_LEN:
        raise Validation(
            f"body_md must be at most {_MAX_BODY_LEN} characters",
            extra={"error": "body_too_long"},
        )
    return clean_subject, clean_body


def _resolve_recipients(
    audience: BroadcastAudience,
    ctx: WorkspaceContext,
    *,
    target: BroadcastTarget,
    selected_recipient_user_ids: Sequence[str],
) -> tuple[BroadcastRecipient, ...]:
    available = list_broadcast_recipients(audience, ctx)
    by_id = {recipient.user_id: recipient for recipient in available}
    if target == "all_staff":
        recipients = available
    else:
        selected = tuple(dict.fromkeys(selected_recipient_user_ids))
        if not selected:
            raise Validation(
                "selected broadcasts require at least one recipient",
                extra={"error": "no_recipients"},
            )
        missing = [user_id for user_id in selected if user_id not in by_id]
        if missing:
            raise Validation(
                "selected_recipient_user_ids must belong to current workspace staff",
                extra={"error": "recipient_not_in_workspace"},
            )
        recipients = tuple(by_id[user_id] for user_id in selected)
    if not recipients:
        raise Validation(
            "broadcast requires at least one recipient",
            extra={"error": "no_recipients"},
        )
    if len(recipients) > _MAX_RECIPIENTS:
        raise Validation(
            f"broadcasts are capped at {_MAX_RECIPIENTS} recipients",
            extra={"error": "too_many_recipients"},
        )
    return recipients


def _validate_recipient_scope(
    audience: BroadcastAudience,
    ctx: WorkspaceContext,
    recipient_user_ids: Sequence[str],
) -> tuple[str, ...]:
    recipients = tuple(dict.fromkeys(recipient_user_ids))
    if len(recipients) > _MAX_RECIPIENTS:
        raise Validation(
            f"broadcasts are capped at {_MAX_RECIPIENTS} recipients",
            extra={"error": "too_many_recipients"},
        )
    available_ids = {
        recipient.user_id for recipient in list_broadcast_recipients(audience, ctx)
    }
    missing = [user_id for user_id in recipients if user_id not in available_ids]
    if missing:
        raise Validation(
            "recipient_user_ids must belong to current workspace staff",
            extra={"error": "recipient_not_in_workspace"},
        )
    return recipients


def _approval_draft(
    ctx: WorkspaceContext,
    *,
    subject: str,
    body_md: str,
    recipient_user_ids: Sequence[str],
    broadcast_id: str,
    clock: Clock,
) -> BroadcastApprovalDraft:
    recipient_count = len(recipient_user_ids)
    return BroadcastApprovalDraft(
        broadcast_id=broadcast_id,
        recipient_count=recipient_count,
        tool_call_id=new_ulid(clock=clock),
        tool_input={
            "workspace_slug": ctx.workspace_slug,
            "broadcast_id": broadcast_id,
            "subject": subject,
            "body_md": body_md,
            "recipient_user_ids": list(recipient_user_ids),
            "confirmed_recipient_count": recipient_count,
        },
        card_summary=f"Broadcast {subject!r} to {recipient_count} recipients?",
        card_risk="medium",
        card_fields={
            "recipient_count": recipient_count,
            "subject": subject,
        },
        pre_approval_source="workspace_configurable",
    )


def broadcast_tool_input(
    payload: Mapping[str, object],
) -> tuple[str, str, tuple[str, ...], str] | None:
    subject = payload.get("subject")
    body_md = payload.get("body_md")
    broadcast_id = payload.get("broadcast_id")
    raw_recipients = payload.get("recipient_user_ids")
    if (
        not isinstance(subject, str)
        or not isinstance(body_md, str)
        or not isinstance(broadcast_id, str)
        or not isinstance(raw_recipients, list)
    ):
        return None
    recipient_user_ids: list[str] = []
    for item in raw_recipients:
        if not isinstance(item, str) or not item:
            return None
        recipient_user_ids.append(item)
    return subject, body_md, tuple(recipient_user_ids), broadcast_id
