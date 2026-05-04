"""SA-backed repositories implementing :mod:`app.domain.messaging.ports`.

The concrete class here adapts SQLAlchemy ``Session`` work to the
Protocol surface :mod:`app.domain.messaging.push_tokens` consumes
(cd-74pb):

* :class:`SqlAlchemyPushTokenRepository` — wraps the ``push_token``
  table and the per-workspace VAPID setting on
  ``workspace.settings_json``.

Reaches into both :mod:`app.adapters.db.messaging.models` (for
``push_token`` rows) and :mod:`app.adapters.db.workspace.models` (for
the ``Workspace.settings_json`` lookup that backs
:func:`~app.domain.messaging.push_tokens.get_vapid_public_key`).
Adapter-to-adapter imports are allowed by the import-linter — only
``app.domain → app.adapters`` is forbidden.

The repo carries an open ``Session`` and never commits or flushes
beyond what the underlying statements require — the caller's UoW
owns the transaction boundary (§01 "Key runtime invariants" #3).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.db.messaging.models import (
    ChatChannel,
    ChatChannelBinding,
    ChatChannelMember,
    ChatGatewayBinding,
    ChatLinkChallenge,
    ChatMessage,
    EmailDelivery,
    NotificationPushQueue,
    PushToken,
)
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.domain.messaging.ports import (
    ChatChannelBindingRepository,
    ChatChannelBindingRow,
    ChatChannelRepository,
    ChatChannelRow,
    ChatGatewayBindingRow,
    ChatGatewayRepository,
    ChatLinkChallengeRow,
    ChatMessageRepository,
    ChatMessageRow,
    EmailDeliveryRepository,
    EmailDeliveryRow,
    PushDeliveryRepository,
    PushDeliveryRow,
    PushTokenRepository,
    PushTokenRow,
)
from app.tenancy import tenant_agnostic
from app.util.clock import aware_utc as _as_utc

_EMAIL_DELIVERY_PROVIDER_STATE_RANK: dict[str, int] = {
    "queued": 0,
    "sent": 1,
    "delivered": 2,
    "failed": 2,
    "bounced": 3,
    "complaint": 4,
}

__all__ = [
    "SqlAlchemyChatChannelBindingRepository",
    "SqlAlchemyChatChannelRepository",
    "SqlAlchemyChatGatewayRepository",
    "SqlAlchemyChatMessageRepository",
    "SqlAlchemyEmailDeliveryRepository",
    "SqlAlchemyPushDeliveryRepository",
    "SqlAlchemyPushTokenRepository",
]


def _to_row(row: PushToken) -> PushTokenRow:
    """Project an ORM ``PushToken`` into the seam-level row.

    Field-by-field copy — :class:`PushTokenRow` is frozen so the
    domain never mutates the ORM-managed instance through a shared
    reference.
    """
    return PushTokenRow(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        endpoint=row.endpoint,
        p256dh=row.p256dh,
        auth=row.auth,
        user_agent=row.user_agent,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )


def _to_channel_row(row: ChatChannel) -> ChatChannelRow:
    return ChatChannelRow(
        id=row.id,
        workspace_id=row.workspace_id,
        kind=row.kind,
        source=row.source,
        external_ref=row.external_ref,
        title=row.title,
        created_at=_as_utc(row.created_at),
        archived_at=_as_utc(row.archived_at) if row.archived_at is not None else None,
    )


def _to_message_row(row: ChatMessage) -> ChatMessageRow:
    return ChatMessageRow(
        id=row.id,
        workspace_id=row.workspace_id,
        channel_id=row.channel_id,
        author_user_id=row.author_user_id,
        author_label=row.author_label,
        body_md=row.body_md,
        attachments_json=[
            {"blob_hash": str(item["blob_hash"])}
            for item in row.attachments_json
            if isinstance(item, dict) and "blob_hash" in item
        ],
        dispatched_to_agent_at=(
            _as_utc(row.dispatched_to_agent_at)
            if row.dispatched_to_agent_at is not None
            else None
        ),
        created_at=_as_utc(row.created_at),
    )


def _display_name_for_user(row: User) -> str:
    return row.display_name or row.email


def _to_channel_binding_row(
    row: ChatChannelBinding, user: User
) -> ChatChannelBindingRow:
    return ChatChannelBindingRow(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        user_display_name=_display_name_for_user(user),
        channel_kind=row.channel_kind,
        address=row.address,
        address_hash=row.address_hash,
        display_label=row.display_label,
        state=row.state,
        created_at=_as_utc(row.created_at),
        verified_at=_as_utc(row.verified_at) if row.verified_at is not None else None,
        revoked_at=_as_utc(row.revoked_at) if row.revoked_at is not None else None,
        revoke_reason=row.revoke_reason,
        last_message_at=(
            _as_utc(row.last_message_at) if row.last_message_at is not None else None
        ),
    )


def _to_link_challenge_row(row: ChatLinkChallenge) -> ChatLinkChallengeRow:
    return ChatLinkChallengeRow(
        id=row.id,
        binding_id=row.binding_id,
        code_hash=row.code_hash,
        code_hash_params=row.code_hash_params,
        attempts=row.attempts,
        expires_at=_as_utc(row.expires_at),
        consumed_at=_as_utc(row.consumed_at) if row.consumed_at is not None else None,
    )


def _to_gateway_binding_row(row: ChatGatewayBinding) -> ChatGatewayBindingRow:
    return ChatGatewayBindingRow(
        id=row.id,
        workspace_id=row.workspace_id,
        provider=row.provider,
        external_contact=row.external_contact,
        channel_id=row.channel_id,
        display_label=row.display_label,
        provider_metadata_json=dict(row.provider_metadata_json),
        created_at=_as_utc(row.created_at),
        last_message_at=(
            _as_utc(row.last_message_at) if row.last_message_at is not None else None
        ),
    )


class SqlAlchemyChatChannelRepository(ChatChannelRepository):
    """SA-backed concretion of :class:`ChatChannelRepository`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def insert(
        self,
        *,
        channel_id: str,
        workspace_id: str,
        kind: str,
        source: str,
        external_ref: str | None,
        title: str | None,
        created_at: datetime,
    ) -> ChatChannelRow:
        row = ChatChannel(
            id=channel_id,
            workspace_id=workspace_id,
            kind=kind,
            source=source,
            external_ref=external_ref,
            title=title,
            created_at=created_at,
            archived_at=None,
        )
        self._session.add(row)
        self._session.flush()
        return _to_channel_row(row)

    def list(
        self,
        *,
        workspace_id: str,
        kinds: Sequence[str],
        include_archived: bool,
        after_id: str | None,
        limit: int,
    ) -> Sequence[ChatChannelRow]:
        stmt = (
            select(ChatChannel)
            .where(
                ChatChannel.workspace_id == workspace_id,
                ChatChannel.kind.in_(tuple(kinds)),
            )
            .order_by(ChatChannel.id.asc())
            .limit(limit)
        )
        if after_id is not None:
            stmt = stmt.where(ChatChannel.id > after_id)
        if not include_archived:
            stmt = stmt.where(ChatChannel.archived_at.is_(None))
        rows = self._session.scalars(stmt).all()
        return [_to_channel_row(row) for row in rows]

    def get(
        self,
        *,
        workspace_id: str,
        channel_id: str,
    ) -> ChatChannelRow | None:
        row = self._session.scalars(
            select(ChatChannel).where(
                ChatChannel.workspace_id == workspace_id,
                ChatChannel.id == channel_id,
            )
        ).one_or_none()
        return _to_channel_row(row) if row is not None else None

    def rename(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        title: str | None,
    ) -> ChatChannelRow:
        row = self._load(workspace_id=workspace_id, channel_id=channel_id)
        row.title = title
        self._session.flush()
        return _to_channel_row(row)

    def archive(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        archived_at: datetime,
    ) -> ChatChannelRow:
        row = self._load(workspace_id=workspace_id, channel_id=channel_id)
        if row.archived_at is None:
            row.archived_at = archived_at
            self._session.flush()
        return _to_channel_row(row)

    def add_member(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        user_id: str,
        added_at: datetime,
    ) -> None:
        existing = self._session.get(ChatChannelMember, (channel_id, user_id))
        if existing is not None:
            return
        self._session.add(
            ChatChannelMember(
                channel_id=channel_id,
                user_id=user_id,
                workspace_id=workspace_id,
                added_at=added_at,
            )
        )
        self._session.flush()

    def is_workspace_member(self, *, workspace_id: str, user_id: str) -> bool:
        return (
            self._session.scalars(
                select(UserWorkspace.user_id)
                .where(
                    UserWorkspace.workspace_id == workspace_id,
                    UserWorkspace.user_id == user_id,
                )
                .limit(1)
            ).first()
            is not None
        )

    def remove_member(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        user_id: str,
    ) -> None:
        row = self._session.get(ChatChannelMember, (channel_id, user_id))
        if row is None or row.workspace_id != workspace_id:
            return
        self._session.delete(row)
        self._session.flush()

    def _load(self, *, workspace_id: str, channel_id: str) -> ChatChannel:
        return self._session.scalars(
            select(ChatChannel).where(
                ChatChannel.workspace_id == workspace_id,
                ChatChannel.id == channel_id,
            )
        ).one()


