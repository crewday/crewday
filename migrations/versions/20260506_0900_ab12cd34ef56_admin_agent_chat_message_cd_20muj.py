"""admin agent chat message cd-20muj

Revision ID: ab12cd34ef56
Revises: a0c2e4f6a8b1
Create Date: 2026-05-06 09:00:00.000000

Adds deployment-scoped transcript storage for the `/admin` embedded
agent. The workspace chat tables require a workspace id, so admin
agent messages need their own deployment-wide table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ab12cd34ef56"
down_revision: str | Sequence[str] | None = "a0c2e4f6a8b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create deployment-admin agent transcript storage."""
    op.create_table(
        "admin_agent_chat_message",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("admin_user_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("body_md", sa.String(), nullable=False),
        sa.Column("page_context", sa.String(), nullable=False),
        sa.Column("author_label", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["admin_user_id"],
            ["user.id"],
            name=op.f("fk_admin_agent_chat_message_admin_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_agent_chat_message")),
    )
    op.create_index(
        "ix_admin_agent_chat_message_user_created",
        "admin_agent_chat_message",
        ["admin_user_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop deployment-admin agent transcript storage."""
    op.drop_index(
        "ix_admin_agent_chat_message_user_created",
        table_name="admin_agent_chat_message",
    )
    op.drop_table("admin_agent_chat_message")
