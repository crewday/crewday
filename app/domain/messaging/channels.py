"""Chat-channel CRUD and authorization contract (cd-7ej8).

Message send/list endpoints land with cd-xae5. Until then this service
owns the boundary predicate future message code must call:
``assert_can_post_message`` permits workers to post to staff channels
and denies manager channels unless the caller holds
``messaging.manager_channel``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.audit import write_audit
from app.authz import PermissionDenied, require
from app.domain.messaging.ports import ChatChannelRepository, ChatChannelRow
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "CHAT_CHANNEL_KINDS",
    "CHAT_CHANNEL_SOURCES",
    "ChatChannelCreate",
    "ChatChannelInvalid",
    "ChatChannelNotFound",
    "ChatChannelPermissionDenied",
    "ChatChannelService",
    "ChatChannelView",
]


ChatChannelKind = Literal["staff", "manager", "chat_gateway"]
ChatChannelSource = Literal["app", "whatsapp", "sms", "email"]

CHAT_CHANNEL_KINDS: frozenset[str] = frozenset({"staff", "manager", "chat_gateway"})
CHAT_CHANNEL_SOURCES: frozenset[str] = frozenset({"app", "whatsapp", "sms", "email"})
MANAGER_CHANNEL_CAPABILITY = "messaging.manager_channel"
CHAT_GATEWAY_READ_CAPABILITY = "chat_gateway.read"
MAX_TITLE_LEN = 160


class ChatChannelInvalid(ValueError):
    """The requested channel mutation violates the service contract."""


class ChatChannelNotFound(LookupError):
    """The channel does not exist or is not visible to the caller."""


class ChatChannelPermissionDenied(PermissionError):
    """The caller lacks the capability for the requested operation."""


@dataclass(frozen=True, slots=True)
class ChatChannelCreate:
    """Input for :meth:`ChatChannelService.create`."""

    kind: ChatChannelKind
    title: str | None = None
    source: ChatChannelSource | None = None
    external_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ChatChannelView:
    """Domain projection returned to API and future message services."""

    id: str
    workspace_id: str
    kind: str
    source: str
    external_ref: str | None
    title: str | None
    created_at: datetime
    archived_at: datetime | None
    can_post_messages: bool


class ChatChannelService:
    """Workspace-scoped chat-channel service."""

    def __init__(self, ctx: WorkspaceContext, *, clock: Clock | None = None) -> None:
        self._ctx = ctx
        self._clock = clock if clock is not None else SystemClock()

    def create(
        self,
        repo: ChatChannelRepository,
        body: ChatChannelCreate,
    ) -> ChatChannelView:
        self._validate_create(body)
        self._require_manage_kind(repo, body.kind)
        now = self._clock.now()
        source = self._source_for(body)
        row = repo.insert(
            channel_id=new_ulid(),
            workspace_id=self._ctx.workspace_id,
            kind=body.kind,
            source=source,
            external_ref=body.external_ref,
            title=_clean_title(body.title),
            created_at=now,
        )
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="chat_channel",
            entity_id=row.id,
            action="messaging.channel.created",
            diff={
                "kind": row.kind,
                "source": row.source,
                "external_ref": row.external_ref,
                "title": row.title,
            },
            clock=self._clock,
        )
        return self._view(row, repo)

    def list(
        self,
        repo: ChatChannelRepository,
        *,
        include_archived: bool = False,
        after_id: str | None = None,
        limit: int = 51,
    ) -> list[ChatChannelView]:
        if limit < 1:
            raise ChatChannelInvalid("limit must be >= 1")
        kinds = self._visible_kinds(repo)
        if not kinds:
            return []
        rows = repo.list(
            workspace_id=self._ctx.workspace_id,
            kinds=kinds,
            include_archived=include_archived,
            after_id=after_id,
            limit=limit,
        )
        return [self._view(row, repo) for row in rows]

    def get(
        self,
        repo: ChatChannelRepository,
        channel_id: str,
        *,
        include_archived: bool = True,
    ) -> ChatChannelView:
        row = self._visible_row(repo, channel_id, include_archived=include_archived)
        return self._view(row, repo)

    def rename(
        self,
        repo: ChatChannelRepository,
        channel_id: str,
        *,
        title: str | None,
    ) -> ChatChannelView:
        row = self._visible_row(repo, channel_id, include_archived=True)
        self._require_manage_kind(repo, row.kind)
        renamed = repo.rename(
            workspace_id=self._ctx.workspace_id,
            channel_id=channel_id,
            title=_clean_title(title),
        )
        return self._view(renamed, repo)

    def archive(
        self,
        repo: ChatChannelRepository,
        channel_id: str,
    ) -> ChatChannelView:
        row = self._visible_row(repo, channel_id, include_archived=True)
        self._require_manage_kind(repo, row.kind)
        was_active = row.archived_at is None
        archived = repo.archive(
            workspace_id=self._ctx.workspace_id,
            channel_id=channel_id,
            archived_at=self._clock.now(),
        )
        if was_active:
            write_audit(
                repo.session,
                self._ctx,
                entity_kind="chat_channel",
                entity_id=archived.id,
                action="messaging.channel.archived",
                diff={"kind": archived.kind},
                clock=self._clock,
            )
        return self._view(archived, repo)

    def add_member(
        self,
        repo: ChatChannelRepository,
        channel_id: str,
        user_id: str,
    ) -> ChatChannelView:
        row = self._visible_row(repo, channel_id, include_archived=True)
        self._require_manage_kind(repo, row.kind)
        if not repo.is_workspace_member(
            workspace_id=self._ctx.workspace_id,
            user_id=user_id,
        ):
            raise ChatChannelInvalid("channel members must belong to the workspace")
        repo.add_member(
            workspace_id=self._ctx.workspace_id,
            channel_id=channel_id,
            user_id=user_id,
            added_at=self._clock.now(),
        )
        return self._view(row, repo)

    def remove_member(
        self,
        repo: ChatChannelRepository,
        channel_id: str,
        user_id: str,
    ) -> ChatChannelView:
        row = self._visible_row(repo, channel_id, include_archived=True)
        self._require_manage_kind(repo, row.kind)
        repo.remove_member(
            workspace_id=self._ctx.workspace_id,
            channel_id=channel_id,
            user_id=user_id,
        )
        return self._view(row, repo)

    def assert_can_post_message(
        self,
        repo: ChatChannelRepository,
        channel_id: str,
    ) -> None:
        row = self._visible_row(repo, channel_id, include_archived=False)
        if not self._can_post_to_kind(repo, row.kind):
            raise ChatChannelPermissionDenied(
                f"actor cannot post to {row.kind!r} channel {channel_id!r}"
            )

    def _visible_row(
        self,
        repo: ChatChannelRepository,
        channel_id: str,
        *,
        include_archived: bool,
    ) -> ChatChannelRow:
        row = repo.get(workspace_id=self._ctx.workspace_id, channel_id=channel_id)
        if row is None:
            raise ChatChannelNotFound(channel_id)
        if row.archived_at is not None and not include_archived:
            raise ChatChannelNotFound(channel_id)
        if not self._can_read_kind(repo, row.kind):
            raise ChatChannelNotFound(channel_id)
        return row

    def _view(
        self,
        row: ChatChannelRow,
        repo: ChatChannelRepository,
    ) -> ChatChannelView:
        return ChatChannelView(
            id=row.id,
            workspace_id=row.workspace_id,
            kind=row.kind,
            source=row.source,
            external_ref=row.external_ref,
            title=row.title,
            created_at=row.created_at,
            archived_at=row.archived_at,
            can_post_messages=(
                row.archived_at is None and self._can_post_to_kind(repo, row.kind)
            ),
        )

    def _visible_kinds(self, repo: ChatChannelRepository) -> tuple[str, ...]:
        kinds = ["staff"]
        if self._has_capability(repo, MANAGER_CHANNEL_CAPABILITY):
            kinds.append("manager")
        if self._has_capability(repo, CHAT_GATEWAY_READ_CAPABILITY):
            kinds.append("chat_gateway")
        return tuple(kinds)

    def _can_read_kind(self, repo: ChatChannelRepository, kind: str) -> bool:
        if kind == "staff":
            return True
        if kind == "manager":
            return self._has_capability(repo, MANAGER_CHANNEL_CAPABILITY)
        if kind == "chat_gateway":
            return self._has_capability(repo, CHAT_GATEWAY_READ_CAPABILITY)
        return False

    def _can_post_to_kind(self, repo: ChatChannelRepository, kind: str) -> bool:
        if kind == "staff":
            return True
        if kind == "manager":
            return self._has_capability(repo, MANAGER_CHANNEL_CAPABILITY)
        if kind == "chat_gateway":
            return self._has_capability(repo, CHAT_GATEWAY_READ_CAPABILITY)
        return False

    def _require_manage_kind(self, repo: ChatChannelRepository, kind: str) -> None:
        capability = (
            CHAT_GATEWAY_READ_CAPABILITY
            if kind == "chat_gateway"
            else MANAGER_CHANNEL_CAPABILITY
        )
        if not self._has_capability(repo, capability):
            raise ChatChannelPermissionDenied(f"caller lacks {capability!r}")

    def _has_capability(
        self,
        repo: ChatChannelRepository,
        action_key: str,
    ) -> bool:
        try:
            require(
                repo.session,
                self._ctx,
                action_key=action_key,
                scope_kind="workspace",
                scope_id=self._ctx.workspace_id,
            )
        except PermissionDenied:
            return False
        return True

    def _validate_create(self, body: ChatChannelCreate) -> None:
        if body.kind not in CHAT_CHANNEL_KINDS:
            raise ChatChannelInvalid(f"unknown channel kind {body.kind!r}")
        _clean_title(body.title)
        source = self._source_for(body)
        if source not in CHAT_CHANNEL_SOURCES:
            raise ChatChannelInvalid(f"unknown channel source {source!r}")
        if body.kind in {"staff", "manager"} and source != "app":
            raise ChatChannelInvalid(f"{body.kind!r} channels must use source='app'")
        if body.kind == "chat_gateway":
            if source == "app":
                raise ChatChannelInvalid(
                    "chat_gateway channels require an external source"
                )
            if body.external_ref is None or body.external_ref.strip() == "":
                raise ChatChannelInvalid("chat_gateway channels require external_ref")
        elif body.external_ref is not None:
            raise ChatChannelInvalid("in-app channels cannot set external_ref")

    def _source_for(self, body: ChatChannelCreate) -> str:
        if body.source is not None:
            return body.source
        if body.kind in {"staff", "manager"}:
            return "app"
        return "whatsapp"


def _clean_title(title: str | None) -> str | None:
    if title is None:
        return None
    cleaned = title.strip()
    if len(cleaned) > MAX_TITLE_LEN:
        raise ChatChannelInvalid(f"title must be <= {MAX_TITLE_LEN} characters")
    return cleaned or None
