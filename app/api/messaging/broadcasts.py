"""API-layer wiring for manager broadcast messaging."""

from __future__ import annotations

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
from app.adapters.notifications.ports import NotificationKind
from app.audit import write_audit
from app.domain.agent.notifications import (
    ApprovalNotificationSink,
    approval_notification_view_from_row,
    notify_approval_needed,
)
from app.domain.agent.runtime import APPROVAL_REQUEST_TTL
from app.domain.messaging.broadcasts import (
    BROADCAST_TOOL_NAME,
    BroadcastApprovalDraft,
    BroadcastApprovalOutcome,
    BroadcastRecipient,
)
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock
from app.util.ulid import new_ulid

__all__ = ["SqlAlchemyBroadcastGateway"]


class SqlAlchemyBroadcastGateway:
    """SQLAlchemy-backed broadcast recipient, idempotency, and approval seam."""

    def __init__(
        self,
        session: Session,
        *,
        approval_notification_sink: ApprovalNotificationSink | None = None,
    ) -> None:
        self._session = session
        self._approval_notification_sink = approval_notification_sink

    def list_recipients(self, ctx: WorkspaceContext) -> tuple[BroadcastRecipient, ...]:
        """Return live current-workspace staff/users eligible for broadcasts."""
        with tenant_agnostic():
            role_user_ids = self._session.scalars(
                select(RoleGrant.user_id).where(
                    RoleGrant.workspace_id == ctx.workspace_id,
                    RoleGrant.scope_kind == "workspace",
                    RoleGrant.grant_role.in_(("manager", "worker")),
                    RoleGrant.revoked_at.is_(None),
                )
            ).all()
            engaged_user_ids = self._session.scalars(
                select(WorkEngagement.user_id).where(
                    WorkEngagement.workspace_id == ctx.workspace_id,
                    WorkEngagement.archived_on.is_(None),
                )
            ).all()
            owner_user_ids = self._session.scalars(
                select(PermissionGroupMember.user_id)
                .join(
                    PermissionGroup,
                    PermissionGroup.id == PermissionGroupMember.group_id,
                )
                .where(
                    PermissionGroup.workspace_id == ctx.workspace_id,
                    PermissionGroupMember.workspace_id == ctx.workspace_id,
                    PermissionGroup.slug == "owners",
                )
            ).all()
            user_ids = sorted(
                set(role_user_ids).union(engaged_user_ids, owner_user_ids)
            )
            if not user_ids:
                return ()
            users = self._session.scalars(
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

    def existing_notification_ids_by_recipient(
        self,
        ctx: WorkspaceContext,
        *,
        broadcast_id: str,
    ) -> dict[str, str]:
        rows = self._session.scalars(
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

    def queue_broadcast_approval(
        self,
        ctx: WorkspaceContext,
        *,
        draft: BroadcastApprovalDraft,
        clock: Clock,
    ) -> BroadcastApprovalOutcome:
        # code-health: ignore[nloc] Approval adapter maps one replayable row.
        now = clock.now()
        row = ApprovalRequest(
            id=new_ulid(clock=clock),
            workspace_id=ctx.workspace_id,
            requester_actor_id=ctx.actor_id,
            action_json={
                "tool_name": BROADCAST_TOOL_NAME,
                "tool_call_id": draft.tool_call_id,
                "tool_input": dict(draft.tool_input),
                "card_summary": draft.card_summary,
                "card_risk": draft.card_risk,
                "card_fields": dict(draft.card_fields),
                "pre_approval_source": draft.pre_approval_source,
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
        self._session.add(row)
        self._session.flush()
        write_audit(
            self._session,
            ctx,
            entity_kind="approval_request",
            entity_id=row.id,
            action="approval.requested",
            diff={
                "approval_request_id": row.id,
                "action_key": "messaging.broadcast",
                "recipient_count": draft.recipient_count,
                "broadcast_id": draft.broadcast_id,
            },
            via="api",
            clock=clock,
        )
        if self._approval_notification_sink is not None:
            notify_approval_needed(
                approval=approval_notification_view_from_row(row),
                recipient_user_ids=list_owner_manager_user_ids(
                    self._session,
                    workspace_id=ctx.workspace_id,
                ),
                sink=self._approval_notification_sink,
            )
        return BroadcastApprovalOutcome(
            approval_request_id=row.id,
            expires_at_iso=row.expires_at.isoformat() if row.expires_at else None,
        )
