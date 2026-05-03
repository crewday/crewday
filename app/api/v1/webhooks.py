"""Workspace outbound webhook API routes.

The domain service owns subscription CRUD and secret handling. This
router keeps the HTTP surface thin: validate request bodies, enforce
workspace-level settings permission, project subscription rows into the
manager page shape, and translate domain errors into stable API errors.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.integrations.models import WebhookDelivery
from app.adapters.db.integrations.repositories import SqlAlchemyWebhookRepository
from app.adapters.db.secrets.repositories import SqlAlchemySecretEnvelopeRepository
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.adapters.storage.ports import EnvelopeEncryptor
from app.api.deps import current_workspace_context, db_session
from app.authz.dep import Permission
from app.config import Settings, get_settings
from app.domain.integrations.webhooks import (
    SubscriptionView,
    create_subscription,
    delete_subscription,
    enable_subscription,
    enqueue,
    list_subscriptions,
    rotate_subscription_secret,
    update_subscription,
)
from app.events.bus import bus as default_event_bus
from app.events.types import WorkspaceChanged
from app.tenancy import WorkspaceContext

router = APIRouter(tags=["webhooks"])

__all__ = [
    "WebhookCreateRequest",
    "WebhookDeliveryResponse",
    "WebhookResponse",
    "WebhookTestRequest",
    "WebhookUpdateRequest",
    "get_envelope",
    "router",
]

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_SettingsGate = Depends(Permission("scope.edit_settings", scope_kind="workspace"))


class WebhookCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=160)
    url: str = Field(..., min_length=1, max_length=2048)
    events: list[str] = Field(..., min_length=1, max_length=128)
    active: bool = True
    secret: str | None = Field(default=None, min_length=16, max_length=512)


class WebhookUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    events: list[str] | None = Field(default=None, min_length=1, max_length=128)
    active: bool | None = None

    @model_validator(mode="after")
    def _require_field(self) -> WebhookUpdateRequest:
        if not self.model_fields_set or all(
            getattr(self, field_name) is None for field_name in self.model_fields_set
        ):
            raise ValueError("PATCH body must include at least one non-null field")
        return self


class WebhookTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str | None = Field(default=None, min_length=1, max_length=128)


class WebhookResponse(BaseModel):
    id: str
    name: str
    url: str
    events: list[str]
    active: bool
    paused_reason: str | None
    paused_at: datetime | None
    last_delivery_at: datetime | None
    last_delivery_status: int | None
    secret_last_4: str
    secret: str | None = None
    created_at: datetime
    updated_at: datetime


class WebhookDeliveryResponse(BaseModel):
    id: str
    event: str
    status: str
    attempt: int
    next_attempt_at: datetime | None
    last_status_code: int | None
    last_error: str | None
    last_attempted_at: datetime | None
    succeeded_at: datetime | None
    dead_lettered_at: datetime | None
    replayed_from_id: str | None
    created_at: datetime


class _DeliverySummary(BaseModel):
    subscription_id: str
    last_delivery_at: datetime | None
    last_delivery_status: int | None


def get_envelope(
    session: _Db,
    settings: Annotated[Settings, Depends(get_settings)],
) -> EnvelopeEncryptor:
    if settings.root_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "envelope_unavailable"},
        )
    return Aes256GcmEnvelope(
        settings.root_key,
        repository=SqlAlchemySecretEnvelopeRepository(session),
    )


def _repo(session: Session) -> SqlAlchemyWebhookRepository:
    return SqlAlchemyWebhookRepository(session)


def _publish_webhooks_changed(ctx: WorkspaceContext) -> None:
    default_event_bus.publish(
        WorkspaceChanged(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=datetime.now(UTC),
            changed_keys=("webhooks",),
        )
    )


def _attach_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "webhook_not_found"},
        )
    if isinstance(exc, ValueError):
        return HTTPException(
            status_code=422,
            detail={"error": "invalid_webhook", "message": str(exc)},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


def _delivery_summaries(
    session: Session,
    *,
    workspace_id: str,
    subscription_ids: Sequence[str],
) -> dict[str, _DeliverySummary]:
    if not subscription_ids:
        return {}

    rows = session.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.workspace_id == workspace_id)
        .where(WebhookDelivery.subscription_id.in_(subscription_ids))
        .order_by(
            WebhookDelivery.last_attempted_at.is_(None),
            WebhookDelivery.last_attempted_at.desc(),
            WebhookDelivery.created_at.desc(),
        )
    )
    summaries: dict[str, _DeliverySummary] = {}
    for row in rows:
        if row.subscription_id in summaries:
            continue
        summaries[row.subscription_id] = _DeliverySummary(
            subscription_id=row.subscription_id,
            last_delivery_at=_attach_utc(row.last_attempted_at or row.created_at),
            last_delivery_status=row.last_status_code,
        )
    return summaries


def _to_response(
    view: SubscriptionView,
    *,
    summary: _DeliverySummary | None,
) -> WebhookResponse:
    return WebhookResponse(
        id=view.id,
        name=view.name,
        url=view.url,
        events=list(view.events),
        active=view.active,
        paused_reason=view.paused_reason,
        paused_at=view.paused_at,
        last_delivery_at=summary.last_delivery_at if summary is not None else None,
        last_delivery_status=(
            summary.last_delivery_status if summary is not None else None
        ),
        secret_last_4=view.secret_last_4,
        secret=view.plaintext_secret,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _to_delivery_response(row: WebhookDelivery) -> WebhookDeliveryResponse:
    return WebhookDeliveryResponse(
        id=row.id,
        event=row.event,
        status=row.status,
        attempt=row.attempt,
        next_attempt_at=_attach_utc(row.next_attempt_at),
        last_status_code=row.last_status_code,
        last_error=row.last_error,
        last_attempted_at=_attach_utc(row.last_attempted_at),
        succeeded_at=_attach_utc(row.succeeded_at),
        dead_lettered_at=_attach_utc(row.dead_lettered_at),
        replayed_from_id=row.replayed_from_id,
        created_at=_attach_utc(row.created_at) or row.created_at,
    )


@router.get(
    "",
    response_model=list[WebhookResponse],
    operation_id="webhooks.list",
    summary="List outbound webhook subscriptions",
    dependencies=[_SettingsGate],
    openapi_extra={
        "x-cli": {
            "group": "webhooks",
            "verb": "list",
            "summary": "List outbound webhook subscriptions",
            "mutates": False,
        },
    },
)
def list_route(ctx: _Ctx, session: _Db) -> list[WebhookResponse]:
    views = list_subscriptions(ctx, repo=_repo(session))
    summaries = _delivery_summaries(
        session,
        workspace_id=ctx.workspace_id,
        subscription_ids=[view.id for view in views],
    )
    return [_to_response(view, summary=summaries.get(view.id)) for view in views]


@router.get(
    "/{webhook_id}",
    response_model=WebhookResponse,
    operation_id="webhooks.read",
    summary="Read an outbound webhook subscription",
    dependencies=[_SettingsGate],
    openapi_extra={
        "x-cli": {
            "group": "webhooks",
            "verb": "read",
            "summary": "Read an outbound webhook subscription",
            "mutates": False,
        },
    },
)
def read_route(webhook_id: str, ctx: _Ctx, session: _Db) -> WebhookResponse:
    view = next(
        (
            subscription
            for subscription in list_subscriptions(ctx, repo=_repo(session))
            if subscription.id == webhook_id
        ),
        None,
    )
    if view is None:
        raise _error(LookupError(webhook_id))
    summaries = _delivery_summaries(
        session,
        workspace_id=ctx.workspace_id,
        subscription_ids=[view.id],
    )
    return _to_response(view, summary=summaries.get(view.id))


@router.get(
    "/{webhook_id}/deliveries",
    response_model=list[WebhookDeliveryResponse],
    operation_id="webhooks.deliveries.list",
    summary="List delivery attempts for an outbound webhook subscription",
    dependencies=[_SettingsGate],
    openapi_extra={
        "x-cli": {
            "group": "webhooks",
            "verb": "deliveries",
            "summary": "List outbound webhook delivery attempts",
            "mutates": False,
        },
    },
)
def list_deliveries_route(
    webhook_id: str,
    ctx: _Ctx,
    session: _Db,
) -> list[WebhookDeliveryResponse]:
    existing = _repo(session).get_subscription(sub_id=webhook_id)
    if existing is None or existing.workspace_id != ctx.workspace_id:
        raise _error(LookupError(webhook_id))
    rows = session.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.workspace_id == ctx.workspace_id)
        .where(WebhookDelivery.subscription_id == webhook_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(100)
    )
    return [_to_delivery_response(row) for row in rows]


@router.post(
    "/{webhook_id}/test",
    response_model=WebhookDeliveryResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="webhooks.test",
    summary="Enqueue a test delivery for an outbound webhook subscription",
    dependencies=[_SettingsGate],
    openapi_extra={
        "x-cli": {
            "group": "webhooks",
            "verb": "test",
            "summary": "Enqueue a test webhook delivery",
            "mutates": True,
        },
    },
)
def test_route(
    webhook_id: str,
    ctx: _Ctx,
    session: _Db,
    body: WebhookTestRequest | None = None,
) -> WebhookDeliveryResponse:
    repo = _repo(session)
    existing = repo.get_subscription(sub_id=webhook_id)
    if existing is None or existing.workspace_id != ctx.workspace_id:
        raise _error(LookupError(webhook_id))
    if not existing.events:
        raise _error(ValueError("webhook subscription must have at least one event"))
    event = (
        body.event
        if body is not None and body.event is not None
        else existing.events[0]
    )
    if event not in existing.events:
        raise _error(ValueError("test event must be registered on the subscription"))
    delivery_ids = enqueue(
        repo=repo,
        workspace_id=ctx.workspace_id,
        subscription_id=webhook_id,
        event=event,
        data={"test": True, "subscription_id": webhook_id},
    )
    if not delivery_ids:
        raise _error(ValueError("webhook subscription is disabled or has no events"))
    delivery = repo.get_delivery(delivery_id=delivery_ids[0])
    if delivery is None:
        raise _error(LookupError(delivery_ids[0]))
    _publish_webhooks_changed(ctx)
    row = session.get(WebhookDelivery, delivery.id)
    if row is None:
        raise _error(LookupError(delivery.id))
    return _to_delivery_response(row)


@router.post(
    "/{webhook_id}/rotate-secret",
    response_model=WebhookResponse,
    operation_id="webhooks.secret.rotate",
    summary="Rotate an outbound webhook subscription secret",
    dependencies=[_SettingsGate],
    openapi_extra={
        "x-cli": {
            "group": "webhooks",
            "verb": "rotate-secret",
            "summary": "Rotate an outbound webhook subscription secret",
            "mutates": True,
        },
    },
)
def rotate_secret_route(
    webhook_id: str,
    ctx: _Ctx,
    session: _Db,
    envelope: Annotated[EnvelopeEncryptor, Depends(get_envelope)],
) -> WebhookResponse:
    try:
        view = rotate_subscription_secret(
            session,
            ctx,
            repo=_repo(session),
            envelope=envelope,
            sub_id=webhook_id,
        )
    except (LookupError, ValueError) as exc:
        raise _error(exc) from exc
    _publish_webhooks_changed(ctx)
    summaries = _delivery_summaries(
        session,
        workspace_id=ctx.workspace_id,
        subscription_ids=[view.id],
    )
    return _to_response(view, summary=summaries.get(view.id))


@router.post(
    "/{webhook_id}/enable",
    response_model=WebhookResponse,
    operation_id="webhooks.enable",
    summary="Enable an outbound webhook subscription",
    dependencies=[_SettingsGate],
    openapi_extra={
        "x-cli": {
            "group": "webhooks",
            "verb": "enable",
            "summary": "Enable an outbound webhook subscription",
            "mutates": True,
        },
    },
)
def enable_route(webhook_id: str, ctx: _Ctx, session: _Db) -> WebhookResponse:
    try:
        view = enable_subscription(
            session,
            ctx,
            repo=_repo(session),
            sub_id=webhook_id,
        )
    except LookupError as exc:
        raise _error(exc) from exc
    _publish_webhooks_changed(ctx)
    summaries = _delivery_summaries(
        session,
        workspace_id=ctx.workspace_id,
        subscription_ids=[view.id],
    )
    return _to_response(view, summary=summaries.get(view.id))


@router.post(
    "",
    response_model=WebhookResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="webhooks.create",
    summary="Create an outbound webhook subscription",
    dependencies=[_SettingsGate],
    openapi_extra={
        "x-cli": {
            "group": "webhooks",
            "verb": "create",
            "summary": "Create an outbound webhook subscription",
            "mutates": True,
        },
    },
)
def create_route(
    body: WebhookCreateRequest,
    ctx: _Ctx,
    session: _Db,
    envelope: Annotated[EnvelopeEncryptor, Depends(get_envelope)],
) -> WebhookResponse:
    try:
        view = create_subscription(
            session,
            ctx,
            repo=_repo(session),
            envelope=envelope,
            name=body.name,
            url=body.url,
            events=body.events,
            secret=body.secret,
            active=body.active,
        )
    except ValueError as exc:
        raise _error(exc) from exc
    _publish_webhooks_changed(ctx)
    return _to_response(view, summary=None)


@router.patch(
    "/{webhook_id}",
    response_model=WebhookResponse,
    operation_id="webhooks.update",
    summary="Update an outbound webhook subscription",
    dependencies=[_SettingsGate],
    openapi_extra={
        "x-cli": {
            "group": "webhooks",
            "verb": "update",
            "summary": "Update an outbound webhook subscription",
            "mutates": True,
        },
    },
)
def update_route(
    webhook_id: str,
    body: WebhookUpdateRequest,
    ctx: _Ctx,
    session: _Db,
) -> WebhookResponse:
    try:
        view = update_subscription(
            session,
            ctx,
            repo=_repo(session),
            sub_id=webhook_id,
            name=body.name,
            url=body.url,
            events=body.events,
            active=body.active,
        )
    except (LookupError, ValueError) as exc:
        raise _error(exc) from exc
    _publish_webhooks_changed(ctx)
    summaries = _delivery_summaries(
        session,
        workspace_id=ctx.workspace_id,
        subscription_ids=[view.id],
    )
    return _to_response(view, summary=summaries.get(view.id))


@router.delete(
    "/{webhook_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="webhooks.delete",
    summary="Delete an outbound webhook subscription",
    dependencies=[_SettingsGate],
    openapi_extra={
        "x-cli": {
            "group": "webhooks",
            "verb": "delete",
            "summary": "Delete an outbound webhook subscription",
            "mutates": True,
        },
    },
)
def delete_route(webhook_id: str, ctx: _Ctx, session: _Db) -> Response:
    try:
        delete_subscription(
            session,
            ctx,
            repo=_repo(session),
            sub_id=webhook_id,
        )
    except LookupError as exc:
        raise _error(exc) from exc
    _publish_webhooks_changed(ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
