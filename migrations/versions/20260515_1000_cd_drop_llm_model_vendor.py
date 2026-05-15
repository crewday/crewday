"""drop llm model vendor

Revision ID: cddropmodelvendor
Revises: cddropthinkpm
Create Date: 2026-05-15 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cddropmodelvendor"
down_revision: str | Sequence[str] | None = "cddropthinkpm"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("llm_model", schema=None) as batch_op:
        batch_op.drop_column("vendor")


def downgrade() -> None:
    with op.batch_alter_table("llm_model", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("vendor", sa.String(), nullable=False, server_default="other")
        )
