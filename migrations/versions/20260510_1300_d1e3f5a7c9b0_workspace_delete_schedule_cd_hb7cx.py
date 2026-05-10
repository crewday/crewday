"""workspace delete scheduling cd-hb7cx

Revision ID: d1e3f5a7c9b0
Revises: cd1k20r1200
Create Date: 2026-05-10 13:00:00.000000

Stores owner-requested workspace deletion separately from ordinary
archive state so the purge job can hard-delete only rows whose
14-day grace period has elapsed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1e3f5a7c9b0"
down_revision: str | Sequence[str] | None = "cd1k20r1200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add workspace deletion scheduling columns."""
    with op.batch_alter_table("workspace", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("delete_requested_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index("ix_workspace_purge_after", ["purge_after"])


def downgrade() -> None:
    """Drop workspace deletion scheduling columns."""
    with op.batch_alter_table("workspace", schema=None) as batch_op:
        batch_op.drop_index("ix_workspace_purge_after")
        batch_op.drop_column("purge_after")
        batch_op.drop_column("delete_requested_at")
