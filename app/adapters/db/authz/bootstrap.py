"""Workspace-creation hook: seed the ``owners`` system group.

Called by:

* production signup (cd-3i5) the moment it creates a workspace and
  assigns the operator seat;
* the ``bootstrap_workspace`` test helper under
  ``tests/factories/identity.py`` so integration tests start from a
  workspace that already carries the governance anchor.

Keeping the helper in ``app/adapters/db/authz`` — not ``tests/`` —
means both callers share exactly the same rows; a production
workspace and an integration-test workspace diverge only in the
caller's choice of clock and IDs.

The spec invariants (§02 "permission_group" §"Invariants") forbid a
workspace from ever having its ``owners`` group empty or missing, so
this seed runs synchronously inside the same transaction that
creates the workspace. Callers are responsible for the transaction
boundary; we only ``session.flush()`` so subsequent reads inside the
txn see the rows.

See ``docs/specs/02-domain-model.md`` §"permission_group",
§"permission_group_member", §"role_grants" and
``docs/specs/05-employees-and-roles.md`` §"Roles & groups".
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = ["seed_owners_system_group"]


def seed_owners_system_group(
    session: Session,
    *,
    workspace_id: str,
    owner_user_id: str,
    clock: Clock | None = None,
) -> tuple[PermissionGroup, PermissionGroupMember, RoleGrant]:
    """Seed the ``owners`` group + its sole member + the owner's role grant.

    Writes three rows in the caller's session — caller owns the
    outer transaction and commit cadence.

    * :class:`PermissionGroup` with ``slug='owners'``, ``system=True``
      and a ``{"all": True}`` capability payload. v1 owners hold
      every capability; the capability-matrix reshuffle (cd-79r /
      cd-zkr) will replace this blanket flag with a finer payload.
    * :class:`PermissionGroupMember` placing ``owner_user_id`` in the
      new group. ``added_by_user_id`` is ``None`` because there is
      no prior actor — this is the self-bootstrap row.
    * :class:`RoleGrant` giving ``owner_user_id`` the ``manager``
      surface on the workspace. The role is ``manager`` (not a
      renamed ``owner``) per §02's v1 enum; owner-level authority
      comes from the permission-group membership, not the role
      grant.

    Returns the three newly-seeded rows so the caller can attach
    audit IDs or continue the transaction.

    Re-running the helper for the same ``workspace_id`` raises
    :class:`~sqlalchemy.exc.IntegrityError` on the
    ``uq_permission_group_workspace_slug`` constraint — owners is a
    singleton per workspace.
    """
    now = (clock if clock is not None else SystemClock()).now()

    group = PermissionGroup(
        id=new_ulid(),
        workspace_id=workspace_id,
        slug="owners",
        name="Owners",
        system=True,
        capabilities_json={"all": True},
        created_at=now,
    )
    session.add(group)
    # Flush before adding the member row so ``group.id`` is settled
    # on the DB side; we already hold it client-side via ``new_ulid``,
    # but the flush also surfaces the unique-slug conflict early if
    # the caller double-seeds.
    session.flush()

    member = PermissionGroupMember(
        group_id=group.id,
        user_id=owner_user_id,
        workspace_id=workspace_id,
        added_at=now,
        added_by_user_id=None,
    )
    grant = RoleGrant(
        id=new_ulid(),
        workspace_id=workspace_id,
        user_id=owner_user_id,
        grant_role="manager",
        scope_property_id=None,
        created_at=now,
        created_by_user_id=None,
    )
    session.add_all([member, grant])
    session.flush()
    return group, member, grant
