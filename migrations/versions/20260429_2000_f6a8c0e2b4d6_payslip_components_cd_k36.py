"""payslip components json cd-k36

Revision ID: f6a8c0e2b4d6
Revises: e5f7a9c1d3e6
Create Date: 2026-04-29 20:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a8c0e2b4d6"
down_revision: str | Sequence[str] | None = "e5f7a9c1d3e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("payslip", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "components_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )

    with op.batch_alter_table("payslip", schema=None) as batch_op:
        batch_op.alter_column("components_json", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("payslip", schema=None) as batch_op:
        batch_op.drop_column("components_json")