class SqlAlchemyChatMessageRepository(ChatMessageRepository):
    """SA-backed concretion of :class:`ChatMessageRepository`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def display_label_for_user(self, *, workspace_id: str, user_id: str) -> str:
        row = self._session.scalars(
            select(User)
            .join(UserWorkspace, UserWorkspace.user_id == User.id)
            .where(
                User.id == user_id,
                UserWorkspace.workspace_id == workspace_id,
            )
        ).one()
        return row.display_name or row.email

    def insert(
        self,
        *,
        message_id: str,
        workspace_id: str,
        channel_id: str,
        author_user_id: str | None,
        author_label: str,
        body_md: str,
        attachments_json: list[dict[str, str]],
        created_at: datetime,
    ) -> ChatMessageRow:
        row = ChatMessage(
            id=message_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            author_user_id=author_user_id,
            author_label=author_label,
            body_md=body_md,
            attachments_json=attachments_json,
            dispatched_to_agent_at=None,
            created_at=created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _to_message_row(row)

    def list_for_channel(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        before_created_at: datetime | None,
        before_id: str | None,
        limit: int,
    ) -> Sequence[ChatMessageRow]:
        stmt = (
            select(ChatMessage)
            .where(
                ChatMessage.workspace_id == workspace_id,
                ChatMessage.channel_id == channel_id,
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(limit)
        )
        if before_created_at is not None and before_id is not None:
            stmt = stmt.where(
                or_(
                    ChatMessage.created_at < before_created_at,
                    (
                        (ChatMessage.created_at == before_created_at)
                        & (ChatMessage.id < before_id)
                    ),
                )
            )
        rows = self._session.scalars(stmt).all()
        return [_to_message_row(row) for row in rows]


class SqlAlchemyChatChannelBindingRepository(ChatChannelBindingRepository):
    """SA-backed concretion for §23 user channel bindings."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def list_bindings(
        self,
        *,
        workspace_id: str,
        user_id: str | None,
        include_revoked: bool,
    ) -> Sequence[ChatChannelBindingRow]:
        stmt = (
            select(ChatChannelBinding, User)
            .join(User, User.id == ChatChannelBinding.user_id)
            .where(ChatChannelBinding.workspace_id == workspace_id)
            .order_by(
                ChatChannelBinding.created_at.desc(), ChatChannelBinding.id.desc()
            )
        )
        if user_id is not None:
            stmt = stmt.where(ChatChannelBinding.user_id == user_id)
        if not include_revoked:
            stmt = stmt.where(ChatChannelBinding.state != "revoked")
        rows = self._session.execute(stmt).all()
        return [_to_channel_binding_row(binding, user) for binding, user in rows]

    def get_binding(
        self,
        *,
        workspace_id: str,
        binding_id: str,
    ) -> ChatChannelBindingRow | None:
        row = self._session.execute(
            select(ChatChannelBinding, User)
            .join(User, User.id == ChatChannelBinding.user_id)
            .where(
                ChatChannelBinding.workspace_id == workspace_id,
                ChatChannelBinding.id == binding_id,
            )
        ).one_or_none()
        if row is None:
            return None
        binding, user = row
        return _to_channel_binding_row(binding, user)

    def user_exists(self, *, workspace_id: str, user_id: str) -> bool:
        return (
            self._session.scalars(
                select(UserWorkspace.user_id)
                .where(
                    UserWorkspace.workspace_id == workspace_id,
                    UserWorkspace.user_id == user_id,
                )
                .limit(1)
            ).first()
            is not None
        )

    def insert_pending_binding(
        self,
        *,
        binding_id: str,
        workspace_id: str,
        user_id: str,
        channel_kind: str,
        address: str,
        address_hash: str,
        display_label: str,
        created_at: datetime,
    ) -> ChatChannelBindingRow:
        row = ChatChannelBinding(
            id=binding_id,
            workspace_id=workspace_id,
            user_id=user_id,
            channel_kind=channel_kind,
            address=address,
            address_hash=address_hash,
            display_label=display_label,
            state="pending",
            created_at=created_at,
            verified_at=None,
            revoked_at=None,
            revoke_reason=None,
            last_message_at=None,
            provider_metadata_json={},
        )
        self._session.add(row)
        self._session.flush()
        user = self._session.get(User, user_id)
        if user is None:
            raise LookupError(f"user {user_id!r} not found")
        return _to_channel_binding_row(row, user)

    def insert_challenge(
        self,
        *,
        challenge_id: str,
        binding_id: str,
        code_hash: str,
        code_hash_params: str,
        sent_via: str,
        expires_at: datetime,
        created_at: datetime,
    ) -> None:
        self._session.add(
            ChatLinkChallenge(
                id=challenge_id,
                binding_id=binding_id,
                code_hash=code_hash,
                code_hash_params=code_hash_params,
                sent_via=sent_via,
                attempts=0,
                expires_at=expires_at,
                consumed_at=None,
                created_at=created_at,
            )
        )
        self._session.flush()

    def latest_open_challenge(
        self,
        *,
        binding_id: str,
    ) -> ChatLinkChallengeRow | None:
        row = self._session.scalars(
            select(ChatLinkChallenge)
            .where(
                ChatLinkChallenge.binding_id == binding_id,
                ChatLinkChallenge.consumed_at.is_(None),
            )
            .order_by(ChatLinkChallenge.created_at.desc(), ChatLinkChallenge.id.desc())
            .limit(1)
        ).one_or_none()
        return _to_link_challenge_row(row) if row is not None else None

    def increment_challenge_attempts(self, *, challenge_id: str) -> None:
        row = self._session.get(ChatLinkChallenge, challenge_id)
        if row is None:
            raise LookupError(f"chat_link_challenge {challenge_id!r} not found")
        row.attempts += 1
        self._session.flush()

    def verify_binding(
        self,
        *,
        binding_id: str,
        challenge_id: str,
        verified_at: datetime,
    ) -> ChatChannelBindingRow:
        binding = self._load_binding(binding_id)
        challenge = self._session.get(ChatLinkChallenge, challenge_id)
        if challenge is None:
            raise LookupError(f"chat_link_challenge {challenge_id!r} not found")
        binding.state = "active"
        binding.verified_at = verified_at
        binding.revoked_at = None
        binding.revoke_reason = None
        challenge.consumed_at = verified_at
        self._session.flush()
        user = self._session.get(User, binding.user_id)
        if user is None:
            raise LookupError(f"user {binding.user_id!r} not found")
        return _to_channel_binding_row(binding, user)

    def revoke_binding(
        self,
        *,
        binding_id: str,
        revoked_at: datetime,
        reason: str,
    ) -> ChatChannelBindingRow:
        binding = self._load_binding(binding_id)
        binding.state = "revoked"
        binding.revoked_at = revoked_at
        binding.revoke_reason = reason
        self._session.flush()
        user = self._session.get(User, binding.user_id)
        if user is None:
            raise LookupError(f"user {binding.user_id!r} not found")
        return _to_channel_binding_row(binding, user)

    def _load_binding(self, binding_id: str) -> ChatChannelBinding:
        row = self._session.get(ChatChannelBinding, binding_id)
        if row is None:
            raise LookupError(f"chat_channel_binding {binding_id!r} not found")
        return row


