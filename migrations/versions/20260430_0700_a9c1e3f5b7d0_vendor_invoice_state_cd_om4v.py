"""vendor invoice state cd-om4v

Revision ID: a9c1e3f5b7d0
Revises: f8a0c2e4f6b8
Create Date: 2026-04-30 07:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9c1e3f5b7d0"
down_revision: str | Sequence[str] | None = "f8a0c2e4f6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("vendor_invoice", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("payment_method", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("proof_blob_hash", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("disputed_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("vendor_invoice", schema=None) as batch_op:
        batch_op.drop_column("disputed_at")
        batch_op.drop_column("proof_blob_hash")
        batch_op.drop_column("payment_method")
        batch_op.drop_column("paid_at")
        batch_op.drop_column("approved_at")
