"""throttle_window cd-0lnr9

Revision ID: cd0lnr9winhit
Revises: cd1hgd4occidx
Create Date: 2026-07-04 12:00:00.000000

Adds the deployment-wide ``throttle_window`` table used by the
database-backed abuse throttle (:class:`app.abuse.window_store.DbWindowStore`).
Multi-worker deployments (``rate_limit_backend = "postgres"``) share this
table so the spec §15 per-deployment caps (e.g. ≤ 200 signup starts /
deployment / hour) hold across every worker instead of being multiplied
by the worker count. Bucket keys are already privacy-preserving at the
caller boundary (peppered IP / email / credential hashes).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cd0lnr9winhit"
down_revision: str | Sequence[str] | None = "cd1hgd4occidx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "throttle_window",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("bucket_key", sa.String(), nullable=False),
        sa.Column("hits_json", sa.JSON(), nullable=False),
        sa.Column("updated_at_epoch", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("scope", "bucket_key", name=op.f("pk_throttle_window")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("throttle_window")
