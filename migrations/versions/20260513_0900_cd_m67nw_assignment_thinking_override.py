"""assignment_thinking_override_cd_m67nw

Revision ID: cdm67nwthink
Revises: cdyvu9lprio
Create Date: 2026-05-13 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cdm67nwthink"
down_revision: str | Sequence[str] | None = "cdyvu9lprio"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEVELS = "'disabled', 'low', 'medium', 'high'"


def upgrade() -> None:
    with op.batch_alter_table("llm_assignment", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("thinking_level_override", sa.String(), nullable=True)
        )
        batch_op.create_check_constraint(
            op.f("ck_llm_assignment_thinking_level_override"),
            "thinking_level_override IS NULL "
            f"OR thinking_level_override IN ({_LEVELS})",
        )


def downgrade() -> None:
    with op.batch_alter_table("llm_assignment", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("ck_llm_assignment_thinking_level_override"),
            type_="check",
        )
        batch_op.drop_column("thinking_level_override")
