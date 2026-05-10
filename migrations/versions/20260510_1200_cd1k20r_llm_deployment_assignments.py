"""llm deployment-level assignments cd-1k20r

Revision ID: cd1k20r1200
Revises: bc23de45fa67
Create Date: 2026-05-10 12:00:00.000000

Moves LLM assignment and capability-inheritance definitions out of the
workspace override layer. The legacy ``workspace_id`` columns are kept
nullable as transitional columns, but active rows are represented with
``workspace_id IS NULL`` and indexed by deployment-level keys.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cd1k20r1200"
down_revision: str | Sequence[str] | None = "bc23de45fa67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Collapse legacy per-workspace LLM config rows to deployment rows."""
    with op.batch_alter_table("llm_assignment", schema=None) as batch_op:
        batch_op.drop_index("ix_llm_assignment_workspace_capability_priority")
        batch_op.drop_constraint(
            "fk_llm_assignment_workspace_id_workspace", type_="foreignkey"
        )
        batch_op.alter_column("workspace_id", existing_type=sa.String(), nullable=True)

    with op.batch_alter_table("llm_capability_inheritance", schema=None) as batch_op:
        batch_op.drop_index("uq_llm_capability_inheritance_workspace_capability")
        batch_op.drop_constraint(
            "fk_llm_capability_inheritance_workspace_id_workspace",
            type_="foreignkey",
        )
        batch_op.alter_column("workspace_id", existing_type=sa.String(), nullable=True)

    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            DELETE FROM llm_assignment
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM llm_assignment
                GROUP BY capability, priority
            )
            """
        )
    )
    bind.execute(sa.text("UPDATE llm_assignment SET workspace_id = NULL"))
    bind.execute(
        sa.text(
            """
            DELETE FROM llm_capability_inheritance
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM llm_capability_inheritance
                GROUP BY capability
            )
            """
        )
    )
    bind.execute(sa.text("UPDATE llm_capability_inheritance SET workspace_id = NULL"))

    with op.batch_alter_table("llm_assignment", schema=None) as batch_op:
        batch_op.create_index(
            "ix_llm_assignment_capability_priority",
            ["capability", "priority"],
            unique=False,
        )

    with op.batch_alter_table("llm_capability_inheritance", schema=None) as batch_op:
        batch_op.create_index(
            "uq_llm_capability_inheritance_capability",
            ["capability"],
            unique=True,
        )


def downgrade() -> None:
    """Restore the legacy per-workspace schema shape."""
    bind = op.get_bind()
    fallback_workspace = bind.execute(
        sa.text("SELECT id FROM workspace ORDER BY id LIMIT 1")
    ).scalar()

    with op.batch_alter_table("llm_assignment", schema=None) as batch_op:
        batch_op.drop_index("ix_llm_assignment_capability_priority")
    with op.batch_alter_table("llm_capability_inheritance", schema=None) as batch_op:
        batch_op.drop_index("uq_llm_capability_inheritance_capability")

    if fallback_workspace is None:
        bind.execute(sa.text("DELETE FROM llm_assignment WHERE workspace_id IS NULL"))
        bind.execute(
            sa.text("DELETE FROM llm_capability_inheritance WHERE workspace_id IS NULL")
        )
    else:
        bind.execute(
            sa.text(
                "UPDATE llm_assignment "
                "SET workspace_id = :workspace_id "
                "WHERE workspace_id IS NULL"
            ),
            {"workspace_id": fallback_workspace},
        )
        bind.execute(
            sa.text(
                "UPDATE llm_capability_inheritance "
                "SET workspace_id = :workspace_id "
                "WHERE workspace_id IS NULL"
            ),
            {"workspace_id": fallback_workspace},
        )

    with op.batch_alter_table("llm_assignment", schema=None) as batch_op:
        batch_op.alter_column("workspace_id", existing_type=sa.String(), nullable=False)
        batch_op.create_foreign_key(
            "fk_llm_assignment_workspace_id_workspace",
            "workspace",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_llm_assignment_workspace_capability_priority",
            ["workspace_id", "capability", "priority"],
            unique=False,
        )

    with op.batch_alter_table("llm_capability_inheritance", schema=None) as batch_op:
        batch_op.alter_column("workspace_id", existing_type=sa.String(), nullable=False)
        batch_op.create_foreign_key(
            "fk_llm_capability_inheritance_workspace_id_workspace",
            "workspace",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "uq_llm_capability_inheritance_workspace_capability",
            ["workspace_id", "capability"],
            unique=True,
        )
