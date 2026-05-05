"""expense claim pay-period attachment cd-kcnnz

Revision ID: f9b1c3d5e7a9
Revises: e8a0b2c4d6f0
Create Date: 2026-05-05 11:00:00.000000

Adds ``expense_claim.pay_period_id`` so expense approval records the
period that will reimburse the claim. Existing rows stay NULL and the
payroll rollup keeps the previous purchased-date fallback for them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9b1c3d5e7a9"
down_revision: str | Sequence[str] | None = "e8a0b2c4d6f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add explicit reimbursement pay-period attachment to claims."""
    with op.batch_alter_table("expense_claim", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pay_period_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_expense_claim_pay_period_id_pay_period"),
            "pay_period",
            ["pay_period_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            op.f("ix_expense_claim_workspace_pay_period"),
            ["workspace_id", "pay_period_id"],
            unique=False,
        )


def downgrade() -> None:
    """Drop explicit reimbursement pay-period attachment from claims."""
    with op.batch_alter_table("expense_claim", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_expense_claim_workspace_pay_period"))
        batch_op.drop_constraint(
            op.f("fk_expense_claim_pay_period_id_pay_period"),
            type_="foreignkey",
        )
        batch_op.drop_column("pay_period_id")
