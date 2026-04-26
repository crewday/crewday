"""Places context — repository port for the property-work-role-assignment seam.

Defines :class:`PropertyWorkRoleAssignmentRepository`, the seam
:mod:`app.domain.places.property_work_role_assignments` uses to read
and write the ``property_work_role_assignment`` rows + the
``user_work_role`` / ``property_workspace`` validity lookups —
without importing SQLAlchemy model classes (cd-kezq).

Spec: ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 —
each context defines its own repository port in its public surface
(``app/domain/<context>/ports.py``) and a SQLAlchemy adapter under
``app/adapters/db/<context>/`` (cd-jzfc reconciled the placement
introduced by cd-duv6, mirrored by cd-74pb's messaging seam). The
SA-backed concretion lives in
:mod:`app.adapters.db.places.repositories`; tests substitute fakes.

The repo carries an open SQLAlchemy ``Session`` so the audit writer
(:func:`app.audit.write_audit`) — which still takes a concrete
``Session`` today — can ride the same Unit of Work without forcing
callers to thread a second seam. Drops once the audit writer gains
its own Protocol.

The repo-shaped value object :class:`PropertyWorkRoleAssignmentRow`
mirrors the domain's
:class:`~app.domain.places.property_work_role_assignments.PropertyWorkRoleAssignmentView`.
It lives on the seam so the SA adapter has a domain-owned shape to
project ORM rows into without importing the service module that
produces the view (which would create a circular dependency between
``property_work_role_assignments`` and this module).

The :class:`DuplicateActiveAssignment` sentinel exception isolates
the partial-UNIQUE classification out of the SA layer; the domain
service catches it and re-raises as
:class:`PropertyWorkRoleAssignmentInvariantViolated` with the
canonical "already exists" wire message. The other integrity flavour
(missing-FK on ``property_pay_rule_id``) surfaces as
:class:`AssignmentIntegrityError` carrying the original DB driver
message — the service collapses it into the same domain exception
with a non-duplicate message so the HTTP layer's substring check keeps
mapping to 409 / 422 cleanly.

Protocol is deliberately **not** ``runtime_checkable``: structural
compatibility is checked statically by mypy. Runtime ``isinstance``
against this Protocol would mask typos and invite duck-typing
shortcuts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

__all__ = [
    "AssignmentIntegrityError",
    "DuplicateActiveAssignment",
    "PropertyWorkRoleAssignmentRepository",
    "PropertyWorkRoleAssignmentRow",
]


# ---------------------------------------------------------------------------
# Row shape (value object)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PropertyWorkRoleAssignmentRow:
    """Immutable projection of a ``property_work_role_assignment`` row.

    Mirrors the shape of
    :class:`app.domain.places.property_work_role_assignments.PropertyWorkRoleAssignmentView`;
    declared here so the Protocol surface does not depend on the
    service module (which itself imports this seam).
    """

    id: str
    workspace_id: str
    user_work_role_id: str
    property_id: str
    schedule_ruleset_id: str | None
    property_pay_rule_id: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


# ---------------------------------------------------------------------------
# Seam exceptions
# ---------------------------------------------------------------------------


class DuplicateActiveAssignment(Exception):
    """A live row already exists for the (user_work_role, property) pair.

    Raised by :meth:`PropertyWorkRoleAssignmentRepository.insert` when
    the partial UNIQUE
    ``uq_property_work_role_assignment_role_property_active`` fires —
    either because the SA adapter detected a parallel insert race or
    because a duplicate slipped past the service-layer pre-flight
    SELECT. The domain service catches and re-raises as
    :class:`~app.domain.places.property_work_role_assignments.PropertyWorkRoleAssignmentInvariantViolated`
    with the canonical "already exists" message that the HTTP router
    keys on for the 409 surface.
    """


class AssignmentIntegrityError(Exception):
    """A non-duplicate integrity error fired at flush time.

    Raised by :meth:`PropertyWorkRoleAssignmentRepository.insert` and
    :meth:`PropertyWorkRoleAssignmentRepository.update` for FK misses
    on ``property_pay_rule_id`` (the caller pointed at a ``pay_rule``
    row that does not exist) and any other DB-level integrity
    violation that is **not** the partial-UNIQUE on (role, property).
    The constructor takes the original driver message so the domain
    service can echo it back to the operator without leaking the
    underlying ORM exception type.
    """

    def __init__(self, db_message: str) -> None:
        super().__init__(db_message)
        self.db_message = db_message


# ---------------------------------------------------------------------------
# PropertyWorkRoleAssignmentRepository
# ---------------------------------------------------------------------------


class PropertyWorkRoleAssignmentRepository(Protocol):
    """Read + write seam for ``property_work_role_assignment`` and its parent lookups.

    The repo carries an open SQLAlchemy ``Session`` so domain callers
    that also need :func:`app.audit.write_audit` (which still takes a
    concrete ``Session`` today) can thread the same UoW without
    holding a second seam. The accessor drops once the audit writer
    gains its own Protocol port.

    Every method honours the workspace-scoping invariant: the SA
    concretion always pins reads + writes to the ``workspace_id``
    passed by the caller, mirroring the ORM tenant filter as
    defence-in-depth (a misconfigured filter must fail loud).

    The repo never commits outside what the underlying statements
    require — the caller's UoW owns the transaction boundary (§01
    "Key runtime invariants" #3). Methods that mutate state flush so
    the caller's next read (and the audit writer's FK reference to
    ``entity_id``) sees the new row.
    """

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session.

        Exposed for callers that need to thread the same UoW through
        :func:`app.audit.write_audit` (which still takes a concrete
        ``Session`` today). Drops when the audit writer gains its
        own Protocol port.
        """
        ...

    # -- Parent / sibling lookups (§02 / §05 invariants) ------------------

    def user_work_role_exists_in_workspace(
        self, *, workspace_id: str, user_work_role_id: str
    ) -> bool:
        """Return ``True`` iff the live ``user_work_role`` row exists in the workspace.

        Drives :func:`_assert_user_work_role_in_workspace`. A ``False``
        return collapses every miss flavour (unknown id, soft-deleted,
        cross-workspace borrow attempt) into one signal — the service
        does not need to distinguish them on the wire (§01 "tenant
        surface is not enumerable").
        """
        ...

    def property_in_workspace(self, *, workspace_id: str, property_id: str) -> bool:
        """Return ``True`` iff a live ``property_workspace`` row links the workspace.

        Drives :func:`_assert_property_in_workspace`. A workspace
        cannot pin a role to a property it does not operate (§02
        "property_work_role_assignment" invariant 2).
        """
        ...

    # -- Reads -----------------------------------------------------------

    def get(
        self,
        *,
        workspace_id: str,
        assignment_id: str,
        include_deleted: bool = False,
    ) -> PropertyWorkRoleAssignmentRow | None:
        """Return the row or ``None`` when invisible to the caller.

        Defence-in-depth pins the lookup to ``workspace_id`` even
        though the ORM tenant filter already narrows the read; a
        misconfigured filter must fail loud, not silently.
        ``include_deleted=True`` skips the ``deleted_at IS NULL``
        predicate — used by the soft-delete + tombstone-aware paths.
        """
        ...

    def find_active_for_role_property(
        self,
        *,
        workspace_id: str,
        user_work_role_id: str,
        property_id: str,
    ) -> PropertyWorkRoleAssignmentRow | None:
        """Return the live row for ``(role, property)`` or ``None``.

        Powers the create-path pre-flight check that surfaces a
        duplicate as the canonical "already exists" message before
        :meth:`insert` reaches the partial UNIQUE — a flush-time
        :class:`DuplicateActiveAssignment` would otherwise muddy
        the surface with a generic IntegrityError text.
        """
        ...

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
        """Return up to ``limit + 1`` rows for the workspace, ``id ASC``.

        The caller (a cursor-paginated router) asks for ``limit + 1``
        so the :func:`~app.api.pagination.paginate` helper can compute
        ``has_more`` without a second query. ``property_id`` and
        ``user_work_role_id`` narrow the listing independently —
        passing both gives the live row (or none) for that pair.
        """
        ...

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
        """Insert a new ``property_work_role_assignment`` row and return its projection.

        Flushes so the caller's next read (and the audit writer's
        FK reference to ``entity_id``) sees the new row.

        Raises:

        * :class:`DuplicateActiveAssignment` when the partial
          UNIQUE ``uq_property_work_role_assignment_role_property_active``
          fires — typically a parallel insert race that beat the
          service-layer pre-flight SELECT.
        * :class:`AssignmentIntegrityError` for every other integrity
          flavour (FK miss on ``property_pay_rule_id`` is the realistic
          one) — carries the original driver message so the domain
          service can echo it back.

        The SA concretion rolls back the open transaction on either
        failure so the caller's UoW can keep using the session.
        """
        ...

    def update_pointers(
        self,
        *,
        workspace_id: str,
        assignment_id: str,
        schedule_ruleset_id: str | None,
        property_pay_rule_id: str | None,
        now: datetime,
    ) -> PropertyWorkRoleAssignmentRow:
        """Apply the pointer-only partial update and return the refreshed projection.

        ``schedule_ruleset_id`` and ``property_pay_rule_id`` are both
        passed positionally — the service has already filtered to the
        actual deltas (zero-delta calls never reach this method). The
        identity columns (``user_work_role_id`` / ``property_id`` /
        ``workspace_id``) are deliberately frozen at the DTO boundary
        so the partial UNIQUE on (role, property) cannot fire here.

        Stamps ``updated_at = now`` and flushes.

        Raises:

        * :class:`AssignmentIntegrityError` when the FK on
          ``property_pay_rule_id`` fires — the caller supplied an id
          that does not match a live ``pay_rule`` row.
        """
        ...

    def soft_delete(
        self,
        *,
        workspace_id: str,
        assignment_id: str,
        now: datetime,
    ) -> PropertyWorkRoleAssignmentRow:
        """Stamp ``deleted_at`` + ``updated_at`` and return the tombstoned projection.

        Caller has already confirmed the row exists via :meth:`get`;
        the SA concretion loads the same row via the workspace-scoped
        SELECT so the identity-map entry is reused. Flushes so a peer
        read in the same UoW sees the tombstone.

        The partial UNIQUE on
        ``(user_work_role_id, property_id) WHERE deleted_at IS NULL``
        excludes tombstoned rows so a re-pin after archive mints a
        fresh row without colliding with the historical one.
        """
        ...
