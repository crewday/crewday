"""demo workspace cd-otv3

Revision ID: d4f6a8c0e2b4
Revises: c3e5f7a9b1d2
Create Date: 2026-04-29 18:00:00.000000

Create the §24 demo workspace marker table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4f6a8c0e2b4"
down_revision: str | Sequence[str] | None = "c3e5f7a9b1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "demo_workspace",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("scenario_key", sa.String(), nullable=False),
        sa.Column("seed_digest", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cookie_binding_digest", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["id"],
            ["workspace.id"],
            name=op.f("fk_demo_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demo_workspace")),
    )
    op.create_index(
        "ix_demo_workspace_expires_at",
        "demo_workspace",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_demo_workspace_expires_at", table_name="demo_workspace")
    op.drop_table("demo_workspace")
