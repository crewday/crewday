"""expense FK promotion cd-48c1

Revision ID: b3d5e7f9a1c4
Revises: a2c4e6f8b0d2
Create Date: 2026-05-03 13:00:00.000000

Promotes four expense soft-refs left by cd-lbn into real foreign
keys now that their parent tables have landed:

* ``expense_claim.owed_destination_id`` → ``payout_destination.id``
  (``ON DELETE RESTRICT``).
* ``expense_claim.reimbursement_destination_id`` →
  ``payout_destination.id`` (``ON DELETE RESTRICT``).
* ``expense_claim.property_id`` → ``property.id`` (``ON DELETE
  SET NULL``).
* ``expense_line.asset_id`` → ``asset.id`` (``ON DELETE SET NULL``).

**ON DELETE rationale.** The two ``payout_destination`` snapshots
on an approved claim are payroll-law evidence (§09 §"Currency
alignment rule" / §"Amount owed to the employee"). Silently
nulling them on a raw ``DELETE FROM payout_destination`` would
strip the audit trail, so we use ``RESTRICT`` — the normal archive
path is ``payout_destination.archived_at``, which leaves the FK
valid. ``property_id`` and ``expense_line.asset_id`` are not
audit-bearing in the same way: a deleted property doesn't sweep
the expense (the purchase data lives on independently in
``llm_autofill_json`` and the pay-period rollup), and a deleted
asset row doesn't invalidate the line's description + amount.
``SET NULL`` keeps the claim / line alive while cleanly detaching
the FK.

The ``expense_attachment.blob_hash`` soft-ref is **not** promoted
here. The §02 §"Shared tables" §"file" table has not yet landed;
this migration explicitly defers that FK promotion until it does.

**Validation.** For each promoted FK we reject orphan rows up
front so a stale dev DB fails the migration loudly instead of
silently corrupting the new constraint. The check raises a
``RuntimeError`` listing the offending child + parent column so the
operator can investigate before re-running.

**Dialect branching.** SQLite cannot ``ALTER TABLE ... ADD
CONSTRAINT FOREIGN KEY``, so the SQLite path goes through
``op.batch_alter_table`` (table-copy). PostgreSQL uses
``op.create_foreign_key`` directly — a cheap metadata update.
``downgrade`` mirrors the same branch.

See ``docs/specs/02-domain-model.md`` §"Core entities (by
document)" (§09 row), §"Shared tables"; and
``docs/specs/09-time-payroll-expenses.md`` §"Expense claims",
§"Payout destinations", §"Amount owed to the employee".
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "b3d5e7f9a1c4"
down_revision: str | Sequence[str] | None = "a2c4e6f8b0d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (child_table, child_column, parent_table) tuples for the validation
# pre-check. Mirrors the FK list created below.
_PROMOTIONS: tuple[tuple[str, str, str], ...] = (
    ("expense_claim", "owed_destination_id", "payout_destination"),
    ("expense_claim", "reimbursement_destination_id", "payout_destination"),
    ("expense_claim", "property_id", "property"),
    ("expense_line", "asset_id", "asset"),
)


def _assert_no_orphans() -> None:
    """Raise if any row points at a missing parent.

    Each promoted FK gets a dedicated ``SELECT id FROM <child>
    WHERE <col> IS NOT NULL AND <col> NOT IN (SELECT id FROM
    <parent>)`` probe. A non-empty result is a stale dev DB; we
    refuse to add the constraint until the operator clears or
    repoints those rows.
    """
    bind = op.get_bind()
    failures: list[str] = []
    for child_table, child_column, parent_table in _PROMOTIONS:
        sql = text(
            f"SELECT id FROM {child_table} "
            f"WHERE {child_column} IS NOT NULL "
            f"AND {child_column} NOT IN (SELECT id FROM {parent_table})"
        )
        orphan_ids = [row[0] for row in bind.execute(sql)]
        if orphan_ids:
            preview = ", ".join(orphan_ids[:5])
            suffix = "" if len(orphan_ids) <= 5 else f" (+{len(orphan_ids) - 5} more)"
            failures.append(
                f"{child_table}.{child_column} → {parent_table}.id: "
                f"{len(orphan_ids)} orphan row(s): {preview}{suffix}"
            )
    if failures:
        raise RuntimeError(
            "expense FK promotion cd-48c1 refuses to run: "
            + "; ".join(failures)
            + ". Clear or repoint these rows before re-running the migration."
        )


def upgrade() -> None:
    """Upgrade schema."""
    _assert_no_orphans()

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.create_foreign_key(
            op.f("fk_expense_claim_owed_destination_id_payout_destination"),
            "expense_claim",
            "payout_destination",
            ["owed_destination_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_foreign_key(
            op.f("fk_expense_claim_reimbursement_destination_id_payout_destination"),
            "expense_claim",
            "payout_destination",
            ["reimbursement_destination_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_foreign_key(
            op.f("fk_expense_claim_property_id_property"),
            "expense_claim",
            "property",
            ["property_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            op.f("fk_expense_line_asset_id_asset"),
            "expense_line",
            "asset",
            ["asset_id"],
            ["id"],
            ondelete="SET NULL",
        )
    else:
        # SQLite — must use batch_alter_table so the new FK lands via
        # a table copy.
        with op.batch_alter_table("expense_claim", schema=None) as batch_op:
            batch_op.create_foreign_key(
                op.f("fk_expense_claim_owed_destination_id_payout_destination"),
                "payout_destination",
                ["owed_destination_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch_op.create_foreign_key(
                op.f(
                    "fk_expense_claim_reimbursement_destination_id_payout_destination"
                ),
                "payout_destination",
                ["reimbursement_destination_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch_op.create_foreign_key(
                op.f("fk_expense_claim_property_id_property"),
                "property",
                ["property_id"],
                ["id"],
                ondelete="SET NULL",
            )
        with op.batch_alter_table("expense_line", schema=None) as batch_op:
            batch_op.create_foreign_key(
                op.f("fk_expense_line_asset_id_asset"),
                "asset",
                ["asset_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    """Downgrade schema."""
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.drop_constraint(
            op.f("fk_expense_line_asset_id_asset"),
            "expense_line",
            type_="foreignkey",
        )
        op.drop_constraint(
            op.f("fk_expense_claim_property_id_property"),
            "expense_claim",
            type_="foreignkey",
        )
        op.drop_constraint(
            op.f("fk_expense_claim_reimbursement_destination_id_payout_destination"),
            "expense_claim",
            type_="foreignkey",
        )
        op.drop_constraint(
            op.f("fk_expense_claim_owed_destination_id_payout_destination"),
            "expense_claim",
            type_="foreignkey",
        )
    else:
        with op.batch_alter_table("expense_line", schema=None) as batch_op:
            batch_op.drop_constraint(
                op.f("fk_expense_line_asset_id_asset"),
                type_="foreignkey",
            )
        with op.batch_alter_table("expense_claim", schema=None) as batch_op:
            batch_op.drop_constraint(
                op.f("fk_expense_claim_property_id_property"),
                type_="foreignkey",
            )
            batch_op.drop_constraint(
                op.f(
                    "fk_expense_claim_reimbursement_destination_id_payout_destination"
                ),
                type_="foreignkey",
            )
            batch_op.drop_constraint(
                op.f("fk_expense_claim_owed_destination_id_payout_destination"),
                type_="foreignkey",
            )
