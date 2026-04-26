"""webhooks cd-q885

Revision ID: c0d3e6f9a2b5
Revises: b9c2d5e8f1a4
Create Date: 2026-04-26 18:00:00.000000

Lands the §10 outbound webhook tables — ``webhook_subscription`` and
``webhook_delivery`` — for cd-q885's HMAC-signed delivery pipeline.
Both tables are workspace-scoped (carry ``workspace_id`` + register
through :mod:`app.tenancy.registry`).

``webhook_subscription`` columns:

* ``id`` — ULID PK.
* ``workspace_id`` — FK → ``workspace.id`` ``ON DELETE CASCADE``.
* ``name`` — operator-facing label.
* ``url`` — outbound POST target.
* ``secret_blob`` — pointer-tagged ciphertext for the HMAC-SHA256
  signing secret (cd-znv4 row-backed envelope; the blob is exactly
  ``0x02 || envelope_id`` in latin-1-text form so the column type
  can stay ``String`` on both SQLite and Postgres without binary-
  transcoding surprises).
* ``secret_last_4`` — last 4 chars of the plaintext for /webhooks
  listing disambiguation.
* ``events_json`` — JSON array of subscribed event names.
* ``active`` — soft-disable flag.
* ``created_at`` / ``updated_at`` — aware UTC timestamps.

``webhook_delivery`` columns:

* ``id`` — ULID PK.
* ``workspace_id`` — FK → ``workspace.id`` ``ON DELETE CASCADE``.
* ``subscription_id`` — FK → ``webhook_subscription.id`` ``ON DELETE
  CASCADE``. The delivery row has no meaning without the
  subscription it targeted; deleting a subscription drops its
  in-flight + dead-letter rows.
* ``event`` — event name (``approval.pending``, ``task.completed``,
  …).
* ``payload_json`` — full envelope: ``event``, ``delivery_id``,
  ``delivered_at``, ``data``.
* ``status`` — ``pending | in_flight | succeeded | dead_lettered``.
  CHECK-enforced via ``ck_webhook_delivery_status``.
* ``attempt`` — 0-indexed attempt counter; cd-q885 schedule caps at
  6 (``[0s, 30s, 5m, 1h, 6h, 24h]``).
* ``next_attempt_at`` — wall-clock timestamp the dispatcher should
  refire; ``NULL`` on terminal rows.
* ``last_status_code`` — HTTP status of the latest attempt (``NULL``
  on network / timeout failures).
* ``last_error`` — short error label (``http_500``,
  ``timeout:ConnectTimeout``, …).
* ``last_attempted_at`` — wall-clock of the most recent attempt.
* ``succeeded_at`` / ``dead_lettered_at`` — terminal stamps; only
  one is non-null per row.
* ``replayed_from_id`` — FK → ``webhook_delivery.id`` ``ON DELETE
  SET NULL``. Carries the audit chain for manual replays.
* ``created_at`` — wall-clock of the enqueue call.

Indexes:

* ``ix_webhook_subscription_workspace`` — tenant filter.
* ``ix_webhook_subscription_workspace_active`` — dispatcher fan-out
  scopes to ``WHERE workspace_id = ? AND active = TRUE``.
* ``ix_webhook_delivery_workspace`` — tenant filter.
* ``ix_webhook_delivery_next_attempt`` — dispatcher wakeup hot path
  (``WHERE next_attempt_at <= now AND status = 'pending'``).
* ``ix_webhook_delivery_subscription`` — /webhooks/{id} drill-down.

**Reversibility.** ``downgrade()`` drops the indexes + the tables.
Any rows are lost; an operator running a real rollback should drain
in-flight deliveries first or accept that they vanish (replay surface
won't be able to re-mint them post-rollback). The ``secret_envelope``
rows referenced by ``secret_blob`` are NOT cascaded — they live in a
sibling table (cd-znv4) and are swept by the rotation worker; a
rollback that drops the subscription rows leaves orphan envelope rows
that can be cleaned up by hand.

See ``docs/specs/02-domain-model.md`` §"webhook_subscription" /
§"webhook_delivery", ``docs/specs/10-messaging-notifications.md``
§"Webhooks (outbound)".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c0d3e6f9a2b5"
down_revision: str | Sequence[str] | None = "b9c2d5e8f1a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DELIVERY_STATUS_VALUES: tuple[str, ...] = (
    "pending",
    "in_flight",
    "succeeded",
    "dead_lettered",
)


def _in_clause(values: tuple[str, ...]) -> str:
    return "'" + "', '".join(values) + "'"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "webhook_subscription",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("secret_blob", sa.String(), nullable=False),
        sa.Column("secret_last_4", sa.String(), nullable=False),
        sa.Column("events_json", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_webhook_subscription_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_subscription")),
    )
    with op.batch_alter_table("webhook_subscription", schema=None) as batch_op:
        batch_op.create_index(
            "ix_webhook_subscription_workspace",
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_webhook_subscription_workspace_active",
            ["workspace_id", "active"],
            unique=False,
        )

    op.create_table(
        "webhook_delivery",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("subscription_id", sa.String(), nullable=False),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replayed_from_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"status IN ({_in_clause(_DELIVERY_STATUS_VALUES)})",
            name=op.f("ck_webhook_delivery_status"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_webhook_delivery_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["webhook_subscription.id"],
            name=op.f("fk_webhook_delivery_subscription_id_webhook_subscription"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["replayed_from_id"],
            ["webhook_delivery.id"],
            name=op.f("fk_webhook_delivery_replayed_from_id_webhook_delivery"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_delivery")),
    )
    with op.batch_alter_table("webhook_delivery", schema=None) as batch_op:
        batch_op.create_index(
            "ix_webhook_delivery_workspace",
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_webhook_delivery_next_attempt",
            ["next_attempt_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_webhook_delivery_subscription",
            ["subscription_id"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("webhook_delivery", schema=None) as batch_op:
        batch_op.drop_index("ix_webhook_delivery_subscription")
        batch_op.drop_index("ix_webhook_delivery_next_attempt")
        batch_op.drop_index("ix_webhook_delivery_workspace")

    op.drop_table("webhook_delivery")

    with op.batch_alter_table("webhook_subscription", schema=None) as batch_op:
        batch_op.drop_index("ix_webhook_subscription_workspace_active")
        batch_op.drop_index("ix_webhook_subscription_workspace")

    op.drop_table("webhook_subscription")
