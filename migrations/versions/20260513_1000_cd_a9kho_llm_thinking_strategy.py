"""llm_thinking_strategy_cd_a9kho

Revision ID: cda9khothink
Revises: cdm67nwthink
Create Date: 2026-05-13 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cda9khothink"
down_revision: str | Sequence[str] | None = "cdm67nwthink"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STRATEGIES = "'none', 'gemma_system_token', 'glm_extra_body', 'openrouter_extra_body'"


def upgrade() -> None:
    with op.batch_alter_table("llm_model", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "thinking_strategy",
                sa.String(),
                nullable=False,
                server_default="none",
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_llm_model_thinking_strategy"),
            f"thinking_strategy IN ({_STRATEGIES})",
        )

    with op.batch_alter_table("llm_provider_model", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("thinking_strategy_override", sa.String(), nullable=True)
        )
        batch_op.create_check_constraint(
            op.f("ck_llm_provider_model_thinking_strategy_override"),
            "thinking_strategy_override IS NULL "
            f"OR thinking_strategy_override IN ({_STRATEGIES})",
        )

    op.execute(
        "UPDATE llm_provider_model "
        "SET thinking_strategy_override = 'openrouter_extra_body' "
        "WHERE id IN ("
        "SELECT pm.id "
        "FROM llm_provider_model pm "
        "JOIN llm_provider provider ON provider.id = pm.provider_id "
        "JOIN llm_model model ON model.id = pm.model_id "
        "WHERE provider.provider_type = 'openrouter' "
        "AND ("
        "pm.thinking_level_override IN ('low', 'medium', 'high') "
        "OR (pm.thinking_level_override IS NULL "
        "AND model.thinking_level IN ('low', 'medium', 'high')) "
        "OR EXISTS ("
        "SELECT 1 FROM llm_assignment assignment "
        "WHERE assignment.model_id = pm.id "
        "AND assignment.thinking_level_override IN ('low', 'medium', 'high')"
        ")"
        ")"
        ")"
    )


def downgrade() -> None:
    with op.batch_alter_table("llm_provider_model", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("ck_llm_provider_model_thinking_strategy_override"),
            type_="check",
        )
        batch_op.drop_column("thinking_strategy_override")

    with op.batch_alter_table("llm_model", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("ck_llm_model_thinking_strategy"),
            type_="check",
        )
        batch_op.drop_column("thinking_strategy")
