"""settings cascade columns cd-ro6v

Revision ID: c0e2f4a6b8d0
Revises: b9d1f3a5c7e9
Create Date: 2026-05-01 04:00:00.000000

Adds sparse settings override maps for the concrete cascade layers
needed by task completion defaults.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c0e2f4a6b8d0"
down_revision: str | Sequence[str] | None = "b9d1f3a5c7e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "property",
        sa.Column(
            "settings_override_json", sa.JSON(), nullable=False, server_default="{}"
        ),
    )
    op.add_column(
        "work_engagement",
        sa.Column(
            "settings_override_json", sa.JSON(), nullable=False, server_default="{}"
        ),
    )
    op.add_column(
        "task_template",
        sa.Column(
            "settings_override_json", sa.JSON(), nullable=False, server_default="{}"
        ),
    )
    op.add_column(
        "occurrence",
        sa.Column(
            "settings_override_json", sa.JSON(), nullable=False, server_default="{}"
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("occurrence", "settings_override_json")
    op.drop_column("task_template", "settings_override_json")
    op.drop_column("work_engagement", "settings_override_json")
    op.drop_column("property", "settings_override_json")
