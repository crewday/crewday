"""payslip expense reimbursements cd-f6inl

Revision ID: c2d4e6f8a1b3
Revises: b1c3d5e7f902
Create Date: 2026-05-01 11:00:00.000000

Adds the ``expense_reimbursements_cents`` column to ``payslip`` so the
period-close compute can fold approved expense claims into the
authoritative ``net_cents`` per spec §09 §"Payslip". The column carries
``Integer`` (matches ``deductions_cents`` aggregate convention rather
than ``BigInteger`` — a single payslip's reimbursement total fits well
inside INT32) and is non-NULL with a server-side default of ``0`` so
existing rows backfill cleanly without rewriting them.

The per-claim breakdown lives in ``components_json["reimbursements"]``;
the column carries the aggregate that the API + payout manifest read in
hot paths.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2d4e6f8a1b3"
down_revision: str | Sequence[str] | None = "b1c3d5e7f902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``expense_reimbursements_cents`` to ``payslip``."""
    with op.batch_alter_table("payslip", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "expense_reimbursements_cents",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )

    with op.batch_alter_table("payslip", schema=None) as batch_op:
        batch_op.alter_column(
            "expense_reimbursements_cents", server_default=None
        )


def downgrade() -> None:
    """Drop ``expense_reimbursements_cents``."""
    with op.batch_alter_table("payslip", schema=None) as batch_op:
        batch_op.drop_column("expense_reimbursements_cents")
