"""Chat-channel HTTP router — ``/chat/channels``."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from sqlalchemy.orm import Session

from app.adapters.db.messaging.repositories import SqlAlchemyChatChannelRepository
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.domain.errors import DomainError, Forbidden, Internal, NotFound, Validation
from app.domain.messaging.channels import (
    MAX_TITLE_LEN,
    ChatChannelCreate,
    ChatChannelInvalid,
    ChatChannelNotFound,
    ChatChannelPermissionDenied,
    ChatChannelService,
    ChatChannelView,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "ChatChannelCreateRequest",
    "ChatChannelListResponse",
    "ChatChannelPatchRequest",
    "ChatChannelResponse",
    "build_channels_router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]

_CHAT_CHANNEL_PATCH_SCHEMA_EXTRA: dict[str, JsonValue] = {
    "anyOf": [
        {"required": ["title"]},
        {
            "required": ["archived"],
            "properties": {"archived": {"const": True}},
        },
    ]
}


class ChatChannelResponse(BaseModel):
    id: str
    workspace_id: str
    kind: str
    source: str
    external_ref: str | None
    title: str | None
    created_at: datetime
    archived_at: datetime | None
    can_post_messages: bool

    @classmethod
    def from_view(cls, view: ChatChannelView) -> ChatChannelResponse:
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            kind=view.kind,
            source=view.source,
            external_ref=view.external_ref,
            title=view.title,
            created_at=view.created_at,
            archived_at=view.archived_at,
            can_post_messages=view.can_post_messages,
        )


class ChatChannelListResponse(BaseModel):
    data: list[ChatChannelResponse]
    next_cursor: str | None = None
    has_more: bool = False


class AppChatChannelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["staff", "manager"]
    title: str | None = Field(default=None, max_length=MAX_TITLE_LEN)
    source: Literal["app"] | None = None
    external_ref: None = None

    def to_domain(self) -> ChatChannelCreate:
        return ChatChannelCreate(
            kind=self.kind,
            title=self.title,
            source=self.source,
            external_ref=self.external_ref,
        )


class GatewayChatChannelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["chat_gateway"]
    title: str | None = Field(default=None, max_length=MAX_TITLE_LEN)
    source: Literal["whatsapp", "sms", "email"] | None = None
    external_ref: str = Field(min_length=1, max_length=256, pattern=r"\S")

    def to_domain(self) -> ChatChannelCreate:
        return ChatChannelCreate(
            kind=self.kind,
            title=self.title,
            source=self.source,
            external_ref=self.external_ref,
        )


ChatChannelCreateRequest = Annotated[
    AppChatChannelCreateRequest | GatewayChatChannelCreateRequest,
    Field(discriminator="kind"),
]


class ChatChannelPatchRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra=_CHAT_CHANNEL_PATCH_SCHEMA_EXTRA,
    )

    title: str | None = Field(default=None, max_length=MAX_TITLE_LEN)
    archived: bool | None = Field(
        default=None, json_schema_extra={"enum": [True, None]}
    )

    @model_validator(mode="after")
    def _has_mutation(self) -> ChatChannelPatchRequest:
        has_title = "title" in self.model_fields_set
        has_archive = "archived" in self.model_fields_set and self.archived is not None
        if not has_title and not has_archive:
            raise ValueError("PATCH body must include title or archived")
        return self


def _http_for_channel_error(exc: Exception) -> DomainError:
    if isinstance(exc, ChatChannelNotFound):
        return NotFound(extra={"error": "chat_channel_not_found"})
    if isinstance(exc, ChatChannelPermissionDenied):
        message = str(exc)
        return Forbidden(
            message,
            extra={"error": "permission_denied", "message": message},
        )
    if isinstance(exc, ChatChannelInvalid):
        message = str(exc)
        return Validation(
            message,
            extra={"error": "chat_channel_invalid", "message": message},
        )
    return Internal(extra={"error": "internal"})


def build_channels_router() -> APIRouter:
    router = APIRouter(prefix="/chat/channels", tags=["messaging", "chat"])

    @router.get(
        "",
        response_model=ChatChannelListResponse,
        operation_id="messaging.chat_channels.list",
        summary="List visible chat channels in the caller's workspace",
    )
    def list_channels(
        ctx: _Ctx,
        session: _Db,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
        include_archived: bool = False,
    ) -> ChatChannelListResponse:
        service = ChatChannelService(ctx)
        repo = SqlAlchemyChatChannelRepository(session)
        views = service.list(
            repo,
            include_archived=include_archived,
            after_id=decode_cursor(cursor),
            limit=limit + 1,
        )
        page = paginate(views, limit=limit, key_getter=lambda view: view.id)
        return ChatChannelListResponse(
            data=[ChatChannelResponse.from_view(view) for view in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @router.post(
        "",
        response_model=ChatChannelResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="messaging.chat_channels.create",
        summary="Create a chat channel",
    )
    def create_channel(
        body: ChatChannelCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> ChatChannelResponse:
        try:
            view = ChatChannelService(ctx).create(
                SqlAlchemyChatChannelRepository(session),
                body.to_domain(),
            )
        except (ChatChannelInvalid, ChatChannelPermissionDenied) as exc:
            raise _http_for_channel_error(exc) from exc
        return ChatChannelResponse.from_view(view)

    @router.patch(
        "/{channel_id}",
        response_model=ChatChannelResponse,
        operation_id="messaging.chat_channels.update",
        summary="Rename or archive a chat channel",
    )
    def patch_channel(
        channel_id: str,
        body: ChatChannelPatchRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> ChatChannelResponse:
        service = ChatChannelService(ctx)
        repo = SqlAlchemyChatChannelRepository(session)
        try:
            if body.archived is False:
                raise ChatChannelInvalid("unarchive is not supported")
            if "title" in body.model_fields_set:
                view = service.rename(repo, channel_id, title=body.title)
            else:
                view = service.get(repo, channel_id)
            if body.archived is True:
                view = service.archive(repo, channel_id)
        except (
            ChatChannelInvalid,
            ChatChannelNotFound,
            ChatChannelPermissionDenied,
        ) as exc:
            raise _http_for_channel_error(exc) from exc
        return ChatChannelResponse.from_view(view)

    return router