class SqlAlchemyChatGatewayRepository(ChatGatewayRepository):
    """SA-backed concretion for inbound chat gateway persistence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def find_binding(
        self, *, provider: str, external_contact: str
    ) -> ChatGatewayBindingRow | None:
        with tenant_agnostic():
            # justification: provider webhooks are bare-host ingress; the
            # binding row itself carries the workspace selected by config.
            row = self._session.scalars(
                select(ChatGatewayBinding).where(
                    ChatGatewayBinding.provider == provider,
                    ChatGatewayBinding.external_contact == external_contact,
                )
            ).one_or_none()
        return _to_gateway_binding_row(row) if row is not None else None

    def insert_binding_with_channel(
        self,
        *,
        binding_id: str,
        channel_id: str,
        workspace_id: str,
        provider: str,
        external_contact: str,
        channel_source: str,
        display_label: str,
        provider_metadata_json: dict[str, object],
        created_at: datetime,
    ) -> ChatGatewayBindingRow:
        channel = ChatChannel(
            id=channel_id,
            workspace_id=workspace_id,
            kind="chat_gateway",
            source=channel_source,
            external_ref=f"{provider}:{external_contact}",
            title=display_label,
            created_at=created_at,
            archived_at=None,
        )
        binding = ChatGatewayBinding(
            id=binding_id,
            workspace_id=workspace_id,
            provider=provider,
            external_contact=external_contact,
            channel_id=channel_id,
            display_label=display_label,
            provider_metadata_json=dict(provider_metadata_json),
            created_at=created_at,
            last_message_at=None,
        )
        self._session.add_all([channel, binding])
        self._session.flush()
        return _to_gateway_binding_row(binding)

    def touch_binding(
        self, *, binding_id: str, last_message_at: datetime
    ) -> ChatGatewayBindingRow:
        with tenant_agnostic():
            # justification: provider webhooks resolve tenant from the binding,
            # not an authenticated workspace route.
            row = self._session.get(ChatGatewayBinding, binding_id)
            if row is None:
                raise LookupError(f"chat_gateway_binding {binding_id!r} not found")
            row.last_message_at = last_message_at
            self._session.flush()
        return _to_gateway_binding_row(row)

    def find_message_by_provider_id(
        self, *, source: str, provider_message_id: str
    ) -> ChatMessageRow | None:
        with tenant_agnostic():
            # justification: replay defeat must work before a tenant context is
            # bound; source/provider_message_id is globally unique.
            row = self._session.scalars(
                select(ChatMessage).where(
                    ChatMessage.source == source,
                    ChatMessage.provider_message_id == provider_message_id,
                )
            ).one_or_none()
        return _to_message_row(row) if row is not None else None

    def insert_inbound_message(
        self,
        *,
        message_id: str,
        workspace_id: str,
        channel_id: str,
        gateway_binding_id: str,
        source: str,
        provider_message_id: str,
        author_label: str,
        body_md: str,
        created_at: datetime,
    ) -> ChatMessageRow:
        row = ChatMessage(
            id=message_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            author_user_id=None,
            author_label=author_label,
            body_md=body_md,
            attachments_json=[],
            source=source,
            provider_message_id=provider_message_id,
            gateway_binding_id=gateway_binding_id,
            dispatched_to_agent_at=None,
            created_at=created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _to_message_row(row)


class SqlAlchemyPushTokenRepository(PushTokenRepository):
    """SA-backed concretion of :class:`PushTokenRepository`.

    Wraps an open :class:`~sqlalchemy.orm.Session` and never commits
    or flushes outside what the underlying statements require — the
    caller's UoW owns the transaction boundary (§01 "Key runtime
    invariants" #3).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # -- Reads -----------------------------------------------------------

    def find_by_user_endpoint(
        self, *, workspace_id: str, user_id: str, endpoint: str
    ) -> PushTokenRow | None:
        row = self._session.scalars(
            select(PushToken).where(
                PushToken.workspace_id == workspace_id,
                PushToken.user_id == user_id,
                PushToken.endpoint == endpoint,
            )
        ).one_or_none()
        return _to_row(row) if row is not None else None

    def list_for_user(
        self, *, workspace_id: str, user_id: str
    ) -> Sequence[PushTokenRow]:
        rows = self._session.scalars(
            select(PushToken)
            .where(
                PushToken.workspace_id == workspace_id,
                PushToken.user_id == user_id,
            )
            .order_by(PushToken.created_at.asc(), PushToken.id.asc())
        ).all()
        return [_to_row(row) for row in rows]

    def get_workspace_vapid_public_key(
        self, *, workspace_id: str, settings_key: str
    ) -> str | None:
        # ``settings_json`` is a flat dict — see
        # :class:`~app.adapters.db.workspace.models.Workspace` docstring.
        # We collapse "row missing", "settings not a dict", "key absent"
        # and "value not a non-empty string" into a single ``None``
        # return because they're operationally identical for the
        # caller (operator must provision the keypair). The defensive
        # ``isinstance`` mirrors the recovery-helper pattern in
        # ``app/auth/recovery.py``.
        payload = self._session.scalars(
            select(Workspace.settings_json).where(Workspace.id == workspace_id)
        ).one_or_none()
        if payload is None or not isinstance(payload, dict):
            return None
        value = payload.get(settings_key)
        if not isinstance(value, str) or not value:
            return None
        return value

    # -- Writes ----------------------------------------------------------

    def insert(
        self,
        *,
        token_id: str,
        workspace_id: str,
        user_id: str,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: str | None,
        created_at: datetime,
    ) -> PushTokenRow:
        row = PushToken(
            id=token_id,
            workspace_id=workspace_id,
            user_id=user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=user_agent,
            created_at=created_at,
            last_used_at=None,
        )
        self._session.add(row)
        self._session.flush()
        return _to_row(row)

    def update_keys(
        self,
        *,
        workspace_id: str,
        user_id: str,
        endpoint: str,
        p256dh: str | None = None,
        auth: str | None = None,
        user_agent: str | None = None,
    ) -> PushTokenRow:
        # Pre-existing service contract: caller has just confirmed
        # the row exists via :meth:`find_by_user_endpoint`. Use the
        # same SELECT shape so the caller's UoW reuses the identity-
        # map entry rather than spawning a second instance for the
        # same primary key.
        row = self._session.scalars(
            select(PushToken).where(
                PushToken.workspace_id == workspace_id,
                PushToken.user_id == user_id,
                PushToken.endpoint == endpoint,
            )
        ).one()

        # Mirror the prior service-layer change-detection so a benign
        # refresh (browser re-running its service worker against the
        # same row, with identical keys + UA) never marks the row
        # dirty. Keeps SQLAlchemy from issuing an UPDATE — which in
        # turn keeps the caller's "no audit row on benign refresh"
        # invariant intact.
        changed = False
        if p256dh is not None and row.p256dh != p256dh:
            row.p256dh = p256dh
            changed = True
        if auth is not None and row.auth != auth:
            row.auth = auth
            changed = True
        # ``user_agent`` follows the existing service rule of "only
        # refresh when the caller actually provided one" — a curl
        # caller passes ``None`` and we keep the prior snapshot.
        if user_agent is not None and row.user_agent != user_agent:
            row.user_agent = user_agent
            changed = True
        if changed:
            self._session.flush()
        return _to_row(row)

    def delete(self, *, workspace_id: str, user_id: str, endpoint: str) -> None:
        row = self._session.scalars(
            select(PushToken).where(
                PushToken.workspace_id == workspace_id,
                PushToken.user_id == user_id,
                PushToken.endpoint == endpoint,
            )
        ).one_or_none()
        if row is None:
            # Idempotent: deleting a missing row is a no-op. The
            # caller's audit row still records the intent on a
            # successful prior find.
            return
        self._session.delete(row)
        self._session.flush()


