"""organization_legal_name_cd_y3tzt

Revision ID: f4b6d8e0a2c4
Revises: e2f4a6c8d0b1
Create Date: 2026-05-10 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4b6d8e0a2c4"
down_revision: str | Sequence[str] | None = "e2f4a6c8d0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("organization", schema=None) as batch_op:
        batch_op.add_column(sa.Column("legal_name", sa.String(), nullable=True))
        batch_op.create_unique_constraint(
            op.f("uq_organization_workspace_legal_name"),
            ["workspace_id", "legal_name"],
        )


def downgrade() -> None:
    with op.batch_alter_table("organization", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("uq_organization_workspace_legal_name"),
            type_="unique",
        )
        batch_op.drop_column("legal_name")
