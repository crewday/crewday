"""api token previous hash overlap cd-oa8iz

Revision ID: a4c6e8f0b2d4
Revises: f7b9d1e3a5c8
Create Date: 2026-05-04 10:00:00.000000

Adds the nullable sibling hash columns used by token rotation's
one-hour overlap window. Existing rows backfill to NULL and only get
fallback data on their next rotation.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.adapters.db._columns import UtcDateTime

# revision identifiers, used by Alembic.
revision: str = "a4c6e8f0b2d4"
down_revision: str | Sequence[str] | None = "f7b9d1e3a5c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("api_token", schema=None) as batch_op:
        batch_op.add_column(sa.Column("previous_hash", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("previous_hash_expires_at", UtcDateTime(), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("api_token", schema=None) as batch_op:
        batch_op.drop_column("previous_hash_expires_at")
        batch_op.drop_column("previous_hash")
