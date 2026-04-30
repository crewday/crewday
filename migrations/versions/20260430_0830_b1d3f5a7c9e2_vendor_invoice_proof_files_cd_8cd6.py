"""vendor invoice proof files cd-8cd6

Revision ID: b1d3f5a7c9e2
Revises: a9c1e3f5b7d0
Create Date: 2026-04-30 08:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1d3f5a7c9e2"
down_revision: str | Sequence[str] | None = "a9c1e3f5b7d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("vendor_invoice", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "proof_of_payment_file_ids",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("vendor_invoice", schema=None) as batch_op:
        batch_op.drop_column("proof_of_payment_file_ids")
