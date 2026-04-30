"""shift source occurrence cd-1ak

Revision ID: c2e4f6a8d0b2
Revises: b1d3f5a7c9e2
Create Date: 2026-04-30 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2e4f6a8d0b2"
down_revision: str | Sequence[str] | None = "b1d3f5a7c9e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("task_template", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "auto_shift_from_occurrence",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    with op.batch_alter_table("property_workspace", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "auto_shift_from_occurrence",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    with op.batch_alter_table("shift", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("source_occurrence_id", sa.String(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_shift_source_occurrence_id_occurrence",
            "occurrence",
            ["source_occurrence_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "ix_shift_source_occurrence",
            ["workspace_id", "source_occurrence_id"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("shift", schema=None) as batch_op:
        batch_op.drop_index("ix_shift_source_occurrence")
        batch_op.drop_constraint(
            "fk_shift_source_occurrence_id_occurrence",
            type_="foreignkey",
        )
        batch_op.drop_column("source_occurrence_id")

    with op.batch_alter_table("property_workspace", schema=None) as batch_op:
        batch_op.drop_column("auto_shift_from_occurrence")

    with op.batch_alter_table("task_template", schema=None) as batch_op:
        batch_op.drop_column("auto_shift_from_occurrence")
