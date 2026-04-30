"""SA-backed repositories implementing :mod:`app.domain.identity.ports`.

The two concrete classes here adapt SQLAlchemy ``Session`` work to
the Protocol surface :mod:`app.domain.identity.permission_groups`
and :mod:`app.domain.identity.role_grants` consume:

* :class:`SqlAlchemyPermissionGroupRepository` — wraps the
  ``permission_group`` and ``permission_group_member`` tables.
* :class:`SqlAlchemyRoleGrantRepository` — wraps ``role_grant`` and
  the ``property_workspace`` junction (the cross-workspace property-
  scope check belongs on the role-grants seam since
  :mod:`app.domain.identity.role_grants` is the only domain caller
  for it). Cross-package imports between adapter modules are allowed
  by the import-linter — only ``app.domain → app.adapters`` is
  forbidden.

Every repo carries an open ``Session`` and never commits or flushes
beyond what the underlying statements require — the caller's UoW
owns the transaction boundary (§01 "Key runtime invariants" #3).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.places.models import PropertyWorkspace
from app.domain.identity.ports import (
    PermissionGroupMemberRow,
    PermissionGroupRepository,
    PermissionGroupRow,
    PermissionGroupSlugTakenError,
    RoleGrantRepository,
    RoleGrantRow,
)

__all__ = [
    "SqlAlchemyPermissionGroupRepository",
    "SqlAlchemyRoleGrantRepository",
]


def _to_group_row(row: PermissionGroup) -> PermissionGroupRow:
    """Project an ORM ``PermissionGroup`` into the seam-level row.

    Copying ``capabilities_json`` into a fresh ``dict`` severs the
    reference to SQLAlchemy's mutable JSON payload so a caller who
    mutates the returned mapping doesn't poison the identity map.
    """
    return PermissionGroupRow(
        id=row.id,
        slug=row.slug,
        name=row.name,
        system=row.system,
        capabilities=dict(row.capabilities_json),
        created_at=row.created_at,
    )


def _to_member_row(row: PermissionGroupMember) -> PermissionGroupMemberRow:
    """Project an ORM ``PermissionGroupMember`` into the seam-level row."""
    return PermissionGroupMemberRow(
        group_id=row.group_id,
        user_id=row.user_id,
        added_at=row.added_at,
        added_by_user_id=row.added_by_user_id,
    )


def _to_grant_row(row: RoleGrant) -> RoleGrantRow:
    """Project a workspace-scoped ORM ``RoleGrant`` into the seam-level row.

    cd-wchi widened :class:`RoleGrant.workspace_id` to nullable so the
    deployment-scope partition can omit it. Every code path on the
    workspace-scoped repo filters on
    ``RoleGrant.workspace_id == workspace_id`` before reaching this
    helper, so a deployment-scope row can never surface here. The
    assertion narrows the static type without papering over the new
    invariant.
    """
    assert row.workspace_id is not None, (
        "role_grant row reached the workspace-scoped repository with "
        f"workspace_id IS NULL (id={row.id!r}, scope_kind={row.scope_kind!r}); "
        "deployment-scope rows must use the admin surface helpers"
    )
    return RoleGrantRow(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        grant_role=row.grant_role,
        scope_property_id=row.scope_property_id,
        binding_org_id=row.binding_org_id,
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
    )


class SqlAlchemyPermissionGroupRepository(PermissionGroupRepository):
    """SA-backed concretion of :class:`PermissionGroupRepository`.

    Wraps an open :class:`~sqlalchemy.orm.Session` and never commits
    or flushes outside what the underlying statements require — the
    caller's UoW owns the transaction boundary (§01 "Key runtime
    invariants" #3).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # -- Group reads -----------------------------------------------------

    def list_groups(self, *, workspace_id: str) -> Sequence[PermissionGroupRow]:
        rows = self._session.scalars(
            select(PermissionGroup)
            .where(PermissionGroup.workspace_id == workspace_id)
            .order_by(PermissionGroup.created_at.asc(), PermissionGroup.id.asc())
        ).all()
        return [_to_group_row(row) for row in rows]

    def get_group(
        self, *, workspace_id: str, group_id: str
    ) -> PermissionGroupRow | None:
        row = self._session.scalars(
            select(PermissionGroup).where(
                PermissionGroup.id == group_id,
                PermissionGroup.workspace_id == workspace_id,
            )
        ).one_or_none()
        return _to_group_row(row) if row is not None else None

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
        group = PermissionGroup(
            id=group_id,
            workspace_id=workspace_id,
            slug=slug,
            name=name,
            system=system,
            # Snapshot the payload so a later mutation on the caller's
            # dict doesn't bleed into the persisted row.
            capabilities_json=dict(capabilities),
            created_at=created_at,
        )
        # Wrap the flush in a SAVEPOINT so an IntegrityError rolls back
        # only the failed INSERT — the caller's outer transaction (and
        # any prior successful writes inside it) stays intact. A bare
        # ``session.rollback()`` on IntegrityError would nuke the whole
        # transaction, including earlier successful inserts in the same
        # request.
        nested = self._session.begin_nested()
        self._session.add(group)
        try:
            self._session.flush()
        except IntegrityError as exc:
            # The only unique constraint on ``permission_group`` in v1
            # is ``(workspace_id, slug)`` — any other IntegrityError is
            # unexpected and should propagate as-is.
            nested.rollback()
            raise PermissionGroupSlugTakenError(slug) from exc
        nested.commit()
        return _to_group_row(group)

    def update_group(
        self,
        *,
        workspace_id: str,
        group_id: str,
        name: str | None = None,
        capabilities: dict[str, Any] | None = None,
    ) -> PermissionGroupRow:
        row = self._session.scalars(
            select(PermissionGroup).where(
                PermissionGroup.id == group_id,
                PermissionGroup.workspace_id == workspace_id,
            )
        ).one()
        if name is not None:
            row.name = name
        if capabilities is not None:
            row.capabilities_json = dict(capabilities)
        self._session.flush()
        return _to_group_row(row)

    def delete_group(self, *, workspace_id: str, group_id: str) -> None:
        row = self._session.scalars(
            select(PermissionGroup).where(
                PermissionGroup.id == group_id,
                PermissionGroup.workspace_id == workspace_id,
            )
        ).one()
        self._session.delete(row)
        self._session.flush()

    # -- Member reads ----------------------------------------------------

    def list_members(
        self, *, workspace_id: str, group_id: str
    ) -> Sequence[PermissionGroupMemberRow]:
        rows = self._session.scalars(
            select(PermissionGroupMember)
            .where(
                PermissionGroupMember.group_id == group_id,
                PermissionGroupMember.workspace_id == workspace_id,
            )
            .order_by(
                PermissionGroupMember.added_at.asc(),
                PermissionGroupMember.user_id.asc(),
            )
        ).all()
        return [_to_member_row(row) for row in rows]

    def get_member(
        self, *, group_id: str, user_id: str
    ) -> PermissionGroupMemberRow | None:
        row = self._session.get(PermissionGroupMember, (group_id, user_id))
        return _to_member_row(row) if row is not None else None

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
        member = PermissionGroupMember(
            group_id=group_id,
            user_id=user_id,
            workspace_id=workspace_id,
            added_at=added_at,
            added_by_user_id=added_by_user_id,
        )
        self._session.add(member)
        self._session.flush()
        return _to_member_row(member)

    def delete_member(self, *, group_id: str, user_id: str) -> None:
        row = self._session.get(PermissionGroupMember, (group_id, user_id))
        if row is None:
            # Idempotent: deleting a missing membership is a no-op.
            # The caller's audit row still records the intent.
            return
        self._session.delete(row)
        self._session.flush()


