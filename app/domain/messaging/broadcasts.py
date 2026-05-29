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
    "BroadcastApprovalPolicy",
    "BroadcastApprovalQueue",
    "BroadcastAudience",
    "BroadcastAudienceGroup",
    "BroadcastAudienceGroupKind",
    "BroadcastAudiencePreview",
    "BroadcastRecipient",
    "BroadcastSendOutcome",
    "WorkspaceAudienceRole",
    "audience_token_for_everyone",
    "audience_token_for_user",
    "audience_token_for_work_role",
    "audience_token_for_workspace_role",
    "broadcast_tool_input",
    "execute_broadcast",
    "list_broadcast_recipients",
    "preview_broadcast_audience",
    "send_or_queue_broadcast",
]


BROADCAST_TOOL_NAME = "messaging.broadcast"
_MAX_SUBJECT_LEN = 160
_MAX_BODY_LEN = 20_000
_MAX_RECIPIENTS = 500

type BroadcastTarget = Literal["all_staff", "selected"]
type BroadcastAudienceGroupKind = Literal["everyone", "workspace_role", "work_role"]
type BroadcastApprovalPolicy = Literal["direct_send", "queue_multi_recipient"]
type WorkspaceAudienceRole = Literal["owners_admins", "managers", "employees"]

_USER_AUDIENCE_TOKEN_PREFIX = "user:"
_WORKSPACE_ROLE_AUDIENCE_TOKEN_PREFIX = "group:workspace_role:"
_WORK_ROLE_AUDIENCE_TOKEN_PREFIX = "group:work_role:"
_EVERYONE_AUDIENCE_TOKEN = "group:everyone"


def audience_token_for_user(user_id: str) -> str:
    return f"{_USER_AUDIENCE_TOKEN_PREFIX}{user_id}"


def audience_token_for_everyone() -> str:
    return _EVERYONE_AUDIENCE_TOKEN


def audience_token_for_workspace_role(role: WorkspaceAudienceRole) -> str:
    return f"{_WORKSPACE_ROLE_AUDIENCE_TOKEN_PREFIX}{role}"


def audience_token_for_work_role(work_role_id: str) -> str:
    return f"{_WORK_ROLE_AUDIENCE_TOKEN_PREFIX}{work_role_id}"


@dataclass(frozen=True, slots=True)
class BroadcastRecipient:
    user_id: str
    token: str
    display_name: str
    email: str | None


@dataclass(frozen=True, slots=True)
class BroadcastAudienceGroup:
    token: str
    label: str
    kind: BroadcastAudienceGroupKind
    resolved_recipient_count: int
    recipient_user_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BroadcastAudiencePreview:
    people: tuple[BroadcastRecipient, ...]
    groups: tuple[BroadcastAudienceGroup, ...]


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

    def list_groups(self, ctx: WorkspaceContext) -> Sequence[BroadcastAudienceGroup]:
        """Return virtual audience groups backed by current eligible recipients."""
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


def preview_broadcast_audience(
    audience: BroadcastAudience,
    ctx: WorkspaceContext,
) -> BroadcastAudiencePreview:
    """Return people plus virtual audience groups for broadcast compose."""
    return BroadcastAudiencePreview(
        people=tuple(audience.list_recipients(ctx)),
        groups=tuple(audience.list_groups(ctx)),
    )


def send_or_queue_broadcast(
    session: Session,
    ctx: WorkspaceContext,
    *,
    audience: BroadcastAudience,
    audience_tokens: Sequence[str],
    confirmed_recipient_count: int,
    subject: str,
    body_md: str,
    notification_sink: NotificationSink,
    approval_queue: BroadcastApprovalQueue | None = None,
    approval_policy: BroadcastApprovalPolicy = "queue_multi_recipient",
    clock: Clock | None = None,
) -> BroadcastSendOutcome:
    """Send a broadcast immediately or queue multi-recipient approval."""
    # code-health: ignore[nloc,params] Broadcast command keeps approval inputs explicit.
    eff_clock = clock if clock is not None else SystemClock()
    clean_subject, clean_body = _validate_content(subject=subject, body_md=body_md)
    recipients = _resolve_recipients(
        audience,
        ctx,
        audience_tokens=audience_tokens,
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
    if len(recipients) == 1 or approval_policy == "direct_send":
        ids = execute_broadcast(
            session,
            ctx,
            audience=audience,
            subject=clean_subject,
            body_md=clean_body,
            recipient_user_ids=tuple(recipient.user_id for recipient in recipients),
            notification_sink=notification_sink,
            broadcast_id=broadcast_id,
            clock=eff_clock,
        )
        return BroadcastSendOutcome(
            status="sent",
            recipient_count=len(recipients),
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
    # code-health: ignore[nloc,params] Broadcast execution keeps audit atomic.
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
    audience_tokens: Sequence[str],
) -> tuple[BroadcastRecipient, ...]:
    selected_tokens = tuple(dict.fromkeys(audience_tokens))
    if not selected_tokens:
        raise Validation(
            "broadcast requires at least one audience token",
            extra={"error": "no_recipients"},
        )
    preview = preview_broadcast_audience(audience, ctx)
    by_user_id = {recipient.user_id: recipient for recipient in preview.people}
    by_token = {recipient.token: recipient for recipient in preview.people}
    by_group_token = {group.token: group for group in preview.groups}
    missing = [
        token
        for token in selected_tokens
        if token not in by_token and token not in by_group_token
    ]
    if missing:
        raise Validation(
            "audience_tokens must reference current workspace people or groups",
            extra={"error": "audience_token_not_found", "tokens": missing},
        )
    recipient_by_id: dict[str, BroadcastRecipient] = {}
    for token in selected_tokens:
        recipient = by_token.get(token)
        if recipient is not None:
            recipient_by_id.setdefault(recipient.user_id, recipient)
            continue
        group = by_group_token[token]
        for user_id in group.recipient_user_ids:
            group_recipient = by_user_id.get(user_id)
            if group_recipient is not None:
                recipient_by_id.setdefault(user_id, group_recipient)
    recipients = tuple(recipient_by_id.values())
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
