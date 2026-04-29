"""task_approvals_cd_z2py

Revision ID: a7c9e1f3b5d8
Revises: f6a8c0e2b4d6
Create Date: 2026-04-29 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c9e1f3b5d8"
down_revision: str | Sequence[str] | None = "f6a8c0e2b4d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TASK_APPROVAL_STATE_VALUES: tuple[str, ...] = (
    "pending",
    "approved",
    "rejected",
    "changes_requested",
)


def _in_clause(values: tuple[str, ...]) -> str:
    return "'" + "', '".join(values) + "'"


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("task_template", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "required_approval",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    op.create_table(
        "task_approval",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_by_user_id", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", sa.String(), nullable=True),
        sa.Column("note_md", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"state IN ({_in_clause(_TASK_APPROVAL_STATE_VALUES)})",
            name=op.f("ck_task_approval_state"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_task_approval_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["occurrence.id"],
            name=op.f("fk_task_approval_task_id_occurrence"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user.id"],
            name=op.f("fk_task_approval_requested_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["user.id"],
            name=op.f("fk_task_approval_decided_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_approval")),
    )
    op.create_index(
        "ix_task_approval_workspace_state",
        "task_approval",
        ["workspace_id", "state"],
        unique=False,
    )
    op.create_index(
        "ix_task_approval_task",
        "task_approval",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        "uq_task_approval_open_task",
        "task_approval",
        ["task_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('pending', 'changes_requested')"),
        postgresql_where=sa.text("state IN ('pending', 'changes_requested')"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_task_approval_open_task", table_name="task_approval")
    op.drop_index("ix_task_approval_task", table_name="task_approval")
    op.drop_index("ix_task_approval_workspace_state", table_name="task_approval")
    op.drop_table("task_approval")

    with op.batch_alter_table("task_template", schema=None) as batch_op:
        batch_op.drop_column("required_approval")
