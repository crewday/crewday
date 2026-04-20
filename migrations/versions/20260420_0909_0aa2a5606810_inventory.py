"""inventory

Revision ID: 0aa2a5606810
Revises: 53e464485919
Create Date: 2026-04-20 09:09:00.000000

Creates the three inventory-context tables that back the item CRUD +
movement ledger + reorder-rule flows (see
``docs/specs/02-domain-model.md`` §"inventory_item",
§"inventory_movement" and ``docs/specs/08-inventory.md``):

* ``inventory_item`` — a stock-keeping unit with a unit-of-measure
  enum, optional category / barcode, a cached ``current_qty``
  running total, and an optional ``min_qty`` reorder threshold.
  CHECK on ``unit`` clamps the v1 vocabulary (``ea | l | kg | m |
  pkg | box | other``). UNIQUE ``(workspace_id, sku)`` enforces
  cd-bxt's acceptance criterion: a workspace cannot mint two items
  sharing a SKU. ``workspace_id`` FK cascades — sweeping a
  workspace sweeps its stock library (§15 export snapshots first).
  ``current_qty`` and ``min_qty`` are ``Numeric(18, 4)`` Decimals
  for portability across SQLite (TEXT) and Postgres (NUMERIC),
  covering fractional units (0.25 kg, 1.500 l).

* ``inventory_movement`` — the append-only ledger row. Signed
  :class:`Decimal` ``delta`` column (``Numeric(18, 4)``), CHECK on
  ``reason`` clamping the v1 enum (``receive | issue | adjust |
  consume`` — §02 / §08 list a richer ``restock | consume | adjust
  | waste | transfer_in | transfer_out | audit_correction`` that
  widens in the transfer + waste + audit follow-ups).
  ``workspace_id`` FK cascades. ``item_id`` FK cascades — deleting
  an item drops every movement row. ``occurrence_id`` stays a plain
  :class:`str` soft-ref because the §06 occurrence identifier is
  still landing; ``created_by`` is likewise a soft-ref because the
  actor may be a system process. The
  ``(workspace_id, item_id, created_at)`` index powers the
  "ledger for this item, newest first" lookup.

* ``inventory_reorder_rule`` — one rule per (workspace, item) pair.
  ``reorder_at`` threshold + ``reorder_qty`` target + ``enabled``
  kill switch. CHECK constraints clamp ``reorder_at >= 0`` and
  ``reorder_qty > 0`` (a negative threshold never fires; a zero
  target is meaningless). UNIQUE ``(workspace_id, item_id)``
  enforces the one-rule-per-item invariant and powers the
  per-hourly-pass lookup. ``workspace_id`` FK cascades; ``item_id``
  FK cascades — a rule for a deleted item would silently resurrect
  the item's restock task on the next pass.

All three tables are workspace-scoped (registered via the package's
``__init__``). Tables are created in a stable deterministic order
matching the dependency chain (``inventory_item`` before
``inventory_movement`` / ``inventory_reorder_rule`` because the two
child rows carry an FK into the parent); ``downgrade()`` drops in
reverse.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0aa2a5606810"
down_revision: str | Sequence[str] | None = "53e464485919"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "inventory_item",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("sku", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("barcode", sa.String(), nullable=True),
        sa.Column(
            "current_qty",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
        ),
        sa.Column(
            "min_qty",
            sa.Numeric(precision=18, scale=4),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "unit IN ('ea', 'l', 'kg', 'm', 'pkg', 'box', 'other')",
            name=op.f("ck_inventory_item_unit"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_inventory_item_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_item")),
        sa.UniqueConstraint(
            "workspace_id",
            "sku",
            name="uq_inventory_item_workspace_sku",
        ),
    )

    op.create_table(
        "inventory_movement",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column(
            "delta",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
        ),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("occurrence_id", sa.String(), nullable=True),
        sa.Column("note_md", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.CheckConstraint(
            "reason IN ('receive', 'issue', 'adjust', 'consume')",
            name=op.f("ck_inventory_movement_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["inventory_item.id"],
            name=op.f("fk_inventory_movement_item_id_inventory_item"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_inventory_movement_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_movement")),
    )
    with op.batch_alter_table("inventory_movement", schema=None) as batch_op:
        batch_op.create_index(
            "ix_inventory_movement_workspace_item_created",
            ["workspace_id", "item_id", "created_at"],
            unique=False,
        )

    op.create_table(
        "inventory_reorder_rule",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column(
            "reorder_at",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
        ),
        sa.Column(
            "reorder_qty",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "reorder_at >= 0",
            name=op.f("ck_inventory_reorder_rule_reorder_at_nonneg"),
        ),
        sa.CheckConstraint(
            "reorder_qty > 0",
            name=op.f("ck_inventory_reorder_rule_reorder_qty_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["inventory_item.id"],
            name=op.f("fk_inventory_reorder_rule_item_id_inventory_item"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_inventory_reorder_rule_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_reorder_rule")),
        sa.UniqueConstraint(
            "workspace_id",
            "item_id",
            name="uq_inventory_reorder_rule_workspace_item",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("inventory_reorder_rule")
    with op.batch_alter_table("inventory_movement", schema=None) as batch_op:
        batch_op.drop_index("ix_inventory_movement_workspace_item_created")
    op.drop_table("inventory_movement")
    op.drop_table("inventory_item")
