"""chat channel bindings cd-if6t

Revision ID: e8b0c2d4f6a9
Revises: d1f3a5c7e9b1
Create Date: 2026-05-01 06:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8b0c2d4f6a9"
down_revision: str | Sequence[str] | None = "d1f3a5c7e9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_channel_binding",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("channel_kind", sa.String(), nullable=False),
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("address_hash", sa.String(), nullable=False),
        sa.Column("display_label", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "provider_metadata_json",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.CheckConstraint(
            "channel_kind IN ('offapp_whatsapp', 'offapp_telegram')",
            name=op.f("ck_chat_channel_binding_chat_channel_binding_channel_kind"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'active', 'revoked')",
            name=op.f("ck_chat_channel_binding_chat_channel_binding_state"),
        ),
        sa.CheckConstraint(
            "revoke_reason IS NULL OR revoke_reason IN "
            "('user', 'stop_keyword', 'user_archived', 'admin', 'provider_error')",
            name=op.f("ck_chat_channel_binding_chat_channel_binding_revoke_reason"),
        ),
        sa.CheckConstraint(
            "(state = 'active' AND verified_at IS NOT NULL) OR state != 'active'",
            name=op.f("ck_chat_channel_binding_chat_channel_binding_active_verified"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_chat_channel_binding_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_chat_channel_binding_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_channel_binding")),
    )
    with op.batch_alter_table("chat_channel_binding", schema=None) as batch_op:
        batch_op.create_index(
            "uq_chat_channel_binding_workspace_kind_address_active",
            ["workspace_id", "channel_kind", "address_hash"],
            unique=True,
            sqlite_where=sa.text("state != 'revoked'"),
            postgresql_where=sa.text("state != 'revoked'"),
        )
        batch_op.create_index(
            "uq_chat_channel_binding_workspace_user_kind_active",
            ["workspace_id", "user_id", "channel_kind"],
            unique=True,
            sqlite_where=sa.text("state != 'revoked'"),
            postgresql_where=sa.text("state != 'revoked'"),
        )
        batch_op.create_index(
            "ix_chat_channel_binding_workspace_user",
            ["workspace_id", "user_id"],
            unique=False,
        )

    op.create_table(
        "chat_link_challenge",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("binding_id", sa.String(), nullable=False),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("code_hash_params", sa.String(), nullable=False),
        sa.Column("sent_via", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sent_via IN ('channel', 'email')",
            name=op.f("ck_chat_link_challenge_sent_via"),
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_chat_link_challenge_attempts_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["binding_id"],
            ["chat_channel_binding.id"],
            name=op.f("fk_chat_link_challenge_binding_id_chat_channel_binding"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_link_challenge")),
    )
    with op.batch_alter_table("chat_link_challenge", schema=None) as batch_op:
        batch_op.create_index(
            "ix_chat_link_challenge_binding",
            ["binding_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("chat_link_challenge", schema=None) as batch_op:
        batch_op.drop_index("ix_chat_link_challenge_binding")
    op.drop_table("chat_link_challenge")

    with op.batch_alter_table("chat_channel_binding", schema=None) as batch_op:
        batch_op.drop_index("ix_chat_channel_binding_workspace_user")
        batch_op.drop_index("uq_chat_channel_binding_workspace_user_kind_active")
        batch_op.drop_index("uq_chat_channel_binding_workspace_kind_address_active")
    op.drop_table("chat_channel_binding")
