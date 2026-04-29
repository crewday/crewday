"""area_cd_a2k

Revision ID: b8d0e2f4a6c8
Revises: a7c9e1f3b5d8
Create Date: 2026-04-29 22:00:00.000000

Extends ``area`` with the §04 domain-service surface used by cd-a2k:
unit-scoped areas, the ``kind`` enum, one-level parent nesting,
soft-delete timestamps, notes, and a spec-named ``name`` column.

The v1 ``label`` / ``ordering`` columns survive for back-compat with
existing property-list projections. The domain service writes
``name`` and ``label`` together, and exposes ``ordering`` as
``order_hint``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d0e2f4a6c8"
down_revision: str | Sequence[str] | None = "a7c9e1f3b5d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("area", schema=None) as batch_op:
        batch_op.add_column(sa.Column("unit_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("name", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.String(),
                nullable=False,
                server_default="indoor_room",
            )
        )
        batch_op.add_column(sa.Column("parent_area_id", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "notes_md",
                sa.String(),
                nullable=False,
                server_default="",
            )
        )
        batch_op.add_column(
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_area_unit_id_unit",
            "unit",
            ["unit_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_area_parent_area_id_area",
            "area",
            ["parent_area_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_check_constraint(
            "kind",
            "kind IN ('indoor_room', 'outdoor', 'service')",
        )
        batch_op.create_index("ix_area_unit", ["unit_id"], unique=False)
        batch_op.create_index("ix_area_parent", ["parent_area_id"], unique=False)
        batch_op.create_index("ix_area_deleted", ["deleted_at"], unique=False)

    op.execute("UPDATE area SET name = label WHERE name IS NULL")
    op.execute("UPDATE area SET updated_at = created_at WHERE updated_at IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("area", schema=None) as batch_op:
        batch_op.drop_index("ix_area_deleted")
        batch_op.drop_index("ix_area_parent")
        batch_op.drop_index("ix_area_unit")
        batch_op.drop_constraint("kind", type_="check")
        batch_op.drop_constraint("fk_area_parent_area_id_area", type_="foreignkey")
        batch_op.drop_constraint("fk_area_unit_id_unit", type_="foreignkey")
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("notes_md")
        batch_op.drop_column("parent_area_id")
        batch_op.drop_column("kind")
        batch_op.drop_column("name")
        batch_op.drop_column("unit_id")
