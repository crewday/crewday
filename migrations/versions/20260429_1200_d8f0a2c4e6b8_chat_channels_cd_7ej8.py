"""chat_channels_cd_7ej8

Revision ID: d8f0a2c4e6b8
Revises: c7e9f1a3b5d7
Create Date: 2026-04-29 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8f0a2c4e6b8"
down_revision: str | Sequence[str] | None = "c7e9f1a3b5d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("chat_channel", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
        )

    op.create_table(
        "chat_channel_member",
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["chat_channel.id"],
            name=op.f("fk_chat_channel_member_channel_id_chat_channel"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_chat_channel_member_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_chat_channel_member_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "channel_id",
            "user_id",
            name=op.f("pk_chat_channel_member"),
        ),
    )
    with op.batch_alter_table("chat_channel_member", schema=None) as batch_op:
        batch_op.create_index(
            "ix_chat_channel_member_workspace_user",
            ["workspace_id", "user_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("chat_channel_member", schema=None) as batch_op:
        batch_op.drop_index("ix_chat_channel_member_workspace_user")
    op.drop_table("chat_channel_member")

    with op.batch_alter_table("chat_channel", schema=None) as batch_op:
        batch_op.drop_column("archived_at")
