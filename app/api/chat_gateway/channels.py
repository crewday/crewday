"""Workspace chat-channel binding routes for §23."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.messaging.models import ChatGatewayBinding
from app.adapters.db.messaging.repositories import (
    SqlAlchemyChatChannelBindingRepository,
)
from app.api.deps import current_workspace_context, db_session
from app.authz import PermissionDenied, require
from app.config import Settings, get_settings
from app.domain.errors import (
    Conflict,
    DomainError,
    Forbidden,
    Internal,
    NotFound,
    Validation,
)
from app.domain.messaging.channel_bindings import (
    MOCK_LINK_CODE,
    ChatChannelBindingConflict,
    ChatChannelBindingInvalid,
    ChatChannelBindingNotFound,
    ChatChannelBindingPermissionDenied,
    ChatChannelBindingService,
)
from app.domain.messaging.ports import ChatChannelBindingRow
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import aware_utc as _as_utc

__all__ = ["build_chat_channel_bindings_router"]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_ChannelKind = Literal["offapp_whatsapp", "offapp_telegram"]
_BindingState = Literal["pending", "active", "revoked"]
_RevokeReason = Literal[
    "user", "stop_keyword", "user_archived", "admin", "provider_error"
]
_ProviderStatus = Literal["connected", "pending", "error", "not_configured"]


class ChatChannelBindingPayload(BaseModel):
    id: str
    user_id: str
    user_display_name: str
    channel_kind: _ChannelKind
    address: str
    display_label: str
    state: _BindingState
    verified_at: datetime | None
    last_message_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: _RevokeReason | None

    @classmethod
    def from_row(cls, row: ChatChannelBindingRow) -> ChatChannelBindingPayload:
        return cls(
            id=row.id,
            user_id=row.user_id,
            user_display_name=row.user_display_name,
            channel_kind=_channel_kind(row.channel_kind),
            address=row.address,
            display_label=row.display_label,
            state=_binding_state(row.state),
            verified_at=row.verified_at,
            last_message_at=row.last_message_at,
            revoked_at=row.revoked_at,
            revoke_reason=_revoke_reason(row.revoke_reason),
        )


class ChatGatewayProviderPayload(BaseModel):
    channel_kind: _ChannelKind
    provider: str
    status: _ProviderStatus
    display_stub: str
    last_webhook_at: datetime | None
    templates: list[str]


class LinkStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_kind: _ChannelKind
    address: str = Field(min_length=1, max_length=160)
    user_id: str = Field(min_length=1, max_length=64)
    display_label: str | None = Field(default=None, max_length=80)


class LinkStartResponse(BaseModel):
    binding_id: str
    state: _BindingState
    hint: str
    expires_at: datetime


class LinkVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=32)


class BindingStateResponse(BaseModel):
    binding_id: str
    state: _BindingState


def build_chat_channel_bindings_router() -> APIRouter:
    router = APIRouter(prefix="/chat/channels", tags=["chat_gateway"])

    @router.get(
        "",
        response_model=list[ChatChannelBindingPayload],
        operation_id="chat.channel_bindings.list",
        summary="List linked off-app chat channels",
    )
    def list_bindings(ctx: _Ctx, session: _Db) -> list[ChatChannelBindingPayload]:
        rows = ChatChannelBindingService(ctx).list(
            SqlAlchemyChatChannelBindingRepository(session)
        )
        return [ChatChannelBindingPayload.from_row(row) for row in rows]

    @router.get(
        "/providers",
        response_model=list[ChatGatewayProviderPayload],
        operation_id="chat.channel_bindings.providers",
        summary="List chat gateway provider status for this workspace",
    )
    def list_providers(
        ctx: _Ctx,
        session: _Db,
        request: Request,
    ) -> list[ChatGatewayProviderPayload]:
        _require_chat_gateway_read(ctx, session)
        settings = getattr(request.app.state, "settings", None)
        cfg = settings if isinstance(settings, Settings) else get_settings()
        last = _last_webhook_by_provider(session, workspace_id=ctx.workspace_id)
        whatsapp_configured = bool(
            cfg.chat_gateway_workspace_id and cfg.chat_gateway_meta_whatsapp_secret
        )
        return [
            ChatGatewayProviderPayload(
                channel_kind="offapp_whatsapp",
                provider="meta_whatsapp",
                status="connected" if whatsapp_configured else "not_configured",
                display_stub=(
                    "Deployment default" if whatsapp_configured else "Not configured"
                ),
                last_webhook_at=last.get("meta_whatsapp"),
                templates=[
                    "chat_channel_link_code",
                    "chat_agent_nudge",
                    "chat_workspace_pick",
                ],
            ),
            ChatGatewayProviderPayload(
                channel_kind="offapp_telegram",
                provider="telegram",
                status="not_configured",
                display_stub="Not configured",
                last_webhook_at=last.get("telegram"),
                templates=[],
            ),
        ]

    @router.post(
        "/link/start",
        response_model=LinkStartResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="chat.channel_bindings.link_start",
        summary="Start an off-app chat-channel link ceremony",
    )
    def start_link(
        body: LinkStartRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> LinkStartResponse:
        try:
            result = ChatChannelBindingService(ctx).start(
                SqlAlchemyChatChannelBindingRepository(session),
                user_id=body.user_id,
                channel_kind=body.channel_kind,
                address=body.address,
                display_label=body.display_label,
            )
        except (
            ChatChannelBindingConflict,
            ChatChannelBindingInvalid,
            ChatChannelBindingNotFound,
            ChatChannelBindingPermissionDenied,
        ) as exc:
            raise _http_for_binding_error(exc) from exc
        return LinkStartResponse(
            binding_id=result.binding.id,
            state=_binding_state(result.binding.state),
            hint=result.hint,
            expires_at=result.expires_at,
        )

    @router.post(
        "/link/verify",
        response_model=BindingStateResponse,
        operation_id="chat.channel_bindings.link_verify",
        summary=(
            f"Verify an off-app chat-channel link code; dev code is {MOCK_LINK_CODE}"
        ),
    )
    def verify_link(
        body: LinkVerifyRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> BindingStateResponse:
        try:
            row = ChatChannelBindingService(ctx).verify(
                SqlAlchemyChatChannelBindingRepository(session),
                binding_id=body.binding_id,
                code=body.code,
            )
        except (
            ChatChannelBindingInvalid,
            ChatChannelBindingNotFound,
            ChatChannelBindingPermissionDenied,
        ) as exc:
            raise _http_for_binding_error(exc) from exc
        return BindingStateResponse(binding_id=row.id, state=_binding_state(row.state))

    @router.post(
        "/{binding_id}/unlink",
        response_model=BindingStateResponse,
        operation_id="chat.channel_bindings.unlink",
        summary="Unlink an off-app chat-channel binding",
        openapi_extra={"x-agent-confirm": {"message": "Unlink this chat channel?"}},
    )
    def unlink(
        binding_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> BindingStateResponse:
        try:
            row = ChatChannelBindingService(ctx).unlink(
                SqlAlchemyChatChannelBindingRepository(session),
                binding_id=binding_id,
            )
        except (ChatChannelBindingNotFound, ChatChannelBindingPermissionDenied) as exc:
            raise _http_for_binding_error(exc) from exc
        return BindingStateResponse(binding_id=row.id, state=_binding_state(row.state))

    return router


def _http_for_binding_error(exc: Exception) -> DomainError:
    if isinstance(exc, ChatChannelBindingNotFound):
        return NotFound(extra={"error": "chat_channel_binding_not_found"})
    if isinstance(exc, ChatChannelBindingPermissionDenied):
        message = str(exc)
        return Forbidden(
            message,
            extra={"error": "permission_denied", "message": message},
        )
    if isinstance(exc, ChatChannelBindingConflict):
        message = str(exc)
        return Conflict(
            message,
            extra={"error": "chat_channel_binding_conflict", "message": message},
        )
    if isinstance(exc, ChatChannelBindingInvalid):
        message = str(exc)
        return Validation(
            message,
            extra={"error": "chat_channel_binding_invalid", "message": message},
        )
    return Internal(extra={"error": "internal"})


def _require_chat_gateway_read(ctx: WorkspaceContext, session: Session) -> None:
    try:
        require(
            session,
            ctx,
            action_key="chat_gateway.read",
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
        )
    except PermissionDenied as exc:
        raise Forbidden(
            extra={"error": "permission_denied", "action_key": "chat_gateway.read"}
        ) from exc


def _last_webhook_by_provider(
    session: Session, *, workspace_id: str
) -> dict[str, datetime]:
    with tenant_agnostic():
        rows = session.execute(
            select(
                ChatGatewayBinding.provider,
                func.max(ChatGatewayBinding.last_message_at),
            )
            .where(
                ChatGatewayBinding.workspace_id == workspace_id,
                ChatGatewayBinding.last_message_at.is_not(None),
            )
            .group_by(ChatGatewayBinding.provider)
        ).all()
    return {
        provider: _as_utc(last_at)
        for provider, last_at in rows
        if isinstance(provider, str) and isinstance(last_at, datetime)
    }


def _channel_kind(value: str) -> _ChannelKind:
    if value == "offapp_whatsapp":
        return "offapp_whatsapp"
    if value == "offapp_telegram":
        return "offapp_telegram"
    raise ValueError(f"unexpected channel_kind {value!r}")


def _binding_state(value: str) -> _BindingState:
    if value == "pending":
        return "pending"
    if value == "active":
        return "active"
    if value == "revoked":
        return "revoked"
    raise ValueError(f"unexpected binding state {value!r}")


def _revoke_reason(value: str | None) -> _RevokeReason | None:
    if value is None:
        return None
    if value == "user":
        return "user"
    if value == "stop_keyword":
        return "stop_keyword"
    if value == "user_archived":
        return "user_archived"
    if value == "admin":
        return "admin"
    if value == "provider_error":
        return "provider_error"
    raise ValueError(f"unexpected revoke_reason {value!r}")
