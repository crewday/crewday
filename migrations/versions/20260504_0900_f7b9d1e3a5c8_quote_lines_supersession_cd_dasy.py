"""quote lines and supersession pointer cd-dasy

Revision ID: f7b9d1e3a5c8
Revises: e6a8b0c2d4f8
Create Date: 2026-05-04 09:00:00.000000

Adds the §22 line payload and explicit supersession forward pointer to
quotes. Existing total-only rows are backfilled as one "other" line so
history remains readable through the new API shape.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7b9d1e3a5c8"
down_revision: str | Sequence[str] | None = "e6a8b0c2d4f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    default_lines = json.dumps({"schema_version": 1, "lines": []})
    with op.batch_alter_table("quote", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "lines_json",
                sa.JSON(),
                nullable=False,
                server_default=default_lines,
            )
        )
        batch_op.add_column(
            sa.Column(
                "subtotal_cents",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "tax_cents",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column("superseded_by_quote_id", sa.String(), nullable=True)
        )
        batch_op.create_check_constraint("subtotal_cents_nonneg", "subtotal_cents >= 0")
        batch_op.create_check_constraint("tax_cents_nonneg", "tax_cents >= 0")
        batch_op.create_foreign_key(
            "fk_quote_superseded_by_quote_id_quote",
            "quote",
            ["superseded_by_quote_id"],
            ["id"],
            ondelete="SET NULL",
        )

    conn = op.get_bind()
    quote = sa.table(
        "quote",
        sa.column("id", sa.String()),
        sa.column("title", sa.String()),
        sa.column("total_cents", sa.BigInteger()),
        sa.column("lines_json", sa.JSON()),
        sa.column("subtotal_cents", sa.BigInteger()),
    )
    rows = conn.execute(sa.select(quote.c.id, quote.c.title, quote.c.total_cents))
    for row in rows.mappings():
        total_cents = int(row["total_cents"])
        payload = {
            "schema_version": 1,
            "lines": [
                {
                    "kind": "other",
                    "description": row["title"],
                    "quantity": 1,
                    "unit": "unit",
                    "unit_price_cents": total_cents,
                    "total_cents": total_cents,
                }
            ],
        }
        conn.execute(
            quote.update()
            .where(quote.c.id == row["id"])
            .values(lines_json=payload, subtotal_cents=total_cents)
        )

    with op.batch_alter_table("quote", schema=None) as batch_op:
        batch_op.alter_column("lines_json", server_default=None)
        batch_op.alter_column("subtotal_cents", server_default=None)
        batch_op.alter_column("tax_cents", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("quote", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_quote_superseded_by_quote_id_quote", type_="foreignkey"
        )
        batch_op.drop_constraint("tax_cents_nonneg", type_="check")
        batch_op.drop_constraint("subtotal_cents_nonneg", type_="check")
        batch_op.drop_column("superseded_by_quote_id")
        batch_op.drop_column("tax_cents")
        batch_op.drop_column("subtotal_cents")
        batch_op.drop_column("lines_json")
