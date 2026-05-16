"""add llm provider model audio duration pricing

Revision ID: cde34z3audprice
Revises: cdmodeltemperature
Create Date: 2026-05-16 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cde34z3audprice"
down_revision: str | Sequence[str] | None = "cdmodeltemperature"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("llm_provider_model", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("audio_cost_per_hour_usd", sa.Numeric(10, 4), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("llm_provider_model", schema=None) as batch_op:
        batch_op.drop_column("audio_cost_per_hour_usd")
