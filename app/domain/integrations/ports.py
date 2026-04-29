"""Repository Protocol for the outbound webhook layer (cd-q885).

The persisted shape lives across two tables:
``webhook_subscription`` and ``webhook_delivery`` (cd-q885 migration).
This Protocol is the seam :mod:`app.domain.integrations.webhooks` uses
to read and write both — the SA-backed concretion lives at
:mod:`app.adapters.db.integrations.repositories`.

Spec: ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 —
each context defines its own repository port in its public surface
and a SQLAlchemy adapter under ``app/adapters/db/<context>/``.

The repository carries an open SQLAlchemy ``Session`` and never
commits — the caller's UoW owns the transaction boundary (§01 "Key
runtime invariants" #3). Mutating methods flush so a peer read in the
same UoW sees the new row.

Both row shapes are workspace-scoped value objects. The cipher's
pointer-tagged ciphertext (``secret_blob``) carries no plaintext;
callers that need the plaintext must route through
:class:`~app.adapters.storage.envelope.Aes256GcmEnvelope.decrypt`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

__all__ = [
    "WebhookDeliveryRow",
    "WebhookRepository",
    "WebhookSubscriptionRow",
]


@dataclass(frozen=True, slots=True)
class WebhookSubscriptionRow:
    """Immutable projection of a ``webhook_subscription`` row.

    Mirrors :class:`app.adapters.db.integrations.models.WebhookSubscription`
    column-for-column. Declared on the seam so the SA adapter projects
    ORM rows into a domain-owned shape without forcing the dispatcher
    or service helpers to import the ORM class.

    ``secret_blob`` is the cd-znv4 pointer-tagged ciphertext (`0x02 ||
    envelope_id`); callers must route through the envelope cipher to
    recover the plaintext.
    """

    id: str
    workspace_id: str
    name: str
    url: str
    secret_blob: str
    secret_last_4: str
    events: tuple[str, ...]
    active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WebhookDeliveryRow:
    """Immutable projection of a ``webhook_delivery`` row."""

    id: str
    workspace_id: str
    subscription_id: str
    event: str
    payload_json: dict[str, Any]
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


class WebhookRepository(Protocol):
    """Read + write seam for the webhook subscription + delivery tables.

    Concrete implementation:
    :class:`app.adapters.db.integrations.repositories.SqlAlchemyWebhookRepository`.
    """

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
        """Insert a new subscription. Flushes."""
        ...

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
        """Patch a subscription's mutable fields. Flushes.

        ``None`` means "leave alone"; only fields explicitly passed
        are written. ``updated_at`` is always bumped by the caller.
        """
        ...

    def rotate_subscription_secret(
        self,
        *,
        sub_id: str,
        secret_blob: str,
        secret_last_4: str,
        updated_at: datetime,
    ) -> WebhookSubscriptionRow:
        """Replace the encrypted signing secret. Flushes."""
        ...

    def delete_subscription(self, *, sub_id: str) -> None:
        """Hard delete; in-flight + dead-letter rows cascade."""
        ...

    def get_subscription(self, *, sub_id: str) -> WebhookSubscriptionRow | None:
        """Return one subscription, ``None`` if absent."""
        ...

    def list_subscriptions(
        self,
        *,
        workspace_id: str,
        active_only: bool = False,
    ) -> tuple[WebhookSubscriptionRow, ...]:
        """List every subscription in the workspace (newest first)."""
        ...

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
        """Insert a delivery row. Flushes."""
        ...

    def get_delivery(self, *, delivery_id: str) -> WebhookDeliveryRow | None:
        """Return one delivery row, ``None`` if absent."""
        ...

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
        """Stamp the latest attempt's outcome on the row. Flushes."""
        ...
