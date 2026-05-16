"""persist llm usage audio duration seconds

Revision ID: cd6y6s5audsecs
Revises: cde34z3audprice
Create Date: 2026-05-16 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cd6y6s5audsecs"
down_revision: str | Sequence[str] | None = "cde34z3audprice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("llm_usage", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("audio_seconds", sa.Numeric(12, 3), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("llm_usage", schema=None) as batch_op:
        batch_op.drop_column("audio_seconds")
