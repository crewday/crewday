"""instruction_link cd-q3b

Revision ID: e2f4a6c8d0b2
Revises: d0610bb1347c
Create Date: 2026-04-30 15:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2f4a6c8d0b2"
down_revision: str | Sequence[str] | None = "d0610bb1347c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "instruction_link",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("instruction_id", sa.String(), nullable=False),
        sa.Column("target_kind", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("added_by", sa.String(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_kind IN ('task_template', 'schedule', 'work_role', "
            "'task', 'asset', 'stay')",
            name=op.f("ck_instruction_link_target_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["instruction_id"],
            ["instruction.id"],
            name=op.f("fk_instruction_link_instruction_id_instruction"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_instruction_link_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instruction_link")),
        sa.UniqueConstraint(
            "workspace_id",
            "instruction_id",
            "target_kind",
            "target_id",
            name="uq_instruction_link_workspace_instruction_target",
        ),
    )
    with op.batch_alter_table("instruction_link", schema=None) as batch_op:
        batch_op.create_index(
            "ix_instruction_link_workspace_target",
            ["workspace_id", "target_kind", "target_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_instruction_link_workspace_instruction",
            ["workspace_id", "instruction_id"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("instruction_link", schema=None) as batch_op:
        batch_op.drop_index("ix_instruction_link_workspace_instruction")
        batch_op.drop_index("ix_instruction_link_workspace_target")
    op.drop_table("instruction_link")
