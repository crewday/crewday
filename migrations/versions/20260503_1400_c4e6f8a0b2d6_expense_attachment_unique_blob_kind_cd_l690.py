"""expense attachment unique (claim_id, blob_hash, kind) cd-l690

Revision ID: c4e6f8a0b2d6
Revises: b3d5e7f9a1c4
Create Date: 2026-05-03 14:00:00.000000

Adds a unique constraint over ``(claim_id, blob_hash, kind)`` on
``expense_attachment`` so a worker cannot attach the same blob to a
single claim twice under the same kind. Storage is content-addressed
— the bytes are shared — but a duplicate row clutters the manager
approval UI and would force the cd-95zb OCR worker to re-run on the
same hash.

**Decision matrix.** Same blob with a different ``kind`` (receipt vs
invoice) stays legal: the worker may be re-classifying. Same blob
with the same ``kind`` but a different ``pages`` count is rejected —
the unique key intentionally omits ``pages`` because the page count
is derived from the blob, not asserted independently.

**Validation.** A pre-flight ``GROUP BY (claim_id, blob_hash, kind)``
probe rejects any existing duplicate up front so a stale dev DB
fails the migration loudly instead of producing a confusing
``IntegrityError`` mid-DDL. Realistically the dev DB shouldn't carry
duplicates (the v1 attach service has no concurrent-write story), but
the pre-flight makes the error sane if it does.

**Dialect branching.** PostgreSQL gets a direct
``op.create_unique_constraint`` — a metadata-only change. SQLite
cannot ``ALTER TABLE ... ADD CONSTRAINT UNIQUE`` so the SQLite path
goes through ``op.batch_alter_table`` (table copy). ``downgrade``
mirrors the same branch.

See ``docs/specs/02-domain-model.md`` §"expense_attachment" and
``docs/specs/09-time-payroll-expenses.md`` §"Submission flow (worker)".
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "c4e6f8a0b2d6"
down_revision: str | Sequence[str] | None = "b3d5e7f9a1c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT_NAME = "uq_expense_attachment_claim_blob_kind"


def _assert_no_duplicate_rows() -> None:
    """Raise if any (claim_id, blob_hash, kind) triplet already has duplicates.

    A non-empty result is a stale dev DB; we refuse to add the
    constraint until the operator clears or repoints those rows. The
    error message lists the offending triplets so the operator can
    investigate before re-running.
    """
    bind = op.get_bind()
    sql = text(
        "SELECT claim_id, blob_hash, kind, COUNT(*) AS n "
        "FROM expense_attachment "
        "GROUP BY claim_id, blob_hash, kind "
        "HAVING COUNT(*) > 1"
    )
    rows = list(bind.execute(sql))
    if rows:
        preview = ", ".join(
            f"({claim_id!r}, {blob_hash!r}, {kind!r}) x{count}"
            for claim_id, blob_hash, kind, count in rows[:5]
        )
        suffix = "" if len(rows) <= 5 else f" (+{len(rows) - 5} more)"
        raise RuntimeError(
            "expense_attachment unique-key migration cd-l690 refuses to run: "
            f"{len(rows)} duplicate (claim_id, blob_hash, kind) triplet(s) "
            f"already present: {preview}{suffix}. Clear the duplicate rows "
            "before re-running the migration."
        )


def upgrade() -> None:
    """Upgrade schema."""
    _assert_no_duplicate_rows()

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.create_unique_constraint(
            _CONSTRAINT_NAME,
            "expense_attachment",
            ["claim_id", "blob_hash", "kind"],
        )
    else:
        # SQLite — must use batch_alter_table so the new UNIQUE lands
        # via a table copy.
        with op.batch_alter_table("expense_attachment", schema=None) as batch_op:
            batch_op.create_unique_constraint(
                _CONSTRAINT_NAME,
                ["claim_id", "blob_hash", "kind"],
            )


def downgrade() -> None:
    """Downgrade schema."""
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.drop_constraint(
            _CONSTRAINT_NAME,
            "expense_attachment",
            type_="unique",
        )
    else:
        with op.batch_alter_table("expense_attachment", schema=None) as batch_op:
            batch_op.drop_constraint(_CONSTRAINT_NAME, type_="unique")
