"""messaging_cd_pjm

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-24 12:00:00.000000

Creates the five messaging + notifications tables that back the §10
fanout, the §12 ``/me/push-tokens`` surface (returns ``501
push_unavailable`` in v1 until the native-app project ships — the
schema is reserved so the worker and REST layer can land ahead of
the first live delivery), the daily / weekly email-digest worker,
and the §23 chat-gateway substrate.

Tables:

* ``notification`` — the in-app notification fanout primary source.
  ``(workspace_id, recipient_user_id, read_at)`` index powers the
  bell-menu "unread count" hot path. ``kind`` CHECK clamps the v1
  notification taxonomy (``task_assigned``, ``task_overdue``,
  ``expense_approved``, ``approval_needed``, …); widening is
  additive. ``payload_json`` snapshots the renderer context so a
  notification survives the underlying row's evolution. FK: both
  ``workspace_id`` and ``recipient_user_id`` CASCADE.

* ``push_token`` — a browser / future native-app Web-Push
  subscription. ``(workspace_id, user_id)`` index powers the
  per-user fanout the §10 delivery worker walks. ``last_used_at``
  drives the 60-day freshness window; stale tokens are skipped
  silently by the fanout. FK: both ``workspace_id`` and ``user_id``
  CASCADE.

* ``digest_record`` — daily / weekly email-digest send ledger. One
  row per digest actually emitted (written after the SMTP send
  returns ``250 OK``). ``kind`` CHECK clamps the cadence;
  ``period_end > period_start`` CHECK guards against inverted
  windows. ``(workspace_id, recipient_user_id, period_start)``
  index is the idempotency-probe hot path. FK: both ``workspace_
  id`` and ``recipient_user_id`` CASCADE.

* ``chat_channel`` — a conversation thread. ``kind`` CHECK clamps
  the v1 channel taxonomy (``staff | manager | chat_gateway``);
  ``source`` CHECK clamps the transport (``app | whatsapp | sms |
  email``). ``external_ref`` is a plain ``String`` soft-ref for
  opaque per-provider ids. Two indexes: ``(workspace_id)`` for
  the list surface, ``(workspace_id, external_ref)`` for the
  gateway inbound router. FK: ``workspace_id`` CASCADE.

* ``chat_message`` — a single turn in a ``chat_channel``. Two
  indexes: ``(channel_id, created_at)`` for scrollback,
  ``(workspace_id, channel_id)`` for scoped cross-channel
  queries. ``author_user_id`` FK uses ``SET NULL`` so history
  survives an author's hard-delete; nullable on the schema side
  so gateway-inbound rows land with no author.
  ``channel_id`` FK CASCADE so deleting a channel sweeps its
  messages; ``workspace_id`` CASCADE per the tenancy convention.
  ``attachments_json`` is the JSON payload list matching the
  ``comment.attachments_json`` shape. ``dispatched_to_agent_at``
  is NULL on in-app + outbound rows; populated on gateway-inbound
  once the dispatcher handoff completes.

All five tables are workspace-scoped (registered via
``app/adapters/db/messaging/__init__.py``). Tables are created in FK
dependency order (``chat_channel → chat_message``;
``notification`` / ``push_token`` / ``digest_record`` are sibling
tables without cross-FKs). ``downgrade()`` drops in reverse.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ``notification`` — the in-app notification fanout row.
    op.create_table(
        "notification",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("recipient_user_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body_md", sa.String(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload_json",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.CheckConstraint(
            "kind IN ('task_assigned', 'task_overdue', 'expense_approved', "
            "'expense_rejected', 'expense_submitted', 'approval_needed', "
            "'approval_decided', 'issue_reported', 'issue_resolved', "
            "'comment_mention', 'payslip_issued', 'stay_upcoming', "
            "'anomaly_detected', 'agent_message')",
            name=op.f("ck_notification_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["recipient_user_id"],
            ["user.id"],
            name=op.f("fk_notification_recipient_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_notification_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification")),
    )
    with op.batch_alter_table("notification", schema=None) as batch_op:
        batch_op.create_index(
            "ix_notification_workspace_recipient_read",
            ["workspace_id", "recipient_user_id", "read_at"],
            unique=False,
        )

    # ``push_token`` — web-push subscription row.
    op.create_table(
        "push_token",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("p256dh", sa.String(), nullable=False),
        sa.Column("auth", sa.String(), nullable=False),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_push_token_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_push_token_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_push_token")),
    )
    with op.batch_alter_table("push_token", schema=None) as batch_op:
        batch_op.create_index(
            "ix_push_token_workspace_user",
            ["workspace_id", "user_id"],
            unique=False,
        )

    # ``digest_record`` — daily / weekly email-digest ledger.
    op.create_table(
        "digest_record",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("recipient_user_id", sa.String(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("body_md", sa.String(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('daily', 'weekly')",
            name=op.f("ck_digest_record_kind"),
        ),
        sa.CheckConstraint(
            "period_end > period_start",
            name=op.f("ck_digest_record_period_end_after_start"),
        ),
        sa.ForeignKeyConstraint(
            ["recipient_user_id"],
            ["user.id"],
            name=op.f("fk_digest_record_recipient_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_digest_record_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_digest_record")),
    )
    with op.batch_alter_table("digest_record", schema=None) as batch_op:
        batch_op.create_index(
            "ix_digest_record_workspace_recipient_period",
            ["workspace_id", "recipient_user_id", "period_start"],
            unique=False,
        )

    # ``chat_channel`` — parent row for chat messages.
    op.create_table(
        "chat_channel",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("external_ref", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('staff', 'manager', 'chat_gateway')",
            name=op.f("ck_chat_channel_kind"),
        ),
        sa.CheckConstraint(
            "source IN ('app', 'whatsapp', 'sms', 'email')",
            name=op.f("ck_chat_channel_source"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_chat_channel_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_channel")),
    )
    with op.batch_alter_table("chat_channel", schema=None) as batch_op:
        batch_op.create_index(
            "ix_chat_channel_workspace",
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_chat_channel_workspace_external_ref",
            ["workspace_id", "external_ref"],
            unique=False,
        )

    # ``chat_message`` — must come after chat_channel (FK).
    op.create_table(
        "chat_message",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("author_user_id", sa.String(), nullable=True),
        sa.Column("author_label", sa.String(), nullable=False),
        sa.Column("body_md", sa.String(), nullable=False),
        sa.Column(
            "attachments_json",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "dispatched_to_agent_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["user.id"],
            name=op.f("fk_chat_message_author_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["chat_channel.id"],
            name=op.f("fk_chat_message_channel_id_chat_channel"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_chat_message_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_message")),
    )
    with op.batch_alter_table("chat_message", schema=None) as batch_op:
        batch_op.create_index(
            "ix_chat_message_channel_created",
            ["channel_id", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_chat_message_workspace_channel",
            ["workspace_id", "channel_id"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("chat_message", schema=None) as batch_op:
        batch_op.drop_index("ix_chat_message_workspace_channel")
        batch_op.drop_index("ix_chat_message_channel_created")
    op.drop_table("chat_message")

    with op.batch_alter_table("chat_channel", schema=None) as batch_op:
        batch_op.drop_index("ix_chat_channel_workspace_external_ref")
        batch_op.drop_index("ix_chat_channel_workspace")
    op.drop_table("chat_channel")

    with op.batch_alter_table("digest_record", schema=None) as batch_op:
        batch_op.drop_index("ix_digest_record_workspace_recipient_period")
    op.drop_table("digest_record")

    with op.batch_alter_table("push_token", schema=None) as batch_op:
        batch_op.drop_index("ix_push_token_workspace_user")
    op.drop_table("push_token")

    with op.batch_alter_table("notification", schema=None) as batch_op:
        batch_op.drop_index("ix_notification_workspace_recipient_read")
    op.drop_table("notification")
