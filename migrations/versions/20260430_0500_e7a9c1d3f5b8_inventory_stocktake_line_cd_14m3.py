"""inventory stocktake draft lines cd-14m3

Revision ID: e7a9c1d3f5b8
Revises: d6f8a0c2e4f7
Create Date: 2026-04-30 05:00:00.000000

Adds session-scoped draft lines for property-wide inventory stocktakes.
The permanent ledger remains ``inventory_movement``; draft rows are
deleted when the session commits or is abandoned.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7a9c1d3f5b8"
down_revision: str | Sequence[str] | None = "d6f8a0c2e4f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_REASON_VALUES: tuple[str, ...] = (
    "restock",
    "consume",
    "produce",
    "waste",
    "theft",
    "loss",
    "found",
    "returned_to_vendor",
    "transfer_in",
    "transfer_out",
    "audit_correction",
    "adjust",
)
_MOVEMENT_REASON_ENUM = sa.Enum(
    *_REASON_VALUES,
    name="inventory_movement_reason",
    native_enum=True,
    create_constraint=False,
)


def _in_clause(values: tuple[str, ...]) -> str:
    return "'" + "', '".join(values) + "'"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "inventory_stocktake_line",
        sa.Column("stocktake_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column(
            "observed_on_hand",
            sa.Numeric(precision=14, scale=4, asdecimal=True),
            nullable=False,
        ),
        sa.Column("reason", _MOVEMENT_REASON_ENUM, nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"reason IN ({_in_clause(_REASON_VALUES)})",
            name=op.f("ck_inventory_stocktake_line_inventory_stocktake_line_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["inventory_item.id"],
            name=op.f("fk_inventory_stocktake_line_item_id_inventory_item"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["stocktake_id"],
            ["inventory_stocktake.id"],
            name=op.f("fk_inventory_stocktake_line_stocktake_id_inventory_stocktake"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_inventory_stocktake_line_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "stocktake_id",
            "item_id",
            name=op.f("pk_inventory_stocktake_line"),
        ),
    )
    op.create_index(
        "ix_inventory_stocktake_line_workspace_stocktake",
        "inventory_stocktake_line",
        ["workspace_id", "stocktake_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_inventory_stocktake_line_workspace_stocktake",
        table_name="inventory_stocktake_line",
    )
    op.drop_table("inventory_stocktake_line")
