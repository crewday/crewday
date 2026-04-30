"""notification_push_queue cd-y60x

Revision ID: b7d9f1a3c5e7
Revises: a6c8e0f2b4d6
Create Date: 2026-04-30 14:00:00.000000

Adds the ``notification_push_queue`` staging table consumed by the §10
web-push delivery worker (cd-y60x). One row per ``(notification,
push_token)`` pair; the worker walks rows whose ``status='pending'``
and ``next_attempt_at <= now``, fires a ``pywebpush`` send, and stamps
the row with the outcome. Backoff schedule per spec
``docs/specs/10-messaging-notifications.md`` §"Channels" (web push):
``[30s, 2m, 10m, 1h]``; rows dead-letter after 5 attempts.

The row is workspace-scoped — ``workspace_id`` carries the tenant
filter even though the worker tick reads under ``tenant_agnostic``
(cross-tenant by design, like the webhook dispatcher). FK hygiene:

* ``workspace_id`` ``CASCADE`` — sweeping a workspace sweeps its push
  queue.
* ``notification_id`` ``CASCADE`` — the queue row has no meaning
  without the notification it announces.
* ``push_token_id`` ``CASCADE`` — a deleted token's pending pushes
  can't deliver anyway; cascade keeps the table tight.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d9f1a3c5e7"
down_revision: str | Sequence[str] | None = "a6c8e0f2b4d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STATUS_VALUES = "'pending', 'in_flight', 'sent', 'dead_lettered'"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "notification_push_queue",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("notification_id", sa.String(), nullable=False),
        sa.Column("push_token_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_notification_push_queue_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notification.id"],
            name=op.f("fk_notification_push_queue_notification_id_notification"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["push_token_id"],
            ["push_token.id"],
            name=op.f("fk_notification_push_queue_push_token_id_push_token"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_push_queue")),
        sa.CheckConstraint(
            f"status IN ({_STATUS_VALUES})",
            name=op.f("ck_notification_push_queue_status"),
        ),
    )
    with op.batch_alter_table("notification_push_queue", schema=None) as batch_op:
        batch_op.create_index(
            "ix_notification_push_queue_workspace",
            ["workspace_id"],
            unique=False,
        )
        # Worker hot path: pending rows whose retry window has opened.
        batch_op.create_index(
            "ix_notification_push_queue_status_next_attempt",
            ["status", "next_attempt_at"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("notification_push_queue", schema=None) as batch_op:
        batch_op.drop_index("ix_notification_push_queue_status_next_attempt")
        batch_op.drop_index("ix_notification_push_queue_workspace")

    op.drop_table("notification_push_queue")
