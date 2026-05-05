"""Manager-authored workspace broadcast messages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import User
from app.adapters.db.llm.models import ApprovalRequest
from app.adapters.db.messaging.audiences import list_owner_manager_user_ids
from app.adapters.db.messaging.models import Notification
from app.adapters.db.workspace.models import WorkEngagement
from app.adapters.notifications.ports import NotificationSink
from app.audit import write_audit
from app.domain.agent.notifications import (
    ApprovalNotificationSink,
    approval_notification_view_from_row,
    notify_approval_needed,
)
from app.domain.agent.runtime import APPROVAL_REQUEST_TTL
from app.domain.errors import Conflict, Validation
from app.domain.messaging.notifications import NotificationKind
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "BROADCAST_TOOL_NAME",
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
class BroadcastSendOutcome:
    status: Literal["sent", "pending_approval"]
    recipient_count: int
    notification_ids: tuple[str, ...]
    approval_request_id: str | None
    expires_at_iso: str | None


def list_broadcast_recipients(
    session: Session,
    ctx: WorkspaceContext,
) -> tuple[BroadcastRecipient, ...]:
    """Return live current-workspace staff/users eligible for broadcasts."""
    with tenant_agnostic():
        role_user_ids = session.scalars(
            select(RoleGrant.user_id).where(
                RoleGrant.workspace_id == ctx.workspace_id,
                RoleGrant.scope_kind == "workspace",
                RoleGrant.grant_role.in_(("manager", "worker")),
                RoleGrant.revoked_at.is_(None),
            )
        ).all()
        engaged_user_ids = session.scalars(
            select(WorkEngagement.user_id).where(
                WorkEngagement.workspace_id == ctx.workspace_id,
                WorkEngagement.archived_on.is_(None),
            )
        ).all()
        owner_user_ids = session.scalars(
            select(PermissionGroupMember.user_id)
            .join(PermissionGroup, PermissionGroup.id == PermissionGroupMember.group_id)
            .where(
                PermissionGroup.workspace_id == ctx.workspace_id,
                PermissionGroupMember.workspace_id == ctx.workspace_id,
                PermissionGroup.slug == "owners",
            )
        ).all()
        user_ids = sorted(set(role_user_ids).union(engaged_user_ids, owner_user_ids))
        if not user_ids:
            return ()
        users = session.scalars(
            select(User)
            .where(User.id.in_(user_ids), User.archived_at.is_(None))
            .order_by(User.display_name.asc(), User.id.asc())
        ).all()
    return tuple(
        BroadcastRecipient(
            user_id=row.id,
            display_name=row.display_name,
            email=row.email,
        )
        for row in users
    )


def send_or_queue_broadcast(
    session: Session,
    ctx: WorkspaceContext,
    *,
    target: BroadcastTarget,
    selected_recipient_user_ids: Sequence[str],
    confirmed_recipient_count: int,
    subject: str,
    body_md: str,
    notification_sink: NotificationSink,
    approval_notification_sink: ApprovalNotificationSink | None = None,
    clock: Clock | None = None,
) -> BroadcastSendOutcome:
    """Send a single-recipient broadcast or queue multi-recipient approval."""
    eff_clock = clock if clock is not None else SystemClock()
    clean_subject, clean_body = _validate_content(subject=subject, body_md=body_md)
    recipients = _resolve_recipients(
        session,
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

    row = _queue_approval(
        session,
        ctx,
        subject=clean_subject,
        body_md=clean_body,
        recipient_user_ids=tuple(r.user_id for r in recipients),
        broadcast_id=broadcast_id,
        clock=eff_clock,
    )
    if approval_notification_sink is not None:
        notify_approval_needed(
            approval=approval_notification_view_from_row(row),
            recipient_user_ids=list_owner_manager_user_ids(
                session,
                workspace_id=ctx.workspace_id,
            ),
            sink=approval_notification_sink,
        )
    return BroadcastSendOutcome(
        status="pending_approval",
        recipient_count=len(recipients),
        notification_ids=(),
        approval_request_id=row.id,
        expires_at_iso=row.expires_at.isoformat() if row.expires_at else None,
    )


def execute_broadcast(
    session: Session,
    ctx: WorkspaceContext,
    *,
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
    recipients = _validate_recipient_scope(session, ctx, recipient_user_ids)
    resolved_broadcast_id = broadcast_id or new_ulid(clock=eff_clock)
    existing_by_recipient = _existing_notification_ids_by_recipient(
        session,
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
    session: Session,
    ctx: WorkspaceContext,
    *,
    target: BroadcastTarget,
    selected_recipient_user_ids: Sequence[str],
) -> tuple[BroadcastRecipient, ...]:
    available = list_broadcast_recipients(session, ctx)
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
    session: Session,
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
        recipient.user_id for recipient in list_broadcast_recipients(session, ctx)
    }
    missing = [user_id for user_id in recipients if user_id not in available_ids]
    if missing:
        raise Validation(
            "recipient_user_ids must belong to current workspace staff",
            extra={"error": "recipient_not_in_workspace"},
        )
    return recipients


def _existing_notification_ids_by_recipient(
    session: Session,
    ctx: WorkspaceContext,
    *,
    broadcast_id: str,
) -> dict[str, str]:
    rows = session.scalars(
        select(Notification).where(
            Notification.workspace_id == ctx.workspace_id,
            Notification.kind == NotificationKind.AGENT_MESSAGE.value,
        )
    ).all()
    by_recipient: dict[str, str] = {}
    for row in rows:
        if row.payload_json.get("broadcast_id") == broadcast_id:
            by_recipient.setdefault(row.recipient_user_id, row.id)
    return by_recipient


def _queue_approval(
    session: Session,
    ctx: WorkspaceContext,
    *,
    subject: str,
    body_md: str,
    recipient_user_ids: Sequence[str],
    broadcast_id: str,
    clock: Clock,
) -> ApprovalRequest:
    now = clock.now()
    row = ApprovalRequest(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        requester_actor_id=ctx.actor_id,
        action_json={
            "tool_name": BROADCAST_TOOL_NAME,
            "tool_call_id": new_ulid(clock=clock),
            "tool_input": {
                "workspace_slug": ctx.workspace_slug,
                "broadcast_id": broadcast_id,
                "subject": subject,
                "body_md": body_md,
                "recipient_user_ids": list(recipient_user_ids),
                "confirmed_recipient_count": len(recipient_user_ids),
            },
            "card_summary": (
                f"Broadcast {subject!r} to {len(recipient_user_ids)} recipients?"
            ),
            "card_risk": "medium",
            "card_fields": {
                "recipient_count": len(recipient_user_ids),
                "subject": subject,
            },
            "pre_approval_source": "workspace_configurable",
        },
        status="pending",
        decided_by=None,
        decided_at=None,
        rationale_md=None,
        decision_note_md=None,
        result_json=None,
        expires_at=now + APPROVAL_REQUEST_TTL,
        inline_channel="desk_only",
        for_user_id=None,
        resolved_user_mode=None,
        created_at=now,
    )
    session.add(row)
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="approval_request",
        entity_id=row.id,
        action="approval.requested",
        diff={
            "approval_request_id": row.id,
            "action_key": "messaging.broadcast",
            "recipient_count": len(recipient_user_ids),
            "broadcast_id": broadcast_id,
        },
        via="api",
        clock=clock,
    )
    return row


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
