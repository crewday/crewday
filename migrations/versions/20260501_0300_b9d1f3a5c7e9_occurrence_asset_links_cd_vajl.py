"""occurrence asset links cd-vajl

Revision ID: b9d1f3a5c7e9
Revises: a8c0e2f4b6d8
Create Date: 2026-05-01 03:00:00.000000

Adds the §21 traceability links from task occurrences to assets and
asset actions. The asset and asset_action tables already exist from
cd-c66, so this migration only extends occurrence.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9d1f3a5c7e9"
down_revision: str | Sequence[str] | None = "a8c0e2f4b6d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("occurrence", sa.Column("asset_id", sa.String(), nullable=True))
    op.add_column(
        "occurrence", sa.Column("asset_action_id", sa.String(), nullable=True)
    )
    op.create_index(
        "ix_occurrence_workspace_asset",
        "occurrence",
        ["workspace_id", "asset_id"],
        unique=False,
    )
    op.create_index(
        "ix_occurrence_workspace_asset_action",
        "occurrence",
        ["workspace_id", "asset_action_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_occurrence_workspace_asset_action", table_name="occurrence")
    op.drop_index("ix_occurrence_workspace_asset", table_name="occurrence")
    op.drop_column("occurrence", "asset_action_id")
    op.drop_column("occurrence", "asset_id")
