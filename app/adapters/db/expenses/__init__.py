"""expenses — expense_claim / expense_line / expense_attachment.

All three tables in this package are workspace-scoped: each row
carries a ``workspace_id`` column and is registered in
:mod:`app.tenancy.registry` so the ORM tenant filter auto-injects a
``workspace_id`` predicate on every SELECT / UPDATE / DELETE. A
bare read without a :class:`~app.tenancy.WorkspaceContext` raises
:class:`~app.tenancy.orm_filter.TenantFilterMissing`.

Scoping is unambiguous: every expense row belongs to exactly one
workspace's expense ledger — the claim is filed against a workspace-
owned engagement, the lines and attachments live inside the claim.

``decided_by`` is persisted as a soft-ref :class:`str` column (no
SQL foreign key) — see :mod:`app.adapters.db.expenses.models` for
the rationale. ``owed_destination_id`` / ``reimbursement_destination_id``
/ ``property_id`` / ``expense_line.asset_id`` were promoted to real
FKs by cd-48c1 once their parent tables landed. The ``file`` table
referenced by :class:`ExpenseAttachment`'s ``blob_hash`` has not
landed yet — cd-48c1 explicitly defers that promotion until §02
§"Shared tables" §"file" lands; the soft-ref preserves the
content-addressed storage layer's dedup until that migration.

See ``docs/specs/02-domain-model.md`` §"Core entities (by
document)" (§09 row) and ``docs/specs/09-time-payroll-expenses.md``
§"Expense claims".
"""

from __future__ import annotations

from app.adapters.db.expenses.models import (
    ExpenseAttachment,
    ExpenseClaim,
    ExpenseLine,
)
from app.tenancy.registry import register

for _table in ("expense_claim", "expense_line", "expense_attachment"):
    register(_table)

__all__ = ["ExpenseAttachment", "ExpenseClaim", "ExpenseLine"]
