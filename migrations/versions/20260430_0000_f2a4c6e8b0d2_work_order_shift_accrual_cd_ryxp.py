"""work order shift accrual cd-ryxp

Revision ID: f2a4c6e8b0d2
Revises: e1f3a5c7d9b1
Create Date: 2026-04-30 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a4c6e8b0d2"
down_revision: str | Sequence[str] | None = "e1f3a5c7d9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("work_order", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_work_order_id_workspace",
            ["id", "workspace_id"],
        )
    op.create_table(
        "work_order_shift_accrual",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("work_order_id", sa.String(), nullable=False),
        sa.Column("shift_id", sa.String(), nullable=False),
        sa.Column(
            "hours_decimal",
            sa.Numeric(precision=10, scale=2),
            nullable=False,
        ),
        sa.Column("hourly_rate_cents", sa.BigInteger(), nullable=False),
        sa.Column("accrued_cents", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "accrued_cents >= 0",
            name=op.f("ck_work_order_shift_accrual_accrued_cents_nonneg"),
        ),
        sa.CheckConstraint(
            "hourly_rate_cents > 0",
            name=op.f("ck_work_order_shift_accrual_hourly_rate_cents_positive"),
        ),
        sa.CheckConstraint(
            "hours_decimal >= 0",
            name=op.f("ck_work_order_shift_accrual_hours_decimal_nonneg"),
        ),
        sa.ForeignKeyConstraint(
            ["work_order_id", "workspace_id"],
            ["work_order.id", "work_order.workspace_id"],
            name=op.f("fk_work_order_shift_accrual_work_order_id_work_order"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_work_order_shift_accrual_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_order_shift_accrual")),
        sa.UniqueConstraint(
            "workspace_id",
            "shift_id",
            name=op.f("uq_work_order_shift_accrual_workspace_shift"),
        ),
    )
    with op.batch_alter_table("work_order_shift_accrual", schema=None) as batch_op:
        batch_op.create_index(
            "ix_work_order_shift_accrual_work_order",
            ["workspace_id", "work_order_id"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("work_order_shift_accrual", schema=None) as batch_op:
        batch_op.drop_index("ix_work_order_shift_accrual_work_order")
    op.drop_table("work_order_shift_accrual")
    with op.batch_alter_table("work_order", schema=None) as batch_op:
        batch_op.drop_constraint("uq_work_order_id_workspace", type_="unique")
