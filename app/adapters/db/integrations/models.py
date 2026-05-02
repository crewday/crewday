"""SQLAlchemy models for the outbound webhook layer (cd-q885).

Two workspace-scoped tables:

* :class:`WebhookSubscription` — the receiver registration.
  ``url`` is the outbound POST target. ``secret_blob`` carries the
  cd-znv4 pointer-tagged ciphertext (``0x02 || envelope_id``) for the
  HMAC-SHA256 signing secret; the plaintext lives only inside the
  matching ``secret_envelope`` row and is never read except through
  the cipher.
* :class:`WebhookDelivery` — one row per dispatch attempt-set. Carries
  the event name, the JSON payload, the retry schedule, the latest
  attempt outcome, and a permanent / dead-letter flag.

Spec: ``docs/specs/02-domain-model.md`` §"webhook_subscription" /
§"webhook_delivery", ``docs/specs/10-messaging-notifications.md``
§"Webhooks (outbound)", ``docs/specs/12-rest-api.md`` §"Messaging".

FK hygiene:

* ``workspace_id`` ``CASCADE`` on both tables — sweeping a workspace
  sweeps its subscription registry and delivery log together.
* ``WebhookDelivery.subscription_id`` ``CASCADE`` — the delivery row
  has no meaning without the subscription it targeted; deleting a
  subscription drops its in-flight + dead-letter rows in one go.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db._columns import UtcDateTime
from app.adapters.db.base import Base

# Cross-package FK target — see :mod:`app.adapters.db` package
# docstring for the load-order contract.
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = [
    "_WEBHOOK_DELIVERY_STATUS_VALUES",
    "WebhookDelivery",
    "WebhookSubscription",
]


# §10 "Retries" + cd-q885 retry schedule. Live rows transition
# ``pending → in_flight`` while the dispatcher is mid-POST,
# ``in_flight → succeeded`` on a 2xx, ``in_flight → pending`` while
# more retries remain, and ``in_flight → dead_lettered`` on retry
# exhaustion or permanent (4xx) failure.
_WEBHOOK_DELIVERY_STATUS_VALUES: tuple[str, ...] = (
    "pending",
    "in_flight",
    "succeeded",
    "dead_lettered",
    "suppressed_demo",
)


def _in_clause(values: tuple[str, ...]) -> str:
    """Render a ``col IN ('a', 'b', …)`` CHECK body fragment."""
    return "'" + "', '".join(values) + "'"


class WebhookSubscription(Base):
    """Workspace-scoped registration for an outbound HTTP receiver.

    The plaintext signing secret is never stored on the row: it is
    encrypted via :class:`~app.adapters.storage.envelope.Aes256GcmEnvelope`
    in row-backed mode (cd-znv4) and the column ``secret_blob`` carries
    the pointer-tagged ciphertext only (``0x02 || envelope_id``).
    Listing surfaces show ``secret_last_4`` for disambiguation; the
    plaintext is returned exactly once at create time and then
    forgotten by the server.

    ``events_json`` is the closed list of event names the subscription
    receives (a free-form JSON array; the §10 catalog is authoritative
    at the service layer). An empty list is invalid — the service
    refuses to register a subscription with no events.

    ``active`` is the soft-disable flag; the dispatcher refuses to
    enqueue a delivery for an inactive row.
    """

    __tablename__ = "webhook_subscription"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    # cd-znv4 pointer-tagged ciphertext for the HMAC-SHA256 signing
    # secret. Stored as text (latin-1 byte-equivalent) so the column
    # can carry a 27-byte ``0x02 || ulid`` blob without binary
    # round-trip surprises on SQLite. The cipher is the only reader.
    secret_blob: Mapped[str] = mapped_column(String, nullable=False)
    # Last 4 chars of the plaintext secret — for /webhooks listing
    # disambiguation. Plaintext-derived but not the secret itself.
    secret_last_4: Mapped[str] = mapped_column(String, nullable=False)
    # Closed list of event names ("task.completed", "approval.pending",
    # ...). Empty = invalid; service refuses.
    events_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)

    __table_args__ = (
        # Tenant-filter hot path — every read is workspace-scoped.
        Index("ix_webhook_subscription_workspace", "workspace_id"),
        # /webhooks listing pinpoints the active rows for the
        # delivery dispatcher.
        Index(
            "ix_webhook_subscription_workspace_active",
            "workspace_id",
            "active",
        ),
    )


class WebhookDelivery(Base):
    """Outbound delivery attempt-set bound to a subscription.

    One row per ``enqueue`` call; the dispatcher walks the row through
    the retry schedule (``next_attempt_at``) and stamps ``status`` /
    ``last_status_code`` / ``last_error`` on every attempt. ``attempt``
    starts at 0 and increments on each fired attempt; the schedule
    ``[0s, 30s, 5m, 1h, 6h, 24h]`` puts the cap at 6 attempts (indices
    0..5).

    ``payload_json`` is the full envelope the receiver sees: ``event``,
    ``delivery_id``, ``delivered_at``, and the event-specific ``data``
    block. Stored verbatim so the spec's "replay re-attempts the same
    payload with a new signature timestamp" contract is preserved.

    ``replayed_from_id`` (nullable, ``SET NULL``) points at the parent
    delivery on a manual replay — the audit trail keeps the chain
    visible, but a delete-cascade on the parent does not nuke the
    replay row's history.
    """

    __tablename__ = "webhook_delivery"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    subscription_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("webhook_subscription.id", ondelete="CASCADE"),
        nullable=False,
    )
    event: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # ``pending | in_flight | succeeded | dead_lettered``.
    status: Mapped[str] = mapped_column(String, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Free-form short error label ("connect_timeout", "tls_error",
    # "http_500", "permanent_4xx"). The dispatcher writes it on every
    # non-2xx attempt; cleared on success.
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    succeeded_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    replayed_from_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("webhook_delivery.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"status IN ({_in_clause(_WEBHOOK_DELIVERY_STATUS_VALUES)})",
            name="status",
        ),
        # Tenant-filter hot path — every read is workspace-scoped.
        Index("ix_webhook_delivery_workspace", "workspace_id"),
        # Dispatcher hot path — pull every row whose retry window has
        # opened. The ``status`` predicate isn't in the index but the
        # candidate set is small (most rows are ``succeeded`` /
        # ``dead_lettered``); the leading ``next_attempt_at`` keeps the
        # scan tight.
        Index(
            "ix_webhook_delivery_next_attempt",
            "next_attempt_at",
        ),
        # Subscription-scoped listing for /webhooks/{id} drill-down.
        Index(
            "ix_webhook_delivery_subscription",
            "subscription_id",
        ),
    )
