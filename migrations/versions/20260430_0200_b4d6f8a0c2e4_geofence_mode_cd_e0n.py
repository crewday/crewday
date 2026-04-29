"""geofence mode cd-e0n

Revision ID: b4d6f8a0c2e4
Revises: a3c5e7f9b1d4
Create Date: 2026-04-30 02:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4d6f8a0c2e4"
down_revision: str | Sequence[str] | None = "a3c5e7f9b1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("geofence_setting", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "mode",
                sa.String(),
                server_default="enforce",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_geofence_setting_mode",
            "mode IN ('enforce', 'warn', 'off')",
        )

    geofence_setting = sa.table(
        "geofence_setting",
        sa.column("enabled", sa.Boolean()),
        sa.column("mode", sa.String()),
    )
    op.execute(
        geofence_setting.update()
        .where(geofence_setting.c.enabled == sa.false())
        .values(mode="off")
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("geofence_setting", schema=None) as batch_op:
        batch_op.drop_constraint("ck_geofence_setting_mode", type_="check")
        batch_op.drop_column("mode")
