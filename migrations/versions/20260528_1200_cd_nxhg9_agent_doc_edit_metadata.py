"""agent doc editable metadata cd-nxhg9

Revision ID: cdnxhg9agentdoc
Revises: cdollamaprovider
Create Date: 2026-05-28 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cdnxhg9agentdoc"
down_revision: str | Sequence[str] | None = "cdollamaprovider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("agent_doc", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "metadata_default_hash",
                sa.String(length=16),
                nullable=False,
                server_default="",
            )
        )

    with op.batch_alter_table("agent_doc_revision", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("roles", sa.JSON(), nullable=False, server_default="[]")
        )
    op.execute(
        sa.text(
            "UPDATE agent_doc_revision "
            "SET roles = ("
            "SELECT agent_doc.roles "
            "FROM agent_doc "
            "WHERE agent_doc.id = agent_doc_revision.doc_id"
            ")"
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("agent_doc_revision", schema=None) as batch_op:
        batch_op.drop_column("roles")

    with op.batch_alter_table("agent_doc", schema=None) as batch_op:
        batch_op.drop_column("metadata_default_hash")
