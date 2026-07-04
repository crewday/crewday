"""API-layer wiring for manager broadcast messaging."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import or_, select
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
from app.adapters.db.workspace.models import UserWorkRole, WorkEngagement, WorkRole
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
    BroadcastAudienceGroup,
    BroadcastAudienceGroupKind,
    BroadcastRecipient,
    audience_token_for_everyone,
    audience_token_for_user,
    audience_token_for_work_role,
    audience_token_for_workspace_role,
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
        owners_admins, managers, employees = self._workspace_audience_user_ids(ctx)
        user_ids = sorted(set(owners_admins).union(managers, employees))
        if not user_ids:
            return ()
        return self._recipients_for_user_ids(user_ids)

    def list_groups(self, ctx: WorkspaceContext) -> tuple[BroadcastAudienceGroup, ...]:
        """Return virtual groups backed by live current-workspace recipients."""
        owners_admins, managers, employees = self._workspace_audience_user_ids(ctx)
        people = self._recipients_for_user_ids(
            sorted(set(owners_admins + managers + employees))
        )
        available_ids = {person.user_id for person in people}
        recipient_order = {person.user_id: index for index, person in enumerate(people)}
        groups = [
            self._group(
                token=audience_token_for_everyone(),
                label="Everyone",
                kind="everyone",
                user_ids=tuple(available_ids),
                available_ids=available_ids,
                recipient_order=recipient_order,
            ),
            self._group(
                token=audience_token_for_workspace_role("owners_admins"),
                label="Owners and admins",
                kind="workspace_role",
                user_ids=owners_admins,
                available_ids=available_ids,
                recipient_order=recipient_order,
            ),
            self._group(
                token=audience_token_for_workspace_role("managers"),
                label="Managers",
                kind="workspace_role",
                user_ids=managers,
                available_ids=available_ids,
                recipient_order=recipient_order,
            ),
            self._group(
                token=audience_token_for_workspace_role("employees"),
                label="Employees",
                kind="workspace_role",
                user_ids=employees,
                available_ids=available_ids,
                recipient_order=recipient_order,
            ),
        ]
        groups.extend(
            self._work_role_groups(
                ctx,
                available_ids=available_ids,
                recipient_order=recipient_order,
            )
        )
        return tuple(groups)

    def _workspace_audience_user_ids(
        self, ctx: WorkspaceContext
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        today = datetime.now(UTC).date()
        # justification: role_grant, work_engagement and permission_group* are
        # each filtered by explicit workspace_id == ctx.workspace_id predicates.
        with tenant_agnostic():
            manager_user_ids = self._session.scalars(
                select(RoleGrant.user_id).where(
                    RoleGrant.workspace_id == ctx.workspace_id,
                    RoleGrant.scope_kind == "workspace",
                    RoleGrant.grant_role == "manager",
                    RoleGrant.revoked_at.is_(None),
                    or_(RoleGrant.started_on.is_(None), RoleGrant.started_on <= today),
                    or_(RoleGrant.ended_on.is_(None), RoleGrant.ended_on >= today),
                )
            ).all()
            engaged_user_ids = self._session.scalars(
                select(WorkEngagement.user_id).where(
                    WorkEngagement.workspace_id == ctx.workspace_id,
                    WorkEngagement.started_on <= today,
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
        return (
            tuple(dict.fromkeys(owner_user_ids)),
            tuple(dict.fromkeys(manager_user_ids)),
            tuple(dict.fromkeys(engaged_user_ids)),
        )

    def _recipients_for_user_ids(
        self, user_ids: Sequence[str]
    ) -> tuple[BroadcastRecipient, ...]:
        ids = tuple(dict.fromkeys(user_ids))
        if not ids:
            return ()
        # justification: user is identity-scoped (no workspace_id column);
        # keyed by the already-authorised recipient user ids.
        with tenant_agnostic():
            users = self._session.scalars(
                select(User)
                .where(User.id.in_(ids), User.archived_at.is_(None))
                .order_by(User.display_name.asc(), User.id.asc())
            ).all()
        return tuple(
            BroadcastRecipient(
                user_id=row.id,
                token=audience_token_for_user(row.id),
                display_name=row.display_name,
                email=row.email,
            )
            for row in users
        )

    def _work_role_groups(
        self,
        ctx: WorkspaceContext,
        *,
        available_ids: set[str],
        recipient_order: dict[str, int],
    ) -> tuple[BroadcastAudienceGroup, ...]:
        today = datetime.now(UTC).date()
        # justification: work_role, user_work_role and work_engagement are each
        # filtered by explicit workspace_id == ctx.workspace_id predicates.
        with tenant_agnostic():
            rows = self._session.execute(
                select(WorkRole.id, WorkRole.name, UserWorkRole.user_id)
                .join(UserWorkRole, UserWorkRole.work_role_id == WorkRole.id)
                .join(
                    WorkEngagement,
                    (WorkEngagement.workspace_id == UserWorkRole.workspace_id)
                    & (WorkEngagement.user_id == UserWorkRole.user_id),
                )
                .join(User, User.id == UserWorkRole.user_id)
                .where(
                    WorkRole.workspace_id == ctx.workspace_id,
                    WorkRole.deleted_at.is_(None),
                    UserWorkRole.workspace_id == ctx.workspace_id,
                    UserWorkRole.deleted_at.is_(None),
                    UserWorkRole.started_on <= today,
                    or_(
                        UserWorkRole.ended_on.is_(None), UserWorkRole.ended_on >= today
                    ),
                    WorkEngagement.workspace_id == ctx.workspace_id,
                    WorkEngagement.started_on <= today,
                    WorkEngagement.archived_on.is_(None),
                    User.archived_at.is_(None),
                )
                .order_by(WorkRole.name.asc(), WorkRole.id.asc())
            ).all()
        grouped: dict[str, tuple[str, list[str]]] = {}
        for role_id, role_name, user_id in rows:
            if user_id not in available_ids:
                continue
            _, user_ids = grouped.setdefault(role_id, (role_name, []))
            if user_id not in user_ids:
                user_ids.append(user_id)
        return tuple(
            self._group(
                token=audience_token_for_work_role(role_id),
                label=label,
                kind="work_role",
                user_ids=tuple(user_ids),
                available_ids=available_ids,
                recipient_order=recipient_order,
            )
            for role_id, (label, user_ids) in grouped.items()
            if user_ids
        )

    @staticmethod
    def _group(
        *,
        token: str,
        label: str,
        kind: BroadcastAudienceGroupKind,
        user_ids: Sequence[str],
        available_ids: set[str],
        recipient_order: dict[str, int],
    ) -> BroadcastAudienceGroup:
        resolved = tuple(
            sorted(
                {user_id for user_id in user_ids if user_id in available_ids},
                key=lambda user_id: recipient_order[user_id],
            )
        )
        return BroadcastAudienceGroup(
            token=token,
            label=label,
            kind=kind,
            resolved_recipient_count=len(resolved),
            recipient_user_ids=resolved,
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
