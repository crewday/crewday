"""drop_provider_model_thinking_level

Revision ID: cddropthinkpm
Revises: cda9khothink
Create Date: 2026-05-15 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cddropthinkpm"
down_revision: str | Sequence[str] | None = "cda9khothink"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEVELS = "'disabled', 'low', 'medium', 'high'"


def upgrade() -> None:
    with op.batch_alter_table("llm_provider_model", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("ck_llm_provider_model_thinking_level_override"),
            type_="check",
        )
        batch_op.drop_column("thinking_level_override")
        batch_op.drop_column("reasoning_effort")


def downgrade() -> None:
    with op.batch_alter_table("llm_provider_model", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reasoning_effort", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("thinking_level_override", sa.String(), nullable=True)
        )
        batch_op.create_check_constraint(
            op.f("ck_llm_provider_model_thinking_level_override"),
            "thinking_level_override IS NULL "
            f"OR thinking_level_override IN ({_LEVELS})",
        )
