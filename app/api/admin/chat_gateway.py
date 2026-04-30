"""Deployment-admin chat gateway read surface.

Mounts under ``/admin/api/v1/chat`` (§12 "Admin surface", §23 "REST
surface"). The current backend only has deployment-default inbound
webhook configuration; provider override persistence and template sync
tables are not implemented yet, so those read endpoints expose stable
empty/default state rather than fabricating successful provider data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.messaging.models import ChatGatewayBinding
from app.api.admin.deps import current_deployment_admin_principal
from app.api.deps import db_session
from app.config import Settings, get_settings
from app.tenancy import DeploymentContext, tenant_agnostic

_Db = Annotated[Session, Depends(db_session)]

_ChannelKind = Literal["offapp_whatsapp", "offapp_telegram"]
_ProviderStatus = Literal["connected", "error", "not_configured"]
_TemplateStatus = Literal["approved", "pending", "rejected", "paused"]


class AdminChatProviderCredential(BaseModel):
    """One display-only provider credential field."""

    field: str
    label: str
    display_stub: str
    set: bool
    updated_at: str | None
    updated_by: str | None


class AdminChatProviderTemplate(BaseModel):
    """Meta/transport template sync state."""

    name: str
    purpose: str
    status: _TemplateStatus
    last_sync_at: str | None
    rejection_reason: str | None


class AdminChatProvider(BaseModel):
    """Provider row consumed by ``/admin/chat-gateway``."""

    channel_kind: _ChannelKind
    label: str
    phone_display: str
    status: _ProviderStatus
    last_webhook_at: str | None
    last_webhook_error: str | None
    webhook_url: str
    verify_token_stub: str
    credentials: list[AdminChatProviderCredential]
    templates: list[AdminChatProviderTemplate]
    per_workspace_soft_cap: int
    daily_outbound_cap: int
    outbound_24h: int
    delivery_error_rate_pct: float


class AdminChatOverrideRow(BaseModel):
    """Workspace using a custom chat provider instead of deployment default."""

    workspace_id: str
    workspace_name: str
    channel_kind: _ChannelKind
    phone_display: str
    status: _ProviderStatus
    created_at: str
    reason: str | None


class AdminChatHealthProvider(BaseModel):
    """Provider health fields split out for API consumers."""

    channel_kind: _ChannelKind
    status: _ProviderStatus
    last_webhook_at: str | None
    last_webhook_error: str | None
    outbound_24h: int
    delivery_error_rate_pct: float


class AdminChatHealthResponse(BaseModel):
    """Body of ``GET /admin/api/v1/chat/health``."""

    providers: list[AdminChatHealthProvider]


class _ProviderRuntime(BaseModel):
    """Internal projection of currently available provider state."""

    channel_kind: _ChannelKind
    label: str
    phone_display: str
    status: _ProviderStatus
    webhook_url: str
    verify_token_stub: str
    credentials: list[AdminChatProviderCredential]
    templates: list[AdminChatProviderTemplate]
    last_webhook_at: str | None
    last_webhook_error: str | None = None
    per_workspace_soft_cap: int = 0
    daily_outbound_cap: int = 1000
    outbound_24h: int = 0
    delivery_error_rate_pct: float = 0.0


def build_admin_chat_gateway_router() -> APIRouter:
    """Return the deployment-admin chat gateway read router."""
    router = APIRouter(prefix="/chat", tags=["admin", "chat_gateway"])

    @router.get(
        "/providers",
        response_model=list[AdminChatProvider],
        operation_id="admin.chat.providers.list",
        summary="List deployment-default chat gateway providers",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "chat-providers-list",
                "summary": "List chat gateway providers",
                "mutates": False,
            },
        },
    )
    def list_providers(
        _ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        request: Request,
    ) -> list[AdminChatProvider]:
        providers = _provider_runtime_rows(
            session, settings=_settings_from_request(request)
        )
        return [
            AdminChatProvider(
                channel_kind=row.channel_kind,
                label=row.label,
                phone_display=row.phone_display,
                status=row.status,
                last_webhook_at=row.last_webhook_at,
                last_webhook_error=row.last_webhook_error,
                webhook_url=row.webhook_url,
                verify_token_stub=row.verify_token_stub,
                credentials=row.credentials,
                templates=row.templates,
                per_workspace_soft_cap=row.per_workspace_soft_cap,
                daily_outbound_cap=row.daily_outbound_cap,
                outbound_24h=row.outbound_24h,
                delivery_error_rate_pct=row.delivery_error_rate_pct,
            )
            for row in providers
        ]

    @router.get(
        "/templates",
        response_model=list[AdminChatProviderTemplate],
        operation_id="admin.chat.templates.list",
        summary="List chat gateway template sync state",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "chat-templates-list",
                "summary": "List chat gateway templates",
                "mutates": False,
            },
        },
    )
    def list_templates(
        _ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        request: Request,
    ) -> list[AdminChatProviderTemplate]:
        providers = _provider_runtime_rows(
            session, settings=_settings_from_request(request)
        )
        return [template for provider in providers for template in provider.templates]

    @router.get(
        "/overrides",
        response_model=list[AdminChatOverrideRow],
        operation_id="admin.chat.overrides.list",
        summary="List workspaces using custom chat gateway providers",
        description=(
            "Returns an empty list until workspace-specific chat provider "
            "override persistence ships; this is a stable read shape, not "
            "a synthetic success state."
        ),
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "chat-overrides-list",
                "summary": "List chat gateway workspace overrides",
                "mutates": False,
            },
        },
    )
    def list_overrides(
        _ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
    ) -> list[AdminChatOverrideRow]:
        """Return workspace provider overrides once that table exists."""
        return []

    @router.get(
        "/health",
        response_model=AdminChatHealthResponse,
        operation_id="admin.chat.health.get",
        summary="Read chat gateway provider health",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "chat-health",
                "summary": "Read chat gateway health",
                "mutates": False,
            },
        },
    )
    def get_health(
        _ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        request: Request,
    ) -> AdminChatHealthResponse:
        providers = _provider_runtime_rows(
            session, settings=_settings_from_request(request)
        )
        return AdminChatHealthResponse(
            providers=[
                AdminChatHealthProvider(
                    channel_kind=row.channel_kind,
                    status=row.status,
                    last_webhook_at=row.last_webhook_at,
                    last_webhook_error=row.last_webhook_error,
                    outbound_24h=row.outbound_24h,
                    delivery_error_rate_pct=row.delivery_error_rate_pct,
                )
                for row in providers
            ]
        )

    return router


def _provider_runtime_rows(
    session: Session,
    *,
    settings: Settings,
) -> list[_ProviderRuntime]:
    last_webhooks = _last_webhook_by_provider(session)
    return [
        _whatsapp_runtime(
            settings=settings,
            last_webhook_at=last_webhooks.get("meta_whatsapp"),
        ),
        _telegram_runtime(
            last_webhook_at=last_webhooks.get("telegram"),
        ),
    ]


def _whatsapp_runtime(
    *,
    settings: Settings,
    last_webhook_at: datetime | None,
) -> _ProviderRuntime:
    secret_configured = _secret_is_set(settings.chat_gateway_meta_whatsapp_secret)
    workspace_configured = bool(settings.chat_gateway_workspace_id)
    configured = secret_configured and workspace_configured
    status: _ProviderStatus = "connected" if configured else "not_configured"
    return _ProviderRuntime(
        channel_kind="offapp_whatsapp",
        label="WhatsApp",
        phone_display="Deployment default" if configured else "Not configured",
        status=status,
        webhook_url=_webhook_url(settings, provider="meta_whatsapp"),
        verify_token_stub=_stub(secret_configured),
        credentials=[
            _credential(
                field="webhook_signature_secret",
                label="Webhook signature secret",
                is_set=secret_configured,
            ),
            _credential(
                field="access_token",
                label="Access token",
                is_set=False,
            ),
            _credential(
                field="phone_number_id",
                label="Phone number ID",
                is_set=False,
            ),
            _credential(
                field="business_account_id",
                label="Business account ID",
                is_set=False,
            ),
        ],
        templates=_default_whatsapp_templates(
            status="pending" if configured else "paused"
        ),
        last_webhook_at=_format_dt(last_webhook_at),
    )


def _telegram_runtime(
    *,
    last_webhook_at: datetime | None,
) -> _ProviderRuntime:
    return _ProviderRuntime(
        channel_kind="offapp_telegram",
        label="Telegram",
        phone_display="Not configured",
        status="not_configured",
        webhook_url="",
        verify_token_stub="",
        credentials=[
            _credential(field="bot_token", label="Bot token", is_set=False),
            _credential(field="webhook_secret", label="Webhook secret", is_set=False),
        ],
        templates=[],
        last_webhook_at=_format_dt(last_webhook_at),
    )


def _default_whatsapp_templates(
    *,
    status: _TemplateStatus,
) -> list[AdminChatProviderTemplate]:
    return [
        AdminChatProviderTemplate(
            name="chat_channel_link_code",
            purpose="Initial channel link verification code",
            status=status,
            last_sync_at=None,
            rejection_reason=None,
        ),
        AdminChatProviderTemplate(
            name="chat_agent_nudge",
            purpose="Agent follow-up outside the 24-hour session window",
            status=status,
            last_sync_at=None,
            rejection_reason=None,
        ),
        AdminChatProviderTemplate(
            name="chat_workspace_pick",
            purpose="Workspace-selection prompt for ambiguous inbound messages",
            status=status,
            last_sync_at=None,
            rejection_reason=None,
        ),
    ]


def _credential(
    *,
    field: str,
    label: str,
    is_set: bool,
) -> AdminChatProviderCredential:
    return AdminChatProviderCredential(
        field=field,
        label=label,
        display_stub=_stub(is_set),
        set=is_set,
        updated_at=None,
        updated_by=None,
    )


def _last_webhook_by_provider(session: Session) -> dict[str, datetime]:
    rows: list[tuple[str, datetime]] = []
    with tenant_agnostic():
        for provider, last_at in session.execute(
            select(
                ChatGatewayBinding.provider,
                func.max(ChatGatewayBinding.last_message_at),
            )
            .where(ChatGatewayBinding.last_message_at.is_not(None))
            .group_by(ChatGatewayBinding.provider)
        ).all():
            if isinstance(last_at, datetime):
                rows.append((provider, last_at))
    return dict(rows)


def _settings_from_request(request: Request) -> Settings:
    raw = getattr(request.app.state, "settings", None)
    if isinstance(raw, Settings):
        return raw
    return get_settings()


def _webhook_url(settings: Settings, *, provider: str) -> str:
    base = (settings.public_url or "").rstrip("/")
    path = f"/webhooks/chat/{provider}"
    if base:
        return f"{base}{path}"
    return path


def _secret_is_set(secret: SecretStr | None) -> bool:
    return secret is not None and bool(secret.get_secret_value().strip())


def _stub(is_set: bool) -> str:
    return "***" if is_set else ""


def _format_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
