"""Identity context — repository ports for permission groups + role grants.

Defines the seams :mod:`app.domain.identity.permission_groups` and
:mod:`app.domain.identity.role_grants` use to read and write
``permission_group`` / ``permission_group_member`` / ``role_grant``
without importing SQLAlchemy model classes directly.

Spec: ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 —
each context defines its own repository port in its public surface and
a SQLAlchemy adapter under ``app/adapters/db/<context>/``. The two
SA-backed concretions live in :mod:`app.adapters.db.authz.repositories`;
tests substitute fakes.

Two repositories live here:

* :class:`PermissionGroupRepository` — group + member CRUD
  (``permission_group`` and ``permission_group_member``).
* :class:`RoleGrantRepository` — role-grant CRUD plus the cross-
  workspace property-scope check used by the §05 owner-authority
  policy. Returns immutable :class:`RoleGrantRef` projections so
  the domain never sees an ORM row.

Both protocols expose a ``session`` accessor so callers that need
to thread the same UoW through a sibling helper (``app.audit.write_audit``,
``app.domain.identity._owner_guard.count_owner_members_locked``,
``app.authz.owners.is_owner_member``) can do so without holding a
second seam.

The repo-shaped value objects (:class:`PermissionGroupRow`,
:class:`PermissionGroupMemberRow`, :class:`RoleGrantRow`) mirror the
domain's matching refs (``PermissionGroupRef`` /
``PermissionGroupMemberRef`` / ``RoleGrantRef`` on
:mod:`app.domain.identity.permission_groups` and
:mod:`app.domain.identity.role_grants`). They live on the seam so
the SA adapter has a domain-owned shape to project ORM rows into
without importing the service modules that produce the refs (which
would create a circular dependency between ``permission_groups`` /
``role_grants`` and this module).

Protocols are deliberately **not** ``runtime_checkable``:
structural compatibility is checked statically by mypy. Runtime
``isinstance`` against these protocols would mask typos and invite
duck-typing shortcuts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session

__all__ = [
    "PermissionGroupMemberRow",
    "PermissionGroupRepository",
    "PermissionGroupRow",
    "PermissionGroupSlugTakenError",
    "RoleGrantRepository",
    "RoleGrantRow",
]


# ---------------------------------------------------------------------------
# Row shapes (value objects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PermissionGroupRow:
    """Immutable projection of a ``permission_group`` row.

    Mirrors the shape of
    :class:`app.domain.identity.permission_groups.PermissionGroupRef`;
    declared here so the Protocol surface does not depend on the
    service module (which itself imports this seam).
    """

    id: str
    slug: str
    name: str
    system: bool
    capabilities: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PermissionGroupMemberRow:
    """Immutable projection of a ``permission_group_member`` row."""

    group_id: str
    user_id: str
    added_at: datetime
    added_by_user_id: str | None


@dataclass(frozen=True, slots=True)
class RoleGrantRow:
    """Immutable projection of a workspace-scoped ``role_grant`` row.

    The deployment-scope partition (``scope_kind='deployment'``,
    ``workspace_id IS NULL``) is not represented here — it has its
    own admin surface; this seam is for workspace-scoped reads.
    """

    id: str
    workspace_id: str
    user_id: str
    grant_role: str
    scope_property_id: str | None
    binding_org_id: str | None
    created_at: datetime
    created_by_user_id: str | None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PermissionGroupSlugTakenError(Exception):
    """The ``(workspace_id, slug)`` uniqueness constraint rejected an insert.

    The SA-backed concretion wraps the SQLAlchemy ``IntegrityError``
    raised by the unique-constraint violation in this seam-level
    exception so the domain layer can ``except`` on it without
    importing :class:`sqlalchemy.exc.IntegrityError`. The repo wraps
    its insert in a SAVEPOINT so the caller's outer transaction
    survives the rollback (§02 "permission_group" §"Invariants" — a
    failed mint must not nuke prior writes in the same UoW).

    The domain re-raises this as the public-surface
    :class:`app.domain.identity.permission_groups.PermissionGroupSlugTaken`
    so HTTP handlers keep their existing 409 mapping.
    """


# ---------------------------------------------------------------------------
# PermissionGroupRepository
# ---------------------------------------------------------------------------


class PermissionGroupRepository(Protocol):
    """Read + write seam for ``permission_group`` and its membership rows.

    The repo carries an open SQLAlchemy ``Session`` so domain callers
    that also need the audit writer (``app.audit.write_audit``) and
    the locking primitive
    (``app.domain.identity._owner_guard.count_owner_members_locked``)
    can thread the same UoW without juggling a second seam — both
    helpers take a session today.

    Every method honours the workspace-scoping invariant: the SA
    concretion always pins reads + writes to the ``workspace_id``
    passed by the caller, mirroring the ORM tenant filter as
    defence-in-depth (a misconfigured filter must fail loud).

    The repo never commits or flushes outside what the underlying
    statements require — the caller's UoW owns the transaction
    boundary (§01 "Key runtime invariants" #3).
    """

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session.

        Exposed for callers that need to thread the same UoW through
        ``app.audit.write_audit`` (which still takes a concrete
        ``Session`` today) and the cross-dialect locking primitive in
        :mod:`app.domain.identity._owner_guard`. Once those helpers
        gain their own Protocol seams (cd-mb5n / a future audit seam
        task) the accessor can drop.
        """
        ...

    # -- Group reads -----------------------------------------------------

    def list_groups(self, *, workspace_id: str) -> Sequence[PermissionGroupRow]:
        """Return every group in ``workspace_id`` ordered by creation."""
        ...

    def get_group(
        self, *, workspace_id: str, group_id: str
    ) -> PermissionGroupRow | None:
        """Return ``group_id`` scoped to ``workspace_id`` or ``None`` if absent."""
        ...

    # -- Group writes ----------------------------------------------------

    def insert_group(
        self,
        *,
        group_id: str,
        workspace_id: str,
        slug: str,
        name: str,
        system: bool,
        capabilities: dict[str, Any],
        created_at: datetime,
    ) -> PermissionGroupRow:
        """Insert a fresh group inside a SAVEPOINT.

        Raises :class:`PermissionGroupSlugTakenError` if the
        ``(workspace_id, slug)`` UNIQUE rejects the insert. The
        SAVEPOINT keeps the caller's outer transaction alive so prior
        writes in the same UoW survive the failed mint.
        """
        ...

    def update_group(
        self,
        *,
        workspace_id: str,
        group_id: str,
        name: str | None = None,
        capabilities: dict[str, Any] | None = None,
    ) -> PermissionGroupRow:
        """Mutate ``name`` and/or ``capabilities`` and return the new shape.

        Caller is responsible for the system-group + unknown-capability
        guards; this method is a pure SA write. Flushes so the caller's
        next read sees the new values.
        """
        ...

    def delete_group(self, *, workspace_id: str, group_id: str) -> None:
        """Hard-delete the group; member rows cascade via FK.

        Caller is responsible for the system-group guard.
        """
        ...

    # -- Member reads ----------------------------------------------------

    def list_members(
        self, *, workspace_id: str, group_id: str
    ) -> Sequence[PermissionGroupMemberRow]:
        """Return every explicit member of ``group_id`` ordered by addition."""
        ...

    def get_member(
        self, *, group_id: str, user_id: str
    ) -> PermissionGroupMemberRow | None:
        """Return the ``(group_id, user_id)`` row or ``None`` if missing.

        Used by the idempotency check in :func:`add_member` and the
        last-owner gate in :func:`remove_member` — both need to know
        whether the membership row actually exists before deciding
        whether to write.
        """
        ...

    # -- Member writes ---------------------------------------------------

    def insert_member(
        self,
        *,
        group_id: str,
        user_id: str,
        workspace_id: str,
        added_at: datetime,
        added_by_user_id: str | None,
    ) -> PermissionGroupMemberRow:
        """Insert a fresh ``(group_id, user_id)`` membership row.

        Caller is responsible for the idempotency check (the composite
        PK would otherwise trip ``IntegrityError`` and poison the
        caller's UoW).
        """
        ...

    def delete_member(self, *, group_id: str, user_id: str) -> None:
        """Hard-delete the ``(group_id, user_id)`` membership row.

        Idempotent: a missing row is a silent no-op. The SA-backed
        concretion looks the row up before issuing the DELETE so a
        stale "remove me again" doesn't trip an :class:`UnmappedInstanceError`
        at flush. The caller's audit row still records the intent.
        """
        ...


# ---------------------------------------------------------------------------
# RoleGrantRepository
# ---------------------------------------------------------------------------


class RoleGrantRepository(Protocol):
    """Read + write seam for the workspace-scoped ``role_grant`` rows.

    Carries the same ``session`` accessor as
    :class:`PermissionGroupRepository` so the domain can thread the
    same UoW through ``app.audit.write_audit``,
    ``app.authz.owners.is_owner_member`` and
    ``app.domain.identity._owner_guard.count_owner_members_locked``.

    The cross-workspace property check
    (:meth:`is_property_in_workspace`) lives on this repo because
    :mod:`app.domain.identity.role_grants` is the only domain module
    that needs it; pulling it out into a sibling
    :mod:`app.domain.places.ports` would force two repos through
    every call site for one boolean. The SA concretion in
    :mod:`app.adapters.db.authz.repositories` reaches into
    :mod:`app.adapters.db.places.models` directly — adapter-to-adapter
    is allowed by the import-linter contract.
    """

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session.

        See :meth:`PermissionGroupRepository.session` for the
        rationale; the same audit / lock / owners-helper threading
        applies here.
        """
        ...

    # -- Reads -----------------------------------------------------------

    def list_grants(
        self,
        *,
        workspace_id: str,
        user_id: str | None = None,
        scope_property_id: str | None = None,
    ) -> Sequence[RoleGrantRow]:
        """Return every workspace-scoped grant matching the filters.

        Ordered by ``created_at`` ascending with ``id`` as a stable
        tiebreaker so the seeded owner grant always leads.
        """
        ...

    def get_grant(self, *, workspace_id: str, grant_id: str) -> RoleGrantRow | None:
        """Return the named grant scoped to ``workspace_id`` or ``None``."""
        ...

    def has_active_manager_grant(self, *, workspace_id: str, user_id: str) -> bool:
        """Return ``True`` iff ``user_id`` holds a ``manager`` grant here.

        v1 has no ``revoked_at`` column on ``role_grant``; any row
        with ``grant_role='manager'`` in the workspace counts as an
        active manager grant for the §05 owner-authority policy.
        """
        ...

    def is_property_in_workspace(self, *, workspace_id: str, property_id: str) -> bool:
        """Return ``True`` iff ``property_id`` is linked to ``workspace_id``.

        The check runs against ``property_workspace`` — the
        authoritative junction table for "this property belongs to
        this workspace" (§02 "property_workspace"). An unknown
        property id returns ``False`` the same way a sibling-workspace
        id does; the caller maps both to a single
        ``CrossWorkspaceProperty`` error.
        """
        ...

    # -- Writes ----------------------------------------------------------

    def insert_grant(
        self,
        *,
        grant_id: str,
        workspace_id: str,
        user_id: str,
        grant_role: str,
        scope_property_id: str | None,
        created_at: datetime,
        created_by_user_id: str | None,
    ) -> RoleGrantRow:
        """Insert a fresh workspace-scoped ``role_grant`` row."""
        ...

    def delete_grant(self, *, workspace_id: str, grant_id: str) -> None:
        """Hard-delete the named grant.

        Caller is responsible for the last-owner protection (§05
        "Surface grants at a glance"); v1 has no ``revoked_at``
        column so the row is removed outright.
        """
        ...
