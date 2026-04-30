"""rate_limit_bucket cd-uxdk

Revision ID: c5e7f9b1d4e6
Revises: b4d6f8a0c2e4
Create Date: 2026-04-30 03:00:00.000000

Adds the deployment-wide ``rate_limit_bucket`` table used by the
database-backed API rate limiter. Bucket keys are already
privacy-preserving at the middleware boundary: token callers use the
opaque token row id, and IP fallback callers use a peppered IP hash.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5e7f9b1d4e6"
down_revision: str | Sequence[str] | None = "b4d6f8a0c2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "rate_limit_bucket",
        sa.Column("bucket_key", sa.String(), nullable=False),
        sa.Column("tokens", sa.Float(), nullable=False),
        sa.Column("updated_at_epoch", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("bucket_key", name=op.f("pk_rate_limit_bucket")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("rate_limit_bucket")
