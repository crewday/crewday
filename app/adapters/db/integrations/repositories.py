"""SA-backed concretion of :class:`WebhookRepository` (cd-q885).

Pairs with :mod:`app.adapters.db.integrations.models` (the ORM) and
:mod:`app.domain.integrations.ports` (the Protocol). Consumed by the
service layer in :mod:`app.domain.integrations.webhooks` and by the
APScheduler-driven worker tick.

The repo carries an open :class:`~sqlalchemy.orm.Session` and never
commits — the caller's UoW owns the transaction boundary (§01 "Key
runtime invariants" #3). Mutating methods flush so the cipher's
follow-up read (and the audit writer's identity reference) see the
new row.

``webhook_subscription`` and ``webhook_delivery`` are workspace-scoped
(registered via :mod:`app.tenancy.registry`); the ORM tenant filter
auto-injects the ``workspace_id`` predicate. The dispatcher's
``deliver(delivery_id)`` codepath wraps its load + update in
:func:`app.tenancy.tenant_agnostic` because the worker tick has no
ambient :class:`WorkspaceContext` — it dispatches across every
workspace's pending rows in one sweep.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.integrations.models import (
    WebhookDelivery,
    WebhookSubscription,
)
from app.domain.integrations.ports import (
    WebhookDeliveryRow,
    WebhookRepository,
    WebhookSubscriptionRow,
)
from app.tenancy import tenant_agnostic

__all__ = [
    "SqlAlchemyWebhookRepository",
]


def _attach_utc(value: datetime | None) -> datetime | None:
    """Re-attach UTC tzinfo on the SQLite read path.

    SQLite drops tzinfo on round-trip; Postgres preserves it. The
    attach is a no-op on aware values and a tag on naive ones.
    Mirrors the same guard the cd-znv4 secret_envelope repo uses.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _sub_to_row(row: WebhookSubscription) -> WebhookSubscriptionRow:
    """Project an ORM :class:`WebhookSubscription` into the seam shape."""
    created_at = _attach_utc(row.created_at)
    updated_at = _attach_utc(row.updated_at)
    # ``created_at`` and ``updated_at`` are NOT NULL columns; the
    # attach helper returns ``datetime`` for non-None inputs.
    assert created_at is not None
    assert updated_at is not None
    return WebhookSubscriptionRow(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        url=row.url,
        secret_blob=row.secret_blob,
        secret_last_4=row.secret_last_4,
        events=tuple(row.events_json),
        active=bool(row.active),
        created_at=created_at,
        updated_at=updated_at,
    )


def _delivery_to_row(row: WebhookDelivery) -> WebhookDeliveryRow:
    """Project an ORM :class:`WebhookDelivery` into the seam shape."""
    created_at = _attach_utc(row.created_at)
    assert created_at is not None
    return WebhookDeliveryRow(
        id=row.id,
        workspace_id=row.workspace_id,
        subscription_id=row.subscription_id,
        event=row.event,
        payload_json=dict(row.payload_json),
        status=row.status,
        attempt=row.attempt,
        next_attempt_at=_attach_utc(row.next_attempt_at),
        last_status_code=row.last_status_code,
        last_error=row.last_error,
        last_attempted_at=_attach_utc(row.last_attempted_at),
        succeeded_at=_attach_utc(row.succeeded_at),
        dead_lettered_at=_attach_utc(row.dead_lettered_at),
        replayed_from_id=row.replayed_from_id,
        created_at=created_at,
    )


