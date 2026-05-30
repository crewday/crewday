"""instruction property scope cd-dznzk

Revision ID: cddznzkmultiprop
Revises: cdnxhg9agentdoc
Create Date: 2026-05-30 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cddznzkmultiprop"
down_revision: str | Sequence[str] | None = "cdnxhg9agentdoc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "instruction_property_scope",
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("instruction_id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instruction_id"],
            ["instruction.id"],
            name=op.f("fk_instruction_property_scope_instruction_id_instruction"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_instruction_property_scope_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "instruction_id",
            "property_id",
            name=op.f("pk_instruction_property_scope"),
        ),
    )
    with op.batch_alter_table("instruction_property_scope", schema=None) as batch_op:
        batch_op.create_index(
            "ix_instruction_property_scope_workspace_property",
            ["workspace_id", "property_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_instruction_property_scope_workspace_instruction",
            ["workspace_id", "instruction_id"],
            unique=False,
        )

    op.execute(
        sa.text(
            "INSERT INTO instruction_property_scope "
            "(workspace_id, instruction_id, property_id, created_at) "
            "SELECT workspace_id, id, scope_id, created_at "
            "FROM instruction "
            "WHERE scope_kind = 'property' AND scope_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("instruction_property_scope", schema=None) as batch_op:
        batch_op.drop_index("ix_instruction_property_scope_workspace_instruction")
        batch_op.drop_index("ix_instruction_property_scope_workspace_property")
    op.drop_table("instruction_property_scope")
