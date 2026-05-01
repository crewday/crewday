"""task completion tombstone cd-gag2

Revision ID: a8c0e2f4b6d8
Revises: f7a9c1e3d5b7
Create Date: 2026-05-01 02:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8c0e2f4b6d8"
down_revision: str | Sequence[str] | None = "f7a9c1e3d5b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "task_completion",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("occurrence_id", sa.String(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_by_user_id", sa.String(), nullable=True),
        sa.Column("completion_note_md", sa.Text(), nullable=True),
        sa.Column("evidence_blob_hashes", sa.JSON(), nullable=False),
        sa.Column("checklist_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["completed_by_user_id"],
            ["user.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["occurrence_id"],
            ["occurrence.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_task_completion_workspace_occurrence_created",
        "task_completion",
        ["workspace_id", "occurrence_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_task_completion_workspace_occurrence_created",
        table_name="task_completion",
    )
    op.drop_table("task_completion")
