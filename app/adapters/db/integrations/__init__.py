"""integrations — outbound webhook subscriptions + delivery log (cd-q885).

Importing this package registers per-table tenancy behaviour:

* ``webhook_subscription`` and ``webhook_delivery`` are
  workspace-scoped (carry ``workspace_id``); the ORM tenant filter
  auto-injects a ``workspace_id`` predicate on every SELECT / UPDATE
  / DELETE. A bare read without a
  :class:`~app.tenancy.WorkspaceContext` raises
  :class:`~app.tenancy.orm_filter.TenantFilterMissing`.

The subscription's plaintext secret never lives on the row: it is
encrypted by :class:`~app.adapters.storage.envelope.Aes256GcmEnvelope`
in row-backed mode (cd-znv4) and the column carries the tiny
pointer-tagged blob (``0x02 || envelope_id``). The matching
``secret_envelope`` row carries
``owner_entity_kind='webhook_subscription'``. The cipher is wired by
the caller; the model is opaque to encryption details.

See ``docs/specs/10-messaging-notifications.md`` §"Webhooks
(outbound)" and ``docs/specs/02-domain-model.md`` §"webhook_subscription"
/ §"webhook_delivery".
"""

from __future__ import annotations

from app.adapters.db.integrations.models import (
    WebhookDelivery,
    WebhookSubscription,
)
from app.tenancy.registry import register

register("webhook_subscription")
register("webhook_delivery")

__all__ = [
    "WebhookDelivery",
    "WebhookSubscription",
]
