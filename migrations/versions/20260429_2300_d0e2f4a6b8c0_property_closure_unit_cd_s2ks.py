"""property_closure_unit_cd_s2ks

Revision ID: d0e2f4a6b8c0
Revises: c9e1f3a5b7d9
Create Date: 2026-04-29 23:00:00.000000

Adds nullable unit scoping to property closures. NULL remains a
whole-property closure; non-NULL applies only to the referenced unit.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d0e2f4a6b8c0"
down_revision: str | Sequence[str] | None = "c9e1f3a5b7d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("property_closure", schema=None) as batch_op:
        batch_op.add_column(sa.Column("unit_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_property_closure_unit_id_unit",
            "unit",
            ["unit_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_property_closure_unit", ["unit_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("property_closure", schema=None) as batch_op:
        batch_op.drop_index("ix_property_closure_unit")
        batch_op.drop_constraint("fk_property_closure_unit_id_unit", type_="foreignkey")
        batch_op.drop_column("unit_id")
