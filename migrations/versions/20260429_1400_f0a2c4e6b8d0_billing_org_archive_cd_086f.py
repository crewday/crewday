"""billing organization archive cd-086f

Revision ID: f0a2c4e6b8d0
Revises: e9f1a3b5d7c9
Create Date: 2026-04-29 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f0a2c4e6b8d0"
down_revision: str | Sequence[str] | None = "e9f1a3b5d7c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("organization", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(
            "ix_organization_workspace_archived",
            ["workspace_id", "archived_at"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("organization", schema=None) as batch_op:
        batch_op.drop_index("ix_organization_workspace_archived")
        batch_op.drop_column("archived_at")
