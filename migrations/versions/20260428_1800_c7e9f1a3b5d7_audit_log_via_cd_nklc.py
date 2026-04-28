"""audit_log_via_cd_nklc

Revision ID: c7e9f1a3b5d7
Revises: b6d8f0a2c4e6
Create Date: 2026-04-28 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e9f1a3b5d7"
down_revision: str | Sequence[str] | None = "b6d8f0a2c4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("via", sa.String(), nullable=False, server_default="web")
        )
        batch_op.create_check_constraint(
            "via",
            "via IN ('web', 'api', 'cli', 'worker')",
        )
    op.execute("UPDATE audit_log SET via = 'web' WHERE via IS NULL")
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.alter_column(
            "via",
            existing_type=sa.String(),
            existing_nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_constraint("via", type_="check")
        batch_op.drop_column("via")
