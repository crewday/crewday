"""chat compaction columns cd-cn7v

Revision ID: d5f7a9c1e3b5
Revises: c4e6a8b0d2f4
Create Date: 2026-04-30 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f7a9c1e3b5"
down_revision: str | Sequence[str] | None = "c4e6a8b0d2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("chat_message", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.String(),
                nullable=False,
                server_default="message",
            )
        )
        batch_op.add_column(sa.Column("summary_range_from_id", sa.String()))
        batch_op.add_column(sa.Column("summary_range_to_id", sa.String()))
        batch_op.add_column(sa.Column("compacted_into_id", sa.String()))
        batch_op.create_foreign_key(
            "fk_chat_message_summary_range_from_id_chat_message",
            "chat_message",
            ["summary_range_from_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_chat_message_summary_range_to_id_chat_message",
            "chat_message",
            ["summary_range_to_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_chat_message_compacted_into_id_chat_message",
            "chat_message",
            ["compacted_into_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "uq_chat_message_live_summary",
            ["channel_id"],
            unique=True,
            sqlite_where=sa.text("kind = 'summary' AND compacted_into_id IS NULL"),
            postgresql_where=sa.text("kind = 'summary' AND compacted_into_id IS NULL"),
        )
        batch_op.create_index(
            "ix_chat_message_compacted_into",
            ["compacted_into_id"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("chat_message", schema=None) as batch_op:
        batch_op.drop_index("ix_chat_message_compacted_into")
        batch_op.drop_index("uq_chat_message_live_summary")
        batch_op.drop_constraint(
            "fk_chat_message_compacted_into_id_chat_message",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_chat_message_summary_range_to_id_chat_message",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_chat_message_summary_range_from_id_chat_message",
            type_="foreignkey",
        )
        batch_op.drop_column("compacted_into_id")
        batch_op.drop_column("summary_range_to_id")
        batch_op.drop_column("summary_range_from_id")
        batch_op.drop_column("kind")
