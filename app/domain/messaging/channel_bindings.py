"""User chat-channel binding service for §23 off-app channels."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy.exc import IntegrityError

from app.audit import write_audit
from app.authz import PermissionDenied, require
from app.domain.messaging.ports import (
    ChatChannelBindingRepository,
    ChatChannelBindingRow,
)
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import (
    ChatChannelBindingCreated,
    ChatChannelBindingRevoked,
    ChatChannelBindingVerified,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "CHANNEL_KINDS",
    "MOCK_LINK_CODE",
    "ChatChannelBindingConflict",
    "ChatChannelBindingInvalid",
    "ChatChannelBindingNotFound",
    "ChatChannelBindingPermissionDenied",
    "ChatChannelBindingService",
    "ChatChannelKind",
    "LinkStart",
]


ChatChannelKind = Literal["offapp_whatsapp", "offapp_telegram"]
CHANNEL_KINDS: frozenset[str] = frozenset({"offapp_whatsapp", "offapp_telegram"})
MOCK_LINK_CODE = "424242"
_MAX_ATTEMPTS = 5
_CHALLENGE_TTL = timedelta(minutes=15)
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


class ChatChannelBindingInvalid(ValueError):
    """The requested binding mutation violates the service contract."""


class ChatChannelBindingNotFound(LookupError):
    """The binding does not exist or is hidden from the caller."""


class ChatChannelBindingConflict(ValueError):
    """A non-revoked binding already occupies the requested key."""


class ChatChannelBindingPermissionDenied(PermissionError):
    """The caller cannot perform the requested binding operation."""


@dataclass(frozen=True, slots=True)
class LinkStart:
    binding: ChatChannelBindingRow
    hint: str
    expires_at: datetime


class ChatChannelBindingService:
    """Workspace-scoped service for off-app chat-channel bindings."""

    def __init__(
        self,
        ctx: WorkspaceContext,
        *,
        clock: Clock | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._ctx = ctx
        self._clock = clock if clock is not None else SystemClock()
        self._event_bus = event_bus if event_bus is not None else default_event_bus

    def list(
        self,
        repo: ChatChannelBindingRepository,
        *,
        include_revoked: bool = False,
    ) -> list[ChatChannelBindingRow]:
        user_id = None if self._can_read_workspace(repo) else self._ctx.actor_id
        return list(
            repo.list_bindings(
                workspace_id=self._ctx.workspace_id,
                user_id=user_id,
                include_revoked=include_revoked,
            )
        )

    def start(
        self,
        repo: ChatChannelBindingRepository,
        *,
        user_id: str,
        channel_kind: str,
        address: str,
        display_label: str | None = None,
    ) -> LinkStart:
        self._require_self(user_id)
        if channel_kind not in CHANNEL_KINDS:
            raise ChatChannelBindingInvalid("unknown channel_kind")
        normalised = _normalise_address(channel_kind, address)
        if not repo.user_exists(workspace_id=self._ctx.workspace_id, user_id=user_id):
            raise ChatChannelBindingNotFound(user_id)
        now = self._clock.now()
        binding_id = new_ulid(clock=self._clock)
        address_hash = _address_hash(self._ctx.workspace_id, channel_kind, normalised)
        try:
            binding = repo.insert_pending_binding(
                binding_id=binding_id,
                workspace_id=self._ctx.workspace_id,
                user_id=user_id,
                channel_kind=channel_kind,
                address=normalised,
                address_hash=address_hash,
                display_label=(display_label or _default_label(channel_kind)).strip(),
                created_at=now,
            )
            expires_at = now + _CHALLENGE_TTL
            repo.insert_challenge(
                challenge_id=new_ulid(clock=self._clock),
                binding_id=binding_id,
                code_hash=_code_hash(binding_id, MOCK_LINK_CODE),
                code_hash_params="sha256:mock",
                sent_via="channel",
                expires_at=expires_at,
                created_at=now,
            )
        except IntegrityError as exc:
            raise ChatChannelBindingConflict(
                "a non-revoked binding already exists for this user or address"
            ) from exc
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="chat_channel_binding",
            entity_id=binding.id,
            action="chat_channel_binding.created",
            diff={"channel_kind": binding.channel_kind, "user_id": binding.user_id},
            clock=self._clock,
        )
        self._event_bus.publish(
            ChatChannelBindingCreated(
                workspace_id=self._ctx.workspace_id,
                actor_id=self._ctx.actor_id,
                correlation_id=self._ctx.audit_correlation_id,
                occurred_at=now,
                binding_id=binding.id,
                user_id=binding.user_id,
                channel_kind=_event_channel_kind(binding.channel_kind),
                display_label=binding.display_label,
            )
        )
        return LinkStart(
            binding=binding,
            hint="Enter the 6-digit code sent to the channel. Dev code: 424242.",
            expires_at=expires_at,
        )

    def verify(
        self,
        repo: ChatChannelBindingRepository,
        *,
        binding_id: str,
        code: str,
    ) -> ChatChannelBindingRow:
        binding = self._owned_binding(repo, binding_id)
        if binding.state != "pending":
            raise ChatChannelBindingInvalid("binding is not pending")
        challenge = repo.latest_open_challenge(binding_id=binding_id)
        now = self._clock.now()
        if challenge is None or challenge.expires_at <= now:
            raise ChatChannelBindingInvalid("link challenge expired")
        if challenge.attempts >= _MAX_ATTEMPTS:
            raise ChatChannelBindingInvalid("link challenge attempts exhausted")
        if not hmac.compare_digest(challenge.code_hash, _code_hash(binding_id, code)):
            repo.increment_challenge_attempts(challenge_id=challenge.id)
            raise ChatChannelBindingInvalid("link verification code is incorrect")
        verified = repo.verify_binding(
            binding_id=binding_id,
            challenge_id=challenge.id,
            verified_at=now,
        )
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="chat_channel_binding",
            entity_id=verified.id,
            action="chat_channel_binding.verified",
            diff={"channel_kind": verified.channel_kind, "user_id": verified.user_id},
            clock=self._clock,
        )
        self._event_bus.publish(
            ChatChannelBindingVerified(
                workspace_id=self._ctx.workspace_id,
                actor_id=self._ctx.actor_id,
                correlation_id=self._ctx.audit_correlation_id,
                occurred_at=now,
                binding_id=verified.id,
                user_id=verified.user_id,
                channel_kind=_event_channel_kind(verified.channel_kind),
                display_label=verified.display_label,
            )
        )
        return verified

    def unlink(
        self,
        repo: ChatChannelBindingRepository,
        *,
        binding_id: str,
    ) -> ChatChannelBindingRow:
        binding = self._owned_binding(repo, binding_id)
        if binding.state == "revoked":
            return binding
        revoked_at = self._clock.now()
        revoked = repo.revoke_binding(
            binding_id=binding_id,
            revoked_at=revoked_at,
            reason="user",
        )
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="chat_channel_binding",
            entity_id=revoked.id,
            action="chat_channel_binding.revoked",
            diff={
                "channel_kind": revoked.channel_kind,
                "user_id": revoked.user_id,
                "reason": "user",
            },
            clock=self._clock,
        )
        self._event_bus.publish(
            ChatChannelBindingRevoked(
                workspace_id=self._ctx.workspace_id,
                actor_id=self._ctx.actor_id,
                correlation_id=self._ctx.audit_correlation_id,
                occurred_at=revoked_at,
                binding_id=revoked.id,
                user_id=revoked.user_id,
                channel_kind=_event_channel_kind(revoked.channel_kind),
                display_label=revoked.display_label,
                reason="user",
            )
        )
        return revoked

    def _owned_binding(
        self,
        repo: ChatChannelBindingRepository,
        binding_id: str,
    ) -> ChatChannelBindingRow:
        binding = repo.get_binding(
            workspace_id=self._ctx.workspace_id,
            binding_id=binding_id,
        )
        if binding is None:
            raise ChatChannelBindingNotFound(binding_id)
        self._require_self(binding.user_id)
        return binding

    def _require_self(self, user_id: str) -> None:
        if user_id != self._ctx.actor_id:
            raise ChatChannelBindingPermissionDenied(
                "chat channel binding operations are self-service"
            )

    def _can_read_workspace(self, repo: ChatChannelBindingRepository) -> bool:
        try:
            require(
                repo.session,
                self._ctx,
                action_key="chat_gateway.read",
                scope_kind="workspace",
                scope_id=self._ctx.workspace_id,
            )
        except PermissionDenied:
            return False
        return True


def _normalise_address(channel_kind: str, address: str) -> str:
    stripped = address.strip()
    if channel_kind == "offapp_whatsapp":
        compact = re.sub(r"[\s().-]+", "", stripped)
        if not _E164_RE.match(compact):
            raise ChatChannelBindingInvalid("offapp_whatsapp address must be E.164")
        return compact
    if channel_kind == "offapp_telegram":
        handle = stripped if stripped.startswith("@") else f"@{stripped}"
        if len(handle) < 2 or len(handle) > 64:
            raise ChatChannelBindingInvalid("offapp_telegram handle is invalid")
        return handle
    raise ChatChannelBindingInvalid("unknown channel_kind")


def _default_label(channel_kind: str) -> str:
    if channel_kind == "offapp_whatsapp":
        return "WhatsApp"
    if channel_kind == "offapp_telegram":
        return "Telegram"
    return "Chat"


def _event_channel_kind(value: str) -> ChatChannelKind:
    if value == "offapp_whatsapp":
        return "offapp_whatsapp"
    if value == "offapp_telegram":
        return "offapp_telegram"
    raise ChatChannelBindingInvalid(f"unknown channel_kind {value!r}")


def _address_hash(workspace_id: str, channel_kind: str, address: str) -> str:
    return hashlib.sha256(
        f"{workspace_id}:{channel_kind}:{address}".encode()
    ).hexdigest()


def _code_hash(binding_id: str, code: str) -> str:
    return hashlib.sha256(f"{binding_id}:{code.strip()}".encode()).hexdigest()