class SqlAlchemyRoleGrantRepository(RoleGrantRepository):
    """SA-backed concretion of :class:`RoleGrantRepository`.

    Reaches into both :mod:`app.adapters.db.authz.models` (for the
    ``role_grant`` rows) and :mod:`app.adapters.db.places.models`
    (for the ``property_workspace`` junction the cross-workspace
    property-scope check needs). Adapter-to-adapter imports are
    allowed by the import-linter — only ``app.domain → app.adapters``
    is forbidden.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # -- Reads -----------------------------------------------------------

    def list_grants(
        self,
        *,
        workspace_id: str,
        user_id: str | None = None,
        scope_property_id: str | None = None,
    ) -> Sequence[RoleGrantRow]:
        stmt = select(RoleGrant).where(RoleGrant.workspace_id == workspace_id)
        if user_id is not None:
            stmt = stmt.where(RoleGrant.user_id == user_id)
        if scope_property_id is not None:
            stmt = stmt.where(RoleGrant.scope_property_id == scope_property_id)
        stmt = stmt.order_by(RoleGrant.created_at.asc(), RoleGrant.id.asc())
        rows = self._session.scalars(stmt).all()
        return [_to_grant_row(row) for row in rows]

    def get_grant(self, *, workspace_id: str, grant_id: str) -> RoleGrantRow | None:
        row = self._session.scalars(
            select(RoleGrant).where(
                RoleGrant.id == grant_id,
                RoleGrant.workspace_id == workspace_id,
            )
        ).one_or_none()
        return _to_grant_row(row) if row is not None else None

    def has_active_manager_grant(self, *, workspace_id: str, user_id: str) -> bool:
        stmt = (
            select(RoleGrant)
            .where(
                RoleGrant.workspace_id == workspace_id,
                RoleGrant.user_id == user_id,
                RoleGrant.grant_role == "manager",
            )
            .limit(1)
        )
        return self._session.scalars(stmt).first() is not None

    def is_property_in_workspace(self, *, workspace_id: str, property_id: str) -> bool:
        stmt = select(
            exists().where(
                PropertyWorkspace.property_id == property_id,
                PropertyWorkspace.workspace_id == workspace_id,
            )
        )
        return bool(self._session.scalar(stmt))

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
        row = RoleGrant(
            id=grant_id,
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_property_id=scope_property_id,
            binding_org_id=None,
            created_at=created_at,
            created_by_user_id=created_by_user_id,
        )
        self._session.add(row)
        self._session.flush()
        return _to_grant_row(row)

    def delete_grant(self, *, workspace_id: str, grant_id: str) -> None:
        row = self._session.scalars(
            select(RoleGrant).where(
                RoleGrant.id == grant_id,
                RoleGrant.workspace_id == workspace_id,
            )
        ).one()
        self._session.delete(row)
        self._session.flush()
