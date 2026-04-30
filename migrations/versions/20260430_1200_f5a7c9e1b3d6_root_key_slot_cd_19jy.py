"""root key slot table cd-19jy

Revision ID: f5a7c9e1b3d6
Revises: e4f6a8c0d2e5
Create Date: 2026-04-30 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5a7c9e1b3d6"
down_revision: str | Sequence[str] | None = "e4f6a8c0d2e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "root_key_slot",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("key_fp", sa.LargeBinary(), nullable=False),
        sa.Column("key_ref", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_fp", name="uq_root_key_slot_key_fp"),
    )
    op.create_index("ix_root_key_slot_key_fp", "root_key_slot", ["key_fp"])
    op.create_index("ix_root_key_slot_is_active", "root_key_slot", ["is_active"])
    op.create_index("ix_root_key_slot_purge_after", "root_key_slot", ["purge_after"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_root_key_slot_purge_after", table_name="root_key_slot")
    op.drop_index("ix_root_key_slot_is_active", table_name="root_key_slot")
    op.drop_index("ix_root_key_slot_key_fp", table_name="root_key_slot")
    op.drop_table("root_key_slot")
