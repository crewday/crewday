"""Concrete notification sink backed by the messaging domain service."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.messaging.models import Notification
from app.adapters.db.messaging.repositories import SqlAlchemyEmailDeliveryRepository
from app.adapters.mail.null import NullMailer
from app.adapters.notifications.ports import NotificationKind
from app.domain.messaging.notifications import NotificationService
from app.events.bus import EventBus
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock

__all__ = ["SqlAlchemyNotificationSink"]


class SqlAlchemyNotificationSink:
    def __init__(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        clock: Clock,
        bus: EventBus,
    ) -> None:
        self._session = session
        self._ctx = ctx
        self._service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=NullMailer(),
            clock=clock,
            bus=bus,
            email_deliveries=SqlAlchemyEmailDeliveryRepository(session),
        )

    def notify(
        self,
        *,
        recipient_user_id: str,
        kind: NotificationKind,
        payload: Mapping[str, object],
    ) -> str:
        return self._service.notify(
            recipient_user_id=recipient_user_id,
            kind=kind,
            payload=payload,
        )

    def exists(
        self,
        *,
        recipient_user_id: str,
        kind: NotificationKind,
        payload_key: str,
        payload_value: object,
    ) -> bool:
        with tenant_agnostic():
            payloads = self._session.scalars(
                select(Notification.payload_json)
                .where(Notification.workspace_id == self._ctx.workspace_id)
                .where(Notification.recipient_user_id == recipient_user_id)
                .where(Notification.kind == kind.value)
            ).all()
        return any(payload.get(payload_key) == payload_value for payload in payloads)
