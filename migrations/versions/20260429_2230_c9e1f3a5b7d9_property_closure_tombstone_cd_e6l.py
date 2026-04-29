"""property_closure_tombstone_cd_e6l

Revision ID: c9e1f3a5b7d9
Revises: b8d0e2f4a6c8
Create Date: 2026-04-29 22:30:00.000000

Adds the source-VEVENT identity and soft-delete tombstone columns that
make iCal-sourced closure deletes sticky across re-polls.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9e1f3a5b7d9"
down_revision: str | Sequence[str] | None = "b8d0e2f4a6c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("property_closure", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("source_external_uid", sa.String(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("source_last_seen_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
        )

    op.create_index(
        "ix_property_closure_source_uid",
        "property_closure",
        ["source_ical_feed_id", "source_external_uid"],
        unique=False,
    )
    op.create_index(
        "ix_property_closure_deleted",
        "property_closure",
        ["deleted_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_property_closure_deleted", table_name="property_closure")
    op.drop_index("ix_property_closure_source_uid", table_name="property_closure")
    with op.batch_alter_table("property_closure", schema=None) as batch_op:
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("source_last_seen_at")
        batch_op.drop_column("source_external_uid")