class SqlAlchemyWebhookRepository(WebhookRepository):
    """SA-backed concretion of :class:`WebhookRepository`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ---- Subscription mutations ----------------------------------

    def insert_subscription(
        self,
        *,
        sub_id: str,
        workspace_id: str,
        name: str,
        url: str,
        secret_blob: str,
        secret_last_4: str,
        events: Sequence[str],
        active: bool,
        created_at: datetime,
    ) -> WebhookSubscriptionRow:
        row = WebhookSubscription(
            id=sub_id,
            workspace_id=workspace_id,
            name=name,
            url=url,
            secret_blob=secret_blob,
            secret_last_4=secret_last_4,
            events_json=list(events),
            active=active,
            created_at=created_at,
            updated_at=created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _sub_to_row(row)

    def update_subscription(
        self,
        *,
        sub_id: str,
        name: str | None = None,
        url: str | None = None,
        events: Sequence[str] | None = None,
        active: bool | None = None,
        updated_at: datetime,
    ) -> WebhookSubscriptionRow:
        row = self._session.get(WebhookSubscription, sub_id)
        if row is None:
            raise LookupError(f"webhook_subscription {sub_id!r} not found")
        if name is not None:
            row.name = name
        if url is not None:
            row.url = url
        if events is not None:
            row.events_json = list(events)
        if active is not None:
            row.active = active
        row.updated_at = updated_at
        self._session.flush()
        return _sub_to_row(row)

    def rotate_subscription_secret(
        self,
        *,
        sub_id: str,
        secret_blob: str,
        secret_last_4: str,
        updated_at: datetime,
    ) -> WebhookSubscriptionRow:
        row = self._session.get(WebhookSubscription, sub_id)
        if row is None:
            raise LookupError(f"webhook_subscription {sub_id!r} not found")
        row.secret_blob = secret_blob
        row.secret_last_4 = secret_last_4
        row.updated_at = updated_at
        self._session.flush()
        return _sub_to_row(row)

    def delete_subscription(self, *, sub_id: str) -> None:
        row = self._session.get(WebhookSubscription, sub_id)
        if row is None:
            return
        self._session.delete(row)
        self._session.flush()

    def get_subscription(self, *, sub_id: str) -> WebhookSubscriptionRow | None:
        # Worker tick reads cross-tenant; wrap in tenant_agnostic so
        # the load works whether or not a WorkspaceContext is active.
        # Service-layer callers that already hold a context still get
        # the right scoping because they re-check workspace_id on the
        # returned row.
        with tenant_agnostic():
            row = self._session.get(WebhookSubscription, sub_id)
        if row is None:
            return None
        return _sub_to_row(row)

    def list_subscriptions(
        self,
        *,
        workspace_id: str,
        active_only: bool = False,
    ) -> tuple[WebhookSubscriptionRow, ...]:
        stmt = select(WebhookSubscription).where(
            WebhookSubscription.workspace_id == workspace_id
        )
        if active_only:
            stmt = stmt.where(WebhookSubscription.active.is_(True))
        stmt = stmt.order_by(WebhookSubscription.created_at.desc())
        rows = list(self._session.scalars(stmt))
        return tuple(_sub_to_row(r) for r in rows)

    # ---- Delivery mutations --------------------------------------

    def insert_delivery(
        self,
        *,
        delivery_id: str,
        workspace_id: str,
        subscription_id: str,
        event: str,
        payload_json: dict[str, Any],
        status: str,
        attempt: int,
        next_attempt_at: datetime | None,
        replayed_from_id: str | None,
        created_at: datetime,
    ) -> WebhookDeliveryRow:
        row = WebhookDelivery(
            id=delivery_id,
            workspace_id=workspace_id,
            subscription_id=subscription_id,
            event=event,
            payload_json=dict(payload_json),
            status=status,
            attempt=attempt,
            next_attempt_at=next_attempt_at,
            last_status_code=None,
            last_error=None,
            last_attempted_at=None,
            succeeded_at=None,
            dead_lettered_at=None,
            replayed_from_id=replayed_from_id,
            created_at=created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _delivery_to_row(row)

    def get_delivery(self, *, delivery_id: str) -> WebhookDeliveryRow | None:
        # Worker tick reads cross-tenant; same posture as
        # ``get_subscription``.
        with tenant_agnostic():
            row = self._session.get(WebhookDelivery, delivery_id)
        if row is None:
            return None
        return _delivery_to_row(row)

    def update_delivery_attempt(
        self,
        *,
        delivery_id: str,
        status: str,
        attempt: int,
        next_attempt_at: datetime | None,
        last_status_code: int | None,
        last_error: str | None,
        last_attempted_at: datetime,
        succeeded_at: datetime | None = None,
        dead_lettered_at: datetime | None = None,
    ) -> WebhookDeliveryRow:
        # Worker tick writes cross-tenant; the row carries its own
        # ``workspace_id`` and the dispatcher resolves the right
        # subscription via the FK without leaning on an ambient
        # WorkspaceContext.
        with tenant_agnostic():
            row = self._session.get(WebhookDelivery, delivery_id)
            if row is None:
                raise LookupError(f"webhook_delivery {delivery_id!r} not found")
            row.status = status
            row.attempt = attempt
            row.next_attempt_at = next_attempt_at
            row.last_status_code = last_status_code
            row.last_error = last_error
            row.last_attempted_at = last_attempted_at
            if succeeded_at is not None:
                row.succeeded_at = succeeded_at
            if dead_lettered_at is not None:
                row.dead_lettered_at = dead_lettered_at
            self._session.flush()
        return _delivery_to_row(row)