def _to_delivery_row(row: NotificationPushQueue) -> PushDeliveryRow:
    """Project a queue ORM row into the seam-level value object."""
    return PushDeliveryRow(
        id=row.id,
        workspace_id=row.workspace_id,
        notification_id=row.notification_id,
        push_token_id=row.push_token_id,
        kind=row.kind,
        body=row.body,
        payload_json=dict(row.payload_json),
        status=row.status,
        attempt=row.attempt,
        next_attempt_at=(
            _as_utc(row.next_attempt_at) if row.next_attempt_at is not None else None
        ),
        last_status_code=row.last_status_code,
        last_error=row.last_error,
        last_attempted_at=(
            _as_utc(row.last_attempted_at)
            if row.last_attempted_at is not None
            else None
        ),
        sent_at=_as_utc(row.sent_at) if row.sent_at is not None else None,
        dead_lettered_at=(
            _as_utc(row.dead_lettered_at) if row.dead_lettered_at is not None else None
        ),
        created_at=_as_utc(row.created_at),
    )


class SqlAlchemyPushDeliveryRepository(PushDeliveryRepository):
    """SA-backed concretion of :class:`PushDeliveryRepository` (cd-y60x)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def enqueue(
        self,
        *,
        delivery_id: str,
        workspace_id: str,
        notification_id: str,
        push_token_id: str,
        kind: str,
        body: str,
        payload_json: dict[str, object],
        created_at: datetime,
        next_attempt_at: datetime,
    ) -> PushDeliveryRow:
        row = NotificationPushQueue(
            id=delivery_id,
            workspace_id=workspace_id,
            notification_id=notification_id,
            push_token_id=push_token_id,
            kind=kind,
            body=body,
            payload_json=dict(payload_json),
            status="pending",
            attempt=0,
            next_attempt_at=next_attempt_at,
            last_status_code=None,
            last_error=None,
            last_attempted_at=None,
            sent_at=None,
            dead_lettered_at=None,
            created_at=created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _to_delivery_row(row)

    def select_due(self, *, now: datetime, limit: int) -> Sequence[PushDeliveryRow]:
        with tenant_agnostic():
            # justification: cd-y60x worker is deployment-scope; each
            # row carries its own ``workspace_id`` for downstream audit.
            stmt = (
                select(NotificationPushQueue)
                .where(NotificationPushQueue.status == "pending")
                .where(NotificationPushQueue.next_attempt_at <= now)
                .order_by(NotificationPushQueue.next_attempt_at.asc())
                .limit(limit)
            )
            rows = self._session.scalars(stmt).all()
        return [_to_delivery_row(row) for row in rows]

    def claim(
        self,
        *,
        delivery_id: str,
        expected_attempt: int,
        now: datetime,
        in_flight_until: datetime,
    ) -> bool:
        with tenant_agnostic():
            stmt = (
                update(NotificationPushQueue)
                .where(NotificationPushQueue.id == delivery_id)
                .where(NotificationPushQueue.status == "pending")
                .where(NotificationPushQueue.attempt == expected_attempt)
                .values(
                    status="in_flight",
                    last_attempted_at=now,
                    # Push the visibility window forward so a crashed
                    # worker is recovered by the next tick after the
                    # in-flight grace expires; a peer worker that runs
                    # in the same tick window already lost the CAS via
                    # the ``status='pending'`` predicate above.
                    next_attempt_at=in_flight_until,
                )
            )
            result = self._session.execute(stmt)
            self._session.flush()
        # ``Session.execute`` on an ``UPDATE`` statement returns a
        # :class:`~sqlalchemy.engine.CursorResult` whose ``rowcount``
        # is the number of matched + updated rows. ``mypy --strict``
        # narrows the return type to ``Result[Any]`` (no ``rowcount``
        # accessor) — the explicit ``int`` cast keeps the seam typed
        # without sprinkling :class:`Any` through the worker tick.
        rowcount = int(getattr(result, "rowcount", 0))
        return rowcount == 1

    def mark_sent(
        self,
        *,
        delivery_id: str,
        attempt: int,
        now: datetime,
        last_status_code: int | None,
    ) -> PushDeliveryRow:
        row = self._load(delivery_id)
        row.status = "sent"
        row.attempt = attempt
        row.last_status_code = last_status_code
        row.last_error = None
        row.last_attempted_at = now
        row.sent_at = now
        row.next_attempt_at = None
        self._session.flush()
        return _to_delivery_row(row)

    def mark_transient(
        self,
        *,
        delivery_id: str,
        attempt: int,
        next_attempt_at: datetime,
        now: datetime,
        last_status_code: int | None,
        last_error: str,
    ) -> PushDeliveryRow:
        row = self._load(delivery_id)
        row.status = "pending"
        row.attempt = attempt
        row.last_status_code = last_status_code
        row.last_error = last_error
        row.last_attempted_at = now
        row.next_attempt_at = next_attempt_at
        self._session.flush()
        return _to_delivery_row(row)

    def mark_dead_lettered(
        self,
        *,
        delivery_id: str,
        attempt: int,
        now: datetime,
        last_status_code: int | None,
        last_error: str,
    ) -> PushDeliveryRow:
        row = self._load(delivery_id)
        row.status = "dead_lettered"
        row.attempt = attempt
        row.last_status_code = last_status_code
        row.last_error = last_error
        row.last_attempted_at = now
        row.dead_lettered_at = now
        row.next_attempt_at = None
        self._session.flush()
        return _to_delivery_row(row)

    def get(self, *, delivery_id: str) -> PushDeliveryRow | None:
        with tenant_agnostic():
            row = self._session.scalars(
                select(NotificationPushQueue).where(
                    NotificationPushQueue.id == delivery_id
                )
            ).one_or_none()
        return _to_delivery_row(row) if row is not None else None

    def get_token(self, *, push_token_id: str) -> PushTokenRow | None:
        with tenant_agnostic():
            row = self._session.scalars(
                select(PushToken).where(PushToken.id == push_token_id)
            ).one_or_none()
        return _to_row(row) if row is not None else None

    def delete_token(self, *, push_token_id: str) -> str | None:
        with tenant_agnostic():
            row = self._session.scalars(
                select(PushToken).where(PushToken.id == push_token_id)
            ).one_or_none()
            if row is None:
                return None
            user_id = row.user_id
            self._session.delete(row)
            self._session.flush()
        return user_id

    def get_workspace_setting(
        self, *, workspace_id: str, settings_key: str
    ) -> str | None:
        with tenant_agnostic():
            payload = self._session.scalars(
                select(Workspace.settings_json).where(Workspace.id == workspace_id)
            ).one_or_none()
        if payload is None or not isinstance(payload, dict):
            return None
        value = payload.get(settings_key)
        if not isinstance(value, str) or not value:
            return None
        return value

    def touch_token_last_used(self, *, push_token_id: str, now: datetime) -> None:
        with tenant_agnostic():
            row = self._session.scalars(
                select(PushToken).where(PushToken.id == push_token_id)
            ).one_or_none()
            if row is None:
                return
            row.last_used_at = now
            self._session.flush()

    def _load(self, delivery_id: str) -> NotificationPushQueue:
        with tenant_agnostic():
            return self._session.scalars(
                select(NotificationPushQueue).where(
                    NotificationPushQueue.id == delivery_id
                )
            ).one()


def _to_email_delivery_row(row: EmailDelivery) -> EmailDeliveryRow:
    """Project an :class:`EmailDelivery` ORM row into the seam value object."""
    return EmailDeliveryRow(
        id=row.id,
        workspace_id=row.workspace_id,
        to_person_id=row.to_person_id,
        to_email_at_send=row.to_email_at_send,
        template_key=row.template_key,
        context_snapshot_json=dict(row.context_snapshot_json),
        sent_at=_as_utc(row.sent_at) if row.sent_at is not None else None,
        provider_message_id=row.provider_message_id,
        delivery_state=row.delivery_state,
        first_error=row.first_error,
        retry_count=row.retry_count,
        inbound_linkage=row.inbound_linkage,
        created_at=_as_utc(row.created_at),
    )


class SqlAlchemyEmailDeliveryRepository(EmailDeliveryRepository):
    """SA-backed concretion of :class:`EmailDeliveryRepository` (cd-8kg7).

    Wraps an open :class:`~sqlalchemy.orm.Session`. The transitions
    are deliberately small, single-row UPDATEs by primary key so the
    caller's UoW gets predictable SQL: one INSERT (queued) plus at
    most one UPDATE (sent or failed) per dispatched email. The future
    retry worker walks ``queued`` / ``failed`` rows on its own seam
    rather than threading a loop through this class.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def insert_queued(
        self,
        *,
        delivery_id: str,
        workspace_id: str,
        to_person_id: str,
        to_email_at_send: str,
        template_key: str,
        context_snapshot_json: dict[str, object],
        created_at: datetime,
    ) -> EmailDeliveryRow:
        row = EmailDelivery(
            id=delivery_id,
            workspace_id=workspace_id,
            to_person_id=to_person_id,
            to_email_at_send=to_email_at_send,
            template_key=template_key,
            context_snapshot_json=dict(context_snapshot_json),
            sent_at=None,
            provider_message_id=None,
            delivery_state="queued",
            first_error=None,
            retry_count=0,
            inbound_linkage=None,
            created_at=created_at,
        )
        self._session.add(row)
        # Flush so the row id is realised + the workspace FK is
        # validated before the (potentially blocking) ``mailer.send``
        # I/O. A FK / CHECK violation surfaces here, not after a
        # half-completed SMTP round trip.
        self._session.flush()
        return _to_email_delivery_row(row)

    def mark_sent(
        self,
        *,
        delivery_id: str,
        provider_message_id: str,
        sent_at: datetime,
    ) -> EmailDeliveryRow:
        row = self._load(delivery_id)
        row.delivery_state = "sent"
        row.provider_message_id = provider_message_id
        row.sent_at = sent_at
        self._session.flush()
        return _to_email_delivery_row(row)

    def mark_failed(
        self,
        *,
        delivery_id: str,
        error_text: str,
        now: datetime,
    ) -> EmailDeliveryRow:
        del now
        # justification: callers identify the just-inserted ledger row by id.
        with tenant_agnostic():
            stmt = (
                update(EmailDelivery)
                .where(EmailDelivery.id == delivery_id)
                .values(
                    delivery_state="failed",
                    first_error=func.coalesce(EmailDelivery.first_error, error_text),
                    retry_count=EmailDelivery.retry_count + 1,
                )
            )
            self._session.execute(stmt)
            self._session.flush()
        row = self._load(delivery_id)
        return _to_email_delivery_row(row)

    def mark_retry_sent(
        self,
        *,
        delivery_id: str,
        expected_retry_count: int,
        provider_message_id: str,
        sent_at: datetime,
    ) -> EmailDeliveryRow | None:
        # justification: deployment-scope retry worker CASes a selected row.
        with tenant_agnostic():
            stmt = (
                update(EmailDelivery)
                .where(EmailDelivery.id == delivery_id)
                .where(EmailDelivery.delivery_state.in_(("queued", "failed")))
                .where(EmailDelivery.sent_at.is_(None))
                .where(EmailDelivery.retry_count == expected_retry_count)
                .values(
                    delivery_state="sent",
                    provider_message_id=provider_message_id,
                    sent_at=sent_at,
                )
            )
            result = self._session.execute(stmt)
            self._session.flush()
        if _rowcount(result) != 1:
            return None
        return _to_email_delivery_row(self._load(delivery_id))

    def mark_retry_failed(
        self,
        *,
        delivery_id: str,
        expected_retry_count: int,
        error_text: str,
        now: datetime,
        max_attempts: int,
    ) -> EmailDeliveryRow | None:
        del now
        # justification: deployment-scope retry worker CASes a selected row.
        with tenant_agnostic():
            stmt = (
                update(EmailDelivery)
                .where(EmailDelivery.id == delivery_id)
                .where(EmailDelivery.delivery_state.in_(("queued", "failed")))
                .where(EmailDelivery.sent_at.is_(None))
                .where(EmailDelivery.retry_count == expected_retry_count)
                .where(EmailDelivery.retry_count < max_attempts)
                .values(
                    delivery_state="failed",
                    first_error=func.coalesce(EmailDelivery.first_error, error_text),
                    retry_count=EmailDelivery.retry_count + 1,
                )
            )
            result = self._session.execute(stmt)
            self._session.flush()
        if _rowcount(result) != 1:
            return None
        return _to_email_delivery_row(self._load(delivery_id))

    def select_due_for_retry(
        self,
        *,
        workspace_id: str,
        now: datetime,
        backoff_schedule_seconds: Sequence[int],
        max_attempts: int,
        limit: int,
    ) -> Sequence[EmailDeliveryRow]:
        due_windows = [EmailDelivery.retry_count <= 0]
        for retry_count in range(1, max_attempts):
            delay_index = retry_count - 1
            if delay_index >= len(backoff_schedule_seconds):
                break
            due_windows.append(
                and_(
                    EmailDelivery.retry_count == retry_count,
                    EmailDelivery.created_at
                    <= now - timedelta(seconds=backoff_schedule_seconds[delay_index]),
                )
            )

        # justification: deployment-scope retry worker pins SELECT by workspace.
        with tenant_agnostic():
            stmt = (
                select(EmailDelivery)
                .where(EmailDelivery.workspace_id == workspace_id)
                .where(EmailDelivery.delivery_state.in_(("queued", "failed")))
                .where(EmailDelivery.sent_at.is_(None))
                .where(EmailDelivery.retry_count < max_attempts)
                .where(or_(*due_windows))
                .order_by(
                    EmailDelivery.delivery_state.asc(),
                    EmailDelivery.sent_at.asc(),
                )
                .limit(limit)
            )
            rows = self._session.scalars(stmt).all()
        return [_to_email_delivery_row(row) for row in rows]

    def find_by_provider_message_id(
        self,
        *,
        workspace_id: str,
        provider_message_id: str,
    ) -> EmailDeliveryRow | None:
        # justification: deployment-scope webhook lookup pins workspace_id.
        with tenant_agnostic():
            row = self._session.scalars(
                select(EmailDelivery).where(
                    EmailDelivery.workspace_id == workspace_id,
                    EmailDelivery.provider_message_id == provider_message_id,
                )
            ).one_or_none()
        return _to_email_delivery_row(row) if row is not None else None

    def apply_provider_delivery_state(
        self,
        *,
        workspace_id: str,
        delivery_id: str,
        provider_message_id: str,
        delivery_state: str,
        error_text: str | None,
    ) -> EmailDeliveryRow | None:
        target_rank = _EMAIL_DELIVERY_PROVIDER_STATE_RANK[delivery_state]
        eligible_states = tuple(
            state
            for state, rank in _EMAIL_DELIVERY_PROVIDER_STATE_RANK.items()
            if rank < target_rank or state == delivery_state
        )
        values: dict[str, object] = {"delivery_state": delivery_state}
        if error_text is not None:
            values["first_error"] = func.coalesce(EmailDelivery.first_error, error_text)

        # justification: deployment-scope webhook update re-pins workspace + ESP id.
        with tenant_agnostic():
            result = self._session.execute(
                update(EmailDelivery)
                .where(EmailDelivery.id == delivery_id)
                .where(EmailDelivery.workspace_id == workspace_id)
                .where(EmailDelivery.provider_message_id == provider_message_id)
                .where(EmailDelivery.delivery_state.in_(eligible_states))
                .values(**values)
            )
            self._session.flush()
        if _rowcount(result) == 1:
            return _to_email_delivery_row(self._load(delivery_id))
        row = self.find_by_provider_message_id(
            workspace_id=workspace_id,
            provider_message_id=provider_message_id,
        )
        if row is not None and row.id == delivery_id:
            return row
        return None

    def _load(self, delivery_id: str) -> EmailDelivery:
        # justification: transition methods load rows already selected by id.
        with tenant_agnostic():
            return self._session.scalars(
                select(EmailDelivery).where(EmailDelivery.id == delivery_id)
            ).one()


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0))
