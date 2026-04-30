"""Instructions context — repository seam (cd-oyq).

Defines the seam :mod:`app.services.instructions.service` uses to
read and write :mod:`app.adapters.db.instructions.models` (Instruction,
InstructionVersion) plus the cross-package consistency reads it
needs against :mod:`app.adapters.db.places.models` (Area — property
mirror check) — without importing those modules directly.

Spec: ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 —
each context defines its own repository port in its public surface
(``app/domain/<context>/ports.py``). The SA concretion lives at
:mod:`app.adapters.db.instructions.repositories`.

Two seams live here:

* :class:`InstructionsRepository` — read + write seam for every row
  the service touches: instruction CRUD, version inserts +
  ``current_version_id`` re-pointing, the per-instruction max
  version-num lookup the bump helper rides, and the cross-context
  ``Area`` consistency probe (property mirror). Returns immutable
  :class:`InstructionRow` / :class:`InstructionVersionRow` /
  :class:`AreaParentRow` projections so the service never touches an
  ORM row.

The repo carries an open ``Session`` and never commits — the
caller's UoW owns the transaction boundary (§01 "Key runtime
invariants" #3). Mutating methods flush so the caller's audit-writer
FK reference (and any peer read in the same UoW) sees the new row.
The ``session`` property is exposed so the service can thread the
same UoW into :func:`app.audit.write_audit` (which still takes a
concrete ``Session`` today); the accessor drops once the audit
writer gains its own Protocol port — same shape as
:mod:`app.domain.expenses.ports`.

Protocols are deliberately **not** ``runtime_checkable``: structural
compatibility is checked statically by mypy. Runtime ``isinstance``
against these protocols would mask typos and invite duck-typing
shortcuts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

__all__ = [
    "AreaParentRow",
    "InstructionLinkRow",
    "InstructionResolutionRow",
    "InstructionRow",
    "InstructionVersionRow",
    "InstructionsRepository",
]


# ---------------------------------------------------------------------------
# Row + value-object shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstructionRow:
    """Immutable projection of an ``instruction`` row.

    Mirrors the column shape of
    :class:`app.adapters.db.instructions.models.Instruction` plus the
    derived ``property_id`` for area-scoped rows. Declared here so the
    SA adapter projects ORM rows into a domain-owned shape without
    forcing the service to import the ORM class.
    """

    id: str
    workspace_id: str
    slug: str
    title: str
    scope_kind: str
    scope_id: str | None
    property_id: str | None
    current_version_id: str | None
    tags: tuple[str, ...]
    archived_at: datetime | None
    created_by: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InstructionVersionRow:
    """Immutable projection of an ``instruction_version`` row.

    Mirrors the column shape of
    :class:`app.adapters.db.instructions.models.InstructionVersion`.
    """

    id: str
    workspace_id: str
    instruction_id: str
    version_num: int
    body_md: str
    body_hash: str
    author_id: str | None
    change_note: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InstructionResolutionRow:
    """Current live instruction body returned for resolver reads."""

    instruction_id: str
    current_revision_id: str
    body_md: str


@dataclass(frozen=True, slots=True)
class InstructionLinkRow:
    """Immutable projection of an ``instruction_link`` row."""

    id: str
    workspace_id: str
    instruction_id: str
    target_kind: str
    target_id: str
    added_by: str
    added_at: datetime


@dataclass(frozen=True, slots=True)
class AreaParentRow:
    """Immutable projection of an ``area`` row's identity + parent property.

    The instructions service only needs the ``id`` + ``property_id`` +
    a presence flag (``deleted_at``) for the area-scope consistency
    check. Other area columns (``label``, ``kind``, …) are not
    consulted by the service today; if a future caller needs them the
    seam grows then.
    """

    id: str
    property_id: str
    deleted_at: datetime | None


# ---------------------------------------------------------------------------
# InstructionsRepository
# ---------------------------------------------------------------------------


class InstructionsRepository(Protocol):
    """Read + write seam for every row the instructions service touches.

    Hides every direct ORM read from the import surface of
    :mod:`app.services.instructions.service`. The SA-backed concretion
    in :mod:`app.adapters.db.instructions.repositories` walks three
    ORM classes:

    * :class:`~app.adapters.db.instructions.models.Instruction` — CRUD
      + scope / archive / current-version pointer mutation.
    * :class:`~app.adapters.db.instructions.models.InstructionVersion`
      — append-only inserts + per-instruction max version-num lookup.
    * :class:`~app.adapters.db.places.models.Area` — read-only
      consistency probe (the area-scope ``property_id`` mirror per
      spec §"instruction" constraint table).

    The repo carries an open ``Session`` so service callers that also
    need :func:`app.audit.write_audit` (which still takes a concrete
    ``Session`` today) can thread the same UoW without holding a
    second seam. Drops once the audit writer gains its own Protocol
    port.

    Mutating methods flush so the caller's audit-writer FK reference
    sees the new row.
    """

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session.

        Exposed for the service that needs to thread the same UoW
        through :func:`app.audit.write_audit`. Drops when the audit
        writer gains its own Protocol port.
        """
        ...

    # -- Reads -----------------------------------------------------------

    def get_instruction(
        self, *, workspace_id: str, instruction_id: str
    ) -> InstructionRow | None:
        """Return the instruction row scoped to ``workspace_id`` or ``None``."""
        ...

    def get_instruction_for_update(
        self, *, workspace_id: str, instruction_id: str
    ) -> InstructionRow | None:
        """Return the instruction row with a write lock when supported.

        Used by version-bump writes so concurrent body edits serialize
        before allocating the next ``version_num``. Dialects that do
        not support row-level locks may ignore the lock hint; the DB
        UNIQUE on ``(instruction_id, version_num)`` remains the final
        guard.
        """
        ...

    def get_version(
        self, *, workspace_id: str, version_id: str
    ) -> InstructionVersionRow | None:
        """Return the version row scoped to ``workspace_id`` or ``None``."""
        ...

    def get_max_version_num(self, *, workspace_id: str, instruction_id: str) -> int:
        """Return the highest ``version_num`` recorded for this instruction.

        Returns ``0`` when no versions exist (the create path mints
        v1; the bump path mints ``current + 1``).
        """
        ...

    def get_area(self, *, workspace_id: str, area_id: str) -> AreaParentRow | None:
        """Return the area row scoped to ``workspace_id`` or ``None``.

        Used by the scope-validation step on area-scoped instructions
        so the service can refuse a mismatched caller-supplied
        ``property_id``. Workspace scope is enforced via an active
        property share, and the parent property must still be live.

        Returns ``None`` when the area is unknown, soft-deleted, or
        belongs to a soft-deleted property — the service treats those
        as "this area doesn't exist for you".
        """
        ...

    def property_exists_in_workspace(
        self, *, workspace_id: str, property_id: str
    ) -> bool:
        """Return ``True`` when ``property_id`` is a live property in this workspace.

        Used by the scope-validation step on property-scoped
        instructions to refuse a property id that does not exist in
        the caller's workspace (cross-tenant probe collapses to "not
        found" via the ``False`` return).
        """
        ...

    def list_live_current_by_scope(
        self,
        *,
        workspace_id: str,
        scope_kind: str,
        scope_id: str | None,
    ) -> Sequence[InstructionResolutionRow]:
        """Return live instructions for one scope joined to their current body."""
        ...

    def list_live_current_by_link(
        self,
        *,
        workspace_id: str,
        target_kind: str,
        target_id: str,
    ) -> Sequence[InstructionResolutionRow]:
        """Return live instructions linked to one target joined to current body."""
        ...

    # -- Writes ----------------------------------------------------------

    def insert_instruction(
        self,
        *,
        instruction_id: str,
        workspace_id: str,
        slug: str,
        title: str,
        scope_kind: str,
        scope_id: str | None,
        tags: Sequence[str],
        created_by: str | None,
        created_at: datetime,
    ) -> InstructionRow:
        """Insert a fresh instruction row with ``current_version_id = NULL``.

        The service immediately calls :meth:`insert_version` and then
        :meth:`set_current_version` to flip the pointer atomically
        within the same UoW. Flushes so the audit writer's FK
        reference sees the new row.
        """
        ...

    def insert_version(
        self,
        *,
        version_id: str,
        workspace_id: str,
        instruction_id: str,
        version_num: int,
        body_md: str,
        body_hash: str,
        author_id: str | None,
        change_note: str | None,
        created_at: datetime,
    ) -> InstructionVersionRow:
        """Insert one ``instruction_version`` row and return its projection.

        Append-only: a version row is never rewritten, only superseded
        when the instruction's ``current_version_id`` flips. Flushes
        so the audit writer's FK reference (and the immediate
        :meth:`set_current_version` follow-up) sees the new row.
        """
        ...

    def set_current_version(
        self,
        *,
        workspace_id: str,
        instruction_id: str,
        version_id: str,
    ) -> InstructionRow:
        """Re-point ``Instruction.current_version_id`` at ``version_id``.

        Flushes; returns the refreshed projection. The caller is
        responsible for ensuring ``version_id`` belongs to
        ``instruction_id`` — the soft-ref column has no FK so the
        repo cannot enforce it.
        """
        ...

    def update_metadata(
        self,
        *,
        workspace_id: str,
        instruction_id: str,
        title: str | None,
        scope_kind: str | None,
        scope_id: str | None,
        scope_id_provided: bool,
        tags: Sequence[str] | None,
    ) -> InstructionRow:
        """Rewrite the supplied metadata columns and flush.

        Only the provided columns are touched; absent ones leave the
        row's column intact. ``scope_id_provided`` distinguishes
        "caller did not send scope" (leave ``scope_id`` alone) from
        "caller sent global scope" (set ``scope_id = NULL`` even
        though the value is ``None``). The mirror invariant
        (``scope_kind = workspace`` ⇒ ``scope_id IS NULL``) is the
        service's responsibility to enforce upstream.
        """
        ...

    def set_archived_at(
        self,
        *,
        workspace_id: str,
        instruction_id: str,
        archived_at: datetime | None,
    ) -> InstructionRow:
        """Stamp (or clear) ``archived_at`` and flush; return refreshed projection.

        ``None`` clears the tombstone (a future un-archive seam
        — out of scope for cd-oyq, kept here so the seam is
        symmetrical when that work lands).
        """
        ...

    def insert_instruction_link(
        self,
        *,
        link_id: str,
        workspace_id: str,
        instruction_id: str,
        target_kind: str,
        target_id: str,
        added_by: str,
        added_at: datetime,
    ) -> InstructionLinkRow:
        """Insert an explicit instruction link and return its projection."""
        ...
