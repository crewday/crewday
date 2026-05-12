"""llm_thinking_levels_cd_cgefw

Revision ID: cdcgefwthink
Revises: f4b6d8e0a2c4
Create Date: 2026-05-12 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cdcgefwthink"
down_revision: str | Sequence[str] | None = "f4b6d8e0a2c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEVELS = "'disabled', 'low', 'medium', 'high'"


def upgrade() -> None:
    with op.batch_alter_table("llm_model", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "thinking_level",
                sa.String(),
                nullable=False,
                server_default="disabled",
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_llm_model_thinking_level"),
            f"thinking_level IN ({_LEVELS})",
        )

    with op.batch_alter_table("llm_provider_model", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("thinking_level_override", sa.String(), nullable=True)
        )
        batch_op.create_check_constraint(
            op.f("ck_llm_provider_model_thinking_level_override"),
            "thinking_level_override IS NULL "
            f"OR thinking_level_override IN ({_LEVELS})",
        )

    op.execute(
        "UPDATE llm_provider_model "
        "SET thinking_level_override = reasoning_effort "
        "WHERE reasoning_effort IN ('low', 'medium', 'high')"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE llm_provider_model "
        "SET reasoning_effort = CASE "
        "WHEN thinking_level_override IN ('low', 'medium', 'high') "
        "THEN thinking_level_override "
        "WHEN thinking_level_override = 'disabled' THEN '' "
        "ELSE reasoning_effort END"
    )

    with op.batch_alter_table("llm_provider_model", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("ck_llm_provider_model_thinking_level_override"),
            type_="check",
        )
        batch_op.drop_column("thinking_level_override")

    with op.batch_alter_table("llm_model", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_llm_model_thinking_level"), type_="check")
        batch_op.drop_column("thinking_level")
