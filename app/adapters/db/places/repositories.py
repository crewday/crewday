"""SA-backed repositories implementing :mod:`app.domain.places.ports`.

The concrete class here adapts SQLAlchemy ``Session`` work to the
Protocol surface
:mod:`app.domain.places.property_work_role_assignments` consumes
(cd-kezq):

* :class:`SqlAlchemyPropertyWorkRoleAssignmentRepository` — wraps the
  ``property_work_role_assignment`` table plus the
  ``user_work_role`` / ``property_workspace`` validity lookups the
  domain service runs as §02 / §05 invariants.

Reaches into both :mod:`app.adapters.db.places.models` (for
``property_work_role_assignment`` + ``property_workspace`` rows) and
:mod:`app.adapters.db.workspace.models` (for the ``UserWorkRole``
validity lookup that backs
:func:`~app.domain.places.property_work_role_assignments._assert_user_work_role_in_workspace`).
Adapter-to-adapter imports are allowed by the import-linter — only
``app.domain → app.adapters`` is forbidden.

The repo carries an open ``Session`` and never commits beyond what
the underlying statements require — the caller's UoW owns the
transaction boundary (§01 "Key runtime invariants" #3). Mutating
methods flush so a peer read in the same UoW (and the audit
writer's FK reference to ``entity_id``) sees the new row.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.places.models import (
    PropertyWorkRoleAssignment,
    PropertyWorkspace,
)
from app.adapters.db.workspace.models import UserWorkRole
from app.domain.places.ports import (
    AssignmentIntegrityError,
    DuplicateActiveAssignment,
    PropertyWorkRoleAssignmentRepository,
    PropertyWorkRoleAssignmentRow,
)

__all__ = [
    "SqlAlchemyPropertyWorkRoleAssignmentRepository",
]


def _to_row(row: PropertyWorkRoleAssignment) -> PropertyWorkRoleAssignmentRow:
    """Project an ORM ``PropertyWorkRoleAssignment`` into the seam-level row.

    Field-by-field copy — :class:`PropertyWorkRoleAssignmentRow` is
    frozen so the domain never mutates the ORM-managed instance
    through a shared reference.
    """
    return PropertyWorkRoleAssignmentRow(
        id=row.id,
        workspace_id=row.workspace_id,
        user_work_role_id=row.user_work_role_id,
        property_id=row.property_id,
        schedule_ruleset_id=row.schedule_ruleset_id,
        property_pay_rule_id=row.property_pay_rule_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _is_role_property_unique_violation(exc: IntegrityError) -> bool:
    """Return ``True`` iff ``exc`` is the partial UNIQUE on (role, property).

    Tightens the integrity-error classification so a stray PK
    collision (vanishingly unlikely with ULIDs but still distinct
    on the wire) is not mis-tagged as a duplicate-active row.
    Postgres surfaces the index name; SQLite the column tuple. We
    accept either signature — the matcher is the same one the
    pre-refactor service module carried (cd-kezq moved it from
    :mod:`app.domain.places.property_work_role_assignments` so the
    classification stays adjacent to the IntegrityError site).
    """
    message = str(exc).lower()
    if "uq_property_work_role_assignment_role_property_active" in message:
        return True
    # SQLite text shape: ``UNIQUE constraint failed:
    # property_work_role_assignment.user_work_role_id,
    # property_work_role_assignment.property_id``.
    return (
        "unique constraint" in message
        and "user_work_role_id" in message
        and "property_id" in message
    )


class SqlAlchemyPropertyWorkRoleAssignmentRepository(
    PropertyWorkRoleAssignmentRepository
):
    """SA-backed concretion of :class:`PropertyWorkRoleAssignmentRepository`.

    Wraps an open :class:`~sqlalchemy.orm.Session` and never commits
    outside what the underlying statements require — the caller's
    UoW owns the transaction boundary (§01 "Key runtime invariants"
    #3).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # -- Parent / sibling lookups ---------------------------------------

    def user_work_role_exists_in_workspace(
        self, *, workspace_id: str, user_work_role_id: str
    ) -> bool:
        # ``workspace_id`` predicate is defence-in-depth on top of the
        # ORM tenant filter — a misconfigured filter must fail loud,
        # not silently. ``deleted_at IS NULL`` keeps soft-deleted
        # parents invisible (matches §02 invariant 1).
        row = self._session.scalar(
            select(UserWorkRole).where(
                UserWorkRole.id == user_work_role_id,
                UserWorkRole.workspace_id == workspace_id,
                UserWorkRole.deleted_at.is_(None),
            )
        )
        return row is not None

    def property_in_workspace(self, *, workspace_id: str, property_id: str) -> bool:
        # ``property_workspace`` is the workspace-tenancy junction for
        # ``property`` (which is itself shared across workspaces). A
        # live junction row is the only signal "this workspace
        # operates this property" (§02 "property_workspace").
        row = self._session.scalar(
            select(PropertyWorkspace).where(
                PropertyWorkspace.property_id == property_id,
                PropertyWorkspace.workspace_id == workspace_id,
            )
        )
        return row is not None

    # -- Reads -----------------------------------------------------------

    def get(
        self,
        *,
        workspace_id: str,
        assignment_id: str,
        include_deleted: bool = False,
    ) -> PropertyWorkRoleAssignmentRow | None:
        stmt = select(PropertyWorkRoleAssignment).where(
            PropertyWorkRoleAssignment.id == assignment_id,
            PropertyWorkRoleAssignment.workspace_id == workspace_id,
        )
        if not include_deleted:
            stmt = stmt.where(PropertyWorkRoleAssignment.deleted_at.is_(None))
        row = self._session.scalars(stmt).one_or_none()
        return _to_row(row) if row is not None else None

    def find_active_for_role_property(
        self,
        *,
        workspace_id: str,
        user_work_role_id: str,
        property_id: str,
    ) -> PropertyWorkRoleAssignmentRow | None:
        row = self._session.scalar(
            select(PropertyWorkRoleAssignment).where(
                PropertyWorkRoleAssignment.workspace_id == workspace_id,
                PropertyWorkRoleAssignment.user_work_role_id == user_work_role_id,
                PropertyWorkRoleAssignment.property_id == property_id,
                PropertyWorkRoleAssignment.deleted_at.is_(None),
            )
        )
        return _to_row(row) if row is not None else None

    def list(
        self,
        *,
        workspace_id: str,
        limit: int,
        after_id: str | None = None,
        property_id: str | None = None,
        user_work_role_id: str | None = None,
        include_deleted: bool = False,
    ) -> Sequence[PropertyWorkRoleAssignmentRow]:
        stmt = select(PropertyWorkRoleAssignment).where(
            PropertyWorkRoleAssignment.workspace_id == workspace_id,
        )
        if not include_deleted:
            stmt = stmt.where(PropertyWorkRoleAssignment.deleted_at.is_(None))
        if property_id is not None:
            stmt = stmt.where(PropertyWorkRoleAssignment.property_id == property_id)
        if user_work_role_id is not None:
            stmt = stmt.where(
                PropertyWorkRoleAssignment.user_work_role_id == user_work_role_id
            )
        if after_id is not None:
            stmt = stmt.where(PropertyWorkRoleAssignment.id > after_id)
        stmt = stmt.order_by(PropertyWorkRoleAssignment.id.asc()).limit(limit + 1)
        rows = self._session.scalars(stmt).all()
        return [_to_row(r) for r in rows]

    # -- Writes ----------------------------------------------------------

    def insert(
        self,
        *,
        assignment_id: str,
        workspace_id: str,
        user_work_role_id: str,
        property_id: str,
        schedule_ruleset_id: str | None,
        property_pay_rule_id: str | None,
        now: datetime,
    ) -> PropertyWorkRoleAssignmentRow:
        row = PropertyWorkRoleAssignment(
            id=assignment_id,
            workspace_id=workspace_id,
            user_work_role_id=user_work_role_id,
            property_id=property_id,
            schedule_ruleset_id=schedule_ruleset_id,
            property_pay_rule_id=property_pay_rule_id,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            # Two flavours of integrity violation can land here:
            #
            # 1. The partial UNIQUE
            #    ``uq_property_work_role_assignment_role_property_active``
            #    fires if a parallel insert beat the service's pre-flight
            #    SELECT. Rolled back + raised as
            #    :class:`DuplicateActiveAssignment` so the domain
            #    service can re-raise with the canonical "already
            #    exists" message the HTTP layer keys on for 409.
            # 2. The real FK on ``property_pay_rule_id`` (cascading
            #    from a ``pay_rule`` row) fires when the caller
            #    supplies a pointer that does not exist. Rolled back +
            #    raised as :class:`AssignmentIntegrityError` carrying
            #    the original driver message so the domain service can
            #    surface it as a 422 invariant violation without leaking
            #    the SA exception type.
            #
            # Distinguishing the two across SQLite + Postgres is
            # driver-dependent (see :func:`_is_role_property_unique_violation`
            # for the matcher).
            self._session.rollback()
            if _is_role_property_unique_violation(exc):
                raise DuplicateActiveAssignment(
                    f"property_work_role_assignment for "
                    f"user_work_role={user_work_role_id!r} "
                    f"property={property_id!r} already exists"
                ) from exc
            raise AssignmentIntegrityError(str(exc.orig)) from exc
        return _to_row(row)

    def update_pointers(
        self,
        *,
        workspace_id: str,
        assignment_id: str,
        schedule_ruleset_id: str | None,
        property_pay_rule_id: str | None,
        now: datetime,
    ) -> PropertyWorkRoleAssignmentRow:
        # Caller has already confirmed the row exists via :meth:`get`;
        # use the same workspace-scoped SELECT shape so the caller's
        # UoW reuses the identity-map entry rather than spawning a
        # second instance for the same primary key.
        row = self._session.scalars(
            select(PropertyWorkRoleAssignment).where(
                PropertyWorkRoleAssignment.id == assignment_id,
                PropertyWorkRoleAssignment.workspace_id == workspace_id,
                PropertyWorkRoleAssignment.deleted_at.is_(None),
            )
        ).one()

        # Service has already filtered the deltas; we just apply.
        # Both pointer fields are nullable and the caller may set
        # either to None (to clear the override), so a positional
        # write is correct — the caller's "did anything change?"
        # gate is what keeps zero-delta calls from reaching us.
        row.schedule_ruleset_id = schedule_ruleset_id
        row.property_pay_rule_id = property_pay_rule_id
        row.updated_at = now
        try:
            self._session.flush()
        except IntegrityError as exc:
            # The only realistic flush-time failure here is the FK on
            # ``property_pay_rule_id`` — a caller pointing at a
            # ``pay_rule`` row that does not exist or was hard-deleted.
            # Rolled back + raised as :class:`AssignmentIntegrityError`
            # so the domain service surfaces a 422 (not a 500). The
            # partial UNIQUE cannot fire on update because the
            # identity columns ``user_work_role_id`` + ``property_id``
            # are frozen at the DTO boundary.
            self._session.rollback()
            raise AssignmentIntegrityError(str(exc.orig)) from exc
        return _to_row(row)

    def soft_delete(
        self,
        *,
        workspace_id: str,
        assignment_id: str,
        now: datetime,
    ) -> PropertyWorkRoleAssignmentRow:
        row = self._session.scalars(
            select(PropertyWorkRoleAssignment).where(
                PropertyWorkRoleAssignment.id == assignment_id,
                PropertyWorkRoleAssignment.workspace_id == workspace_id,
                PropertyWorkRoleAssignment.deleted_at.is_(None),
            )
        ).one()

        row.deleted_at = now
        row.updated_at = now
        self._session.flush()
        return _to_row(row)
