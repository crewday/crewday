"""SA-backed repository implementing the instructions-context Protocol seam.

The concrete class here adapts SQLAlchemy ``Session`` work to the
:class:`~app.domain.instructions.ports.InstructionsRepository` surface:

* :class:`SqlAlchemyInstructionsRepository` — wraps every read/write the
  :mod:`app.services.instructions.service` module needs against
  :mod:`app.adapters.db.instructions.models` plus the cross-package
  read for area-scope mirror checks
  (:mod:`app.adapters.db.places.models`).

Reaches into the ``places`` adapter package directly. Adapter-to-adapter
imports are allowed by the import-linter — only ``app.domain →
app.adapters`` is forbidden.

The repo carries an open ``Session`` and never commits — the caller's
UoW owns the transaction boundary (§01 "Key runtime invariants" #3).
Mutating methods flush so the caller's audit-writer FK reference (and
any peer read in the same UoW) sees the new row.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.adapters.db.instructions.models import (
    Instruction,
    InstructionLink,
    InstructionVersion,
)
from app.adapters.db.places.models import Area, Property, PropertyWorkspace
from app.domain.instructions.ports import (
    AreaParentRow,
    InstructionLinkRow,
    InstructionResolutionRow,
    InstructionRow,
    InstructionsRepository,
    InstructionVersionRow,
)

__all__ = ["SqlAlchemyInstructionsRepository"]


def _ensure_utc(value: datetime) -> datetime:
    """Tag a naive ``datetime`` as UTC (SQLite roundtrip strips tzinfo).

    The cross-backend invariant ("time is UTC at rest") lets us tag a
    naive value as UTC without guessing. The PG dialect retains tzinfo
    so the ``replace`` is a no-op there.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _ensure_utc_optional(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _ensure_utc(value)


def _to_instruction_row(
    row: Instruction, *, property_id: str | None = None
) -> InstructionRow:
    return InstructionRow(
        id=row.id,
        workspace_id=row.workspace_id,
        slug=row.slug,
        title=row.title,
        scope_kind=row.scope_kind,
        scope_id=row.scope_id,
        property_id=property_id,
        current_version_id=row.current_version_id,
        # ``tags`` is a JSON list on the column; surface it as an
        # immutable tuple so callers can't mutate the projection.
        tags=tuple(row.tags),
        archived_at=_ensure_utc_optional(row.archived_at),
        created_by=row.created_by,
        created_at=_ensure_utc(row.created_at),
    )


def _to_version_row(row: InstructionVersion) -> InstructionVersionRow:
    return InstructionVersionRow(
        id=row.id,
        workspace_id=row.workspace_id,
        instruction_id=row.instruction_id,
        version_num=row.version_num,
        body_md=row.body_md,
        body_hash=row.body_hash,
        author_id=row.author_id,
        change_note=row.change_note,
        created_at=_ensure_utc(row.created_at),
    )


def _to_resolution_row(
    instruction: Instruction, version: InstructionVersion
) -> InstructionResolutionRow:
    if instruction.current_version_id is None:
        raise LookupError(
            f"instruction {instruction.id!r} has no current version in resolver read"
        )
    return InstructionResolutionRow(
        instruction_id=instruction.id,
        current_revision_id=instruction.current_version_id,
        body_md=version.body_md,
    )


def _to_link_row(row: InstructionLink) -> InstructionLinkRow:
    return InstructionLinkRow(
        id=row.id,
        workspace_id=row.workspace_id,
        instruction_id=row.instruction_id,
        target_kind=row.target_kind,
        target_id=row.target_id,
        added_by=row.added_by,
        added_at=_ensure_utc(row.added_at),
    )


def _to_area_parent_row(row: Area) -> AreaParentRow:
    return AreaParentRow(
        id=row.id,
        property_id=row.property_id,
        deleted_at=_ensure_utc_optional(row.deleted_at),
    )


class SqlAlchemyInstructionsRepository(InstructionsRepository):
    """SA-backed repository for the instructions context.

    Implements every method on
    :class:`~app.domain.instructions.ports.InstructionsRepository`
    against a single ``Session``. Never commits; mutating methods
    flush so the caller's audit row sees the new entity_id.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # -- Reads -----------------------------------------------------------

    def get_instruction(
        self, *, workspace_id: str, instruction_id: str
    ) -> InstructionRow | None:
        row = self._load_instruction_or_none(
            workspace_id=workspace_id,
            instruction_id=instruction_id,
            for_update=False,
        )
        return None if row is None else self._to_instruction_row(row)

    def get_instruction_for_update(
        self, *, workspace_id: str, instruction_id: str
    ) -> InstructionRow | None:
        row = self._load_instruction_or_none(
            workspace_id=workspace_id,
            instruction_id=instruction_id,
            for_update=True,
        )
        return None if row is None else self._to_instruction_row(row)

    def get_version(
        self, *, workspace_id: str, version_id: str
    ) -> InstructionVersionRow | None:
        stmt = select(InstructionVersion).where(
            InstructionVersion.id == version_id,
            InstructionVersion.workspace_id == workspace_id,
        )
        row = self._session.scalars(stmt).one_or_none()
        return None if row is None else _to_version_row(row)

    def list_versions(
        self,
        *,
        workspace_id: str,
        instruction_id: str,
        limit: int,
        cursor_id: str | None,
    ) -> Sequence[InstructionVersionRow]:
        stmt = select(InstructionVersion).where(
            InstructionVersion.workspace_id == workspace_id,
            InstructionVersion.instruction_id == instruction_id,
        )
        if cursor_id is not None:
            cursor = self.get_version(workspace_id=workspace_id, version_id=cursor_id)
            if cursor is None or cursor.instruction_id != instruction_id:
                return ()
            stmt = stmt.where(InstructionVersion.version_num < cursor.version_num)
        stmt = stmt.order_by(
            InstructionVersion.version_num.desc(),
            InstructionVersion.id.desc(),
        ).limit(limit)
        return [_to_version_row(row) for row in self._session.scalars(stmt).all()]

    def get_max_version_num(self, *, workspace_id: str, instruction_id: str) -> int:
        stmt = select(func.coalesce(func.max(InstructionVersion.version_num), 0)).where(
            InstructionVersion.instruction_id == instruction_id,
            InstructionVersion.workspace_id == workspace_id,
        )
        result = self._session.execute(stmt).scalar_one()
        # ``func.max`` on an empty table returns ``None``; the
        # ``coalesce`` collapses that to ``0`` so the bump arithmetic
        # below has no special case for "no versions yet".
        return int(result)

    def get_area(self, *, workspace_id: str, area_id: str) -> AreaParentRow | None:
        # ``area`` carries no ``workspace_id`` column directly; the
        # parent ``property`` is workspace-agnostic — workspace
        # membership lives in :class:`PropertyWorkspace`. The join
        # enforces "this workspace must hold an active share of the
        # area's parent property"; an area whose parent property lives
        # only in a different workspace's share-set returns ``None``.
        stmt = (
            select(Area)
            .join(Property, Property.id == Area.property_id)
            .join(PropertyWorkspace, PropertyWorkspace.property_id == Area.property_id)
            .where(
                Area.id == area_id,
                PropertyWorkspace.workspace_id == workspace_id,
                PropertyWorkspace.status == "active",
                Property.deleted_at.is_(None),
            )
        )
        row = self._session.scalars(stmt).one_or_none()
        if row is None or row.deleted_at is not None:
            return None
        return _to_area_parent_row(row)

    def property_exists_in_workspace(
        self, *, workspace_id: str, property_id: str
    ) -> bool:
        # ``property`` is not workspace-scoped — workspace membership
        # lives in :class:`PropertyWorkspace`. We check the
        # active-share row exists, AND the underlying property row is
        # not soft-deleted.
        stmt = (
            select(Property.id)
            .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
            .where(
                Property.id == property_id,
                PropertyWorkspace.workspace_id == workspace_id,
                PropertyWorkspace.status == "active",
                Property.deleted_at.is_(None),
            )
        )
        return self._session.scalars(stmt).first() is not None

    def list_live_current_by_scope(
        self,
        *,
        workspace_id: str,
        scope_kind: str,
        scope_id: str | None,
    ) -> Sequence[InstructionResolutionRow]:
        stmt = self._live_current_stmt(workspace_id=workspace_id).where(
            Instruction.scope_kind == scope_kind
        )
        if scope_id is None:
            stmt = stmt.where(Instruction.scope_id.is_(None))
        else:
            stmt = stmt.where(Instruction.scope_id == scope_id)
        stmt = stmt.order_by(Instruction.created_at.asc(), Instruction.id.asc())
        return [
            _to_resolution_row(inst, version)
            for inst, version in self._session.execute(stmt).all()
        ]

    def list_live_current_by_link(
        self,
        *,
        workspace_id: str,
        target_kind: str,
        target_id: str,
    ) -> Sequence[InstructionResolutionRow]:
        stmt = (
            self._live_current_stmt(workspace_id=workspace_id)
            .join(
                InstructionLink,
                InstructionLink.instruction_id == Instruction.id,
            )
            .where(
                InstructionLink.workspace_id == workspace_id,
                InstructionLink.target_kind == target_kind,
                InstructionLink.target_id == target_id,
            )
            .order_by(InstructionLink.added_at.asc(), InstructionLink.id.asc())
        )
        return [
            _to_resolution_row(inst, version)
            for inst, version in self._session.execute(stmt).all()
        ]

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
        row = Instruction(
            id=instruction_id,
            workspace_id=workspace_id,
            slug=slug,
            title=title,
            scope_kind=scope_kind,
            scope_id=scope_id,
            current_version_id=None,
            tags=list(tags),
            archived_at=None,
            created_by=created_by,
            created_at=created_at,
        )
        self._session.add(row)
        self._session.flush()
        return self._to_instruction_row(row)

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
        row = InstructionVersion(
            id=version_id,
            workspace_id=workspace_id,
            instruction_id=instruction_id,
            version_num=version_num,
            body_md=body_md,
            body_hash=body_hash,
            author_id=author_id,
            change_note=change_note,
            created_at=created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _to_version_row(row)

    def set_current_version(
        self,
        *,
        workspace_id: str,
        instruction_id: str,
        version_id: str,
    ) -> InstructionRow:
        row = self._load_instruction_orm(
            workspace_id=workspace_id, instruction_id=instruction_id
        )
        row.current_version_id = version_id
        self._session.flush()
        return self._to_instruction_row(row)

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
        row = self._load_instruction_orm(
            workspace_id=workspace_id, instruction_id=instruction_id
        )
        if title is not None:
            row.title = title
        if scope_kind is not None:
            row.scope_kind = scope_kind
        if scope_id_provided:
            row.scope_id = scope_id
        if tags is not None:
            row.tags = list(tags)
        self._session.flush()
        return self._to_instruction_row(row)

    def set_archived_at(
        self,
        *,
        workspace_id: str,
        instruction_id: str,
        archived_at: datetime | None,
    ) -> InstructionRow:
        row = self._load_instruction_orm(
            workspace_id=workspace_id, instruction_id=instruction_id
        )
        row.archived_at = archived_at
        self._session.flush()
        return self._to_instruction_row(row)

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
        if (
            self.get_instruction(
                workspace_id=workspace_id, instruction_id=instruction_id
            )
            is None
        ):
            raise LookupError(
                f"instruction {instruction_id!r} not found in workspace "
                f"{workspace_id!r}"
            )
        row = InstructionLink(
            id=link_id,
            workspace_id=workspace_id,
            instruction_id=instruction_id,
            target_kind=target_kind,
            target_id=target_id,
            added_by=added_by,
            added_at=added_at,
        )
        self._session.add(row)
        self._session.flush()
        return _to_link_row(row)

    # -- Internal helpers -----------------------------------------------

    def _load_instruction_orm(
        self, *, workspace_id: str, instruction_id: str
    ) -> Instruction:
        """Return the ORM row for in-place mutation or raise ``LookupError``.

        The service layer always pre-checks existence via
        :meth:`get_instruction` before calling a mutating method, so
        a missing row here is a programming error — bare
        :class:`LookupError` surfaces it as a 500 rather than a 404.
        """
        row = self._load_instruction_or_none(
            workspace_id=workspace_id,
            instruction_id=instruction_id,
            for_update=False,
        )
        if row is None:
            raise LookupError(
                f"instruction {instruction_id!r} not found in workspace "
                f"{workspace_id!r} — service should have rejected upstream"
            )
        return row

    def _load_instruction_or_none(
        self, *, workspace_id: str, instruction_id: str, for_update: bool
    ) -> Instruction | None:
        stmt = select(Instruction).where(
            Instruction.id == instruction_id,
            Instruction.workspace_id == workspace_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self._session.scalars(stmt).one_or_none()

    def _live_current_stmt(
        self, *, workspace_id: str
    ) -> Select[tuple[Instruction, InstructionVersion]]:
        return (
            select(Instruction, InstructionVersion)
            .join(
                InstructionVersion,
                (InstructionVersion.id == Instruction.current_version_id)
                & (InstructionVersion.instruction_id == Instruction.id),
            )
            .where(
                Instruction.workspace_id == workspace_id,
                Instruction.archived_at.is_(None),
                Instruction.current_version_id.is_not(None),
                InstructionVersion.workspace_id == workspace_id,
            )
        )

    def _to_instruction_row(self, row: Instruction) -> InstructionRow:
        property_id: str | None = None
        if row.scope_kind == "property":
            property_id = row.scope_id
        elif row.scope_kind == "area" and row.scope_id is not None:
            property_id = self._session.scalar(
                select(Area.property_id).where(
                    Area.id == row.scope_id,
                    Area.deleted_at.is_(None),
                )
            )
        return _to_instruction_row(row, property_id=property_id)
