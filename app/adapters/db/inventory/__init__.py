"""inventory — inventory_item / inventory_movement / inventory_reorder_rule.

All three tables in this package are workspace-scoped: each row
carries a ``workspace_id`` column and is registered in
:mod:`app.tenancy.registry` so the ORM tenant filter auto-injects a
``workspace_id`` predicate on every SELECT / UPDATE / DELETE. A
bare read without a :class:`~app.tenancy.WorkspaceContext` raises
:class:`~app.tenancy.orm_filter.TenantFilterMissing`.

``item_id`` FKs on ``inventory_movement`` and
``inventory_reorder_rule`` cascade on delete — the two child rows
are dependent on their parent :class:`Item`. A reorder rule and the
ledger of movements have no meaning once the item itself is gone;
the normal archive path is the ``deleted_at`` soft-delete on the
:class:`Item` row (cd-jkwr adds that column), not a hard DELETE.

``occurrence_id`` on :class:`Movement` is a plain :class:`str`
soft-ref (no SQL foreign key) to sidestep the cross-package coupling
that a hard FK into ``task_occurrence`` / ``task`` would introduce —
the column points at whichever occurrence identifier §06 settles on,
and the domain layer resolves it. ``created_by`` is likewise a
soft-ref because the actor may be a system process (the
consume-on-task worker, an agent issuing an asset-action consumption
row).

Spec §02 lists a richer ``inventory_movement_reason`` enum
(``restock | consume | adjust | waste | transfer_in | transfer_out |
audit_correction``) and spec §08 a richer unit vocabulary (``each |
pack | kg | liter | roll``). The v1 slice here clamps the narrower
taxonomy cd-bxt pins — ``receive | issue | adjust | consume`` for
the reason, ``ea | l | kg | m | pkg | box | other`` for the unit —
matching what the cd-jkwr (item CRUD) and downstream follow-ups need
first. Widening to the full §02 / §08 surface without rewriting
history is a one-line CHECK-list change in a later migration. A
:class:`ItemVariant` sibling table was in the task title but neither
spec §02 nor §08 describes one, so no variant table ships in this
slice — the pack-size / flavour axis lands when the spec names it.

See ``docs/specs/02-domain-model.md`` §"inventory_item",
§"inventory_movement", and ``docs/specs/08-inventory.md``.
"""

from __future__ import annotations

from app.adapters.db.inventory.models import Item, Movement, ReorderRule
from app.tenancy.registry import register

for _table in ("inventory_item", "inventory_movement", "inventory_reorder_rule"):
    register(_table)

__all__ = ["Item", "Movement", "ReorderRule"]
