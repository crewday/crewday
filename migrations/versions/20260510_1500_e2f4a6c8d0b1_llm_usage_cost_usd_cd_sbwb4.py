"""llm_usage_cost_usd_cd_sbwb4

Add ``llm_usage.cost_usd`` as the precise per-call LLM cost while
leaving ``cost_cents`` in place for budget and legacy API consumers.
Existing rows are backfilled from cents so old admin reports keep
their previous meaning; new rows can store sub-cent values.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f4a6c8d0b1"
down_revision: str | Sequence[str] | None = "cd_rfk94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("llm_usage", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "cost_usd",
                sa.Numeric(12, 6),
                nullable=False,
                server_default="0",
            )
        )
    op.execute("UPDATE llm_usage SET cost_usd = cost_cents / 100.0")


def downgrade() -> None:
    with op.batch_alter_table("llm_usage", schema=None) as batch_op:
        batch_op.drop_column("cost_usd")
