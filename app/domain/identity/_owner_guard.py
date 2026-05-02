"""Cross-dialect locking primitive for the last-owner invariants.

§02 "permission_group" §"Invariants" requires the system ``owners``
group on every workspace to honour two related write-side invariants:

1. **Owners roster.** ``owners@<ws>`` has at least one active member
   at all times. Removing the sole owners-group member fails with
   422 ``would_orphan_owners_group``.
2. **Administrative reach.** ``owners@<ws>`` has at least one active
   member who **also carries a live ``manager`` role grant on the
   workspace**. Either write surface that would tip the count of
   manager-grant-holding owners to zero refuses with 409
   ``last_owner_grant_protected``: ``role_grants.revoke`` for the
   grant-side path (cd-nj8m) and ``permission_groups.remove_member``
   for the membership-side cross-path race (cd-j5pu).

Both invariants share a count-then-act pattern. Without a lock, the
pattern is a textbook TOCTOU: two concurrent transactions both read
``count == 2`` and both commit their write, leaving the workspace
either un-rostered (cd-mb5n) or administratively decapitated
(cd-nj8m, plus the cross-path race fixed by cd-j5pu).

This module exposes two helpers — :func:`count_owner_members_locked`
(for the roster invariant) and
:func:`count_owner_members_with_manager_grant_locked` (for the
administrative-reach invariant; called from both
``role_grants.revoke`` and ``permission_groups.remove_member`` so
the cross-path race in cd-j5pu cannot wedge the workspace) — that
share a common locking primitive :func:`_lock_owners_group`. Both:

1. Lock the system ``owners`` ``permission_group`` row for the
   caller's workspace, using the dialect's native write-lock
   primitive:

   * **PostgreSQL**: ``SELECT ... FOR UPDATE`` on the owners-group
     row. The row-level lock survives until the caller commits or
     rolls back, so any concurrent transaction that reaches either
     helper blocks on step 1 until the first one settles.
   * **SQLite**: a no-op ``UPDATE permission_group SET slug = slug
     WHERE id = :owners_group_id``. SQLite promotes the connection
     from SHARED to RESERVED on the first write, and Python's
     ``sqlite3`` driver waits up to the default 5 s ``busy_timeout``
     on contention — the second writer blocks until the first
     commits, then re-reads the (now post-write) state.

2. Return a count under the lock. Callers raise their own
   domain-specific exception when the count would drop to zero
   after the pending write.

Sharing the lock primitive across both helpers means the two guards
serialise against each other — a concurrent ``remove_member`` and
``revoke`` on the same workspace can't slip past each other to a
state that violates either invariant. The helpers **never commit**:
the caller owns the transaction boundary, and releasing the lock
mid-transaction would re-open the TOCTOU window.

**Why lock the ``permission_group`` row, not the member rows?** The
counts we protect are functions of the owners-group identity;
serialising on the parent row gives every concurrent guard the same
single rendez-vous point regardless of which member or grant each
thread is trying to remove. Locking individual member rows or grant
rows would leave the count itself unprotected (thread A locks member
X, thread B locks member Y, both count 2 and proceed).

See:

* ``docs/specs/02-domain-model.md`` §"permission_group"
  §"Invariants".
* cd-mb5n — the original TOCTOU fix (membership-removal scenario).
* cd-nj8m — the manager-grant-revoke scenario.
* cd-j5pu — the cross-path race: thread 1 revokes A's manager grant
  while thread 2 drops B from owners, both threads observe a
  too-narrow invariant and the workspace ends up decapitated.
  ``permission_groups.remove_member`` now consults the same admin-
  reach helper (with ``exclude_user_id``) so the lock serialises
  the two paths against each other.
* cd-ckr — the v1 last-owner guard on ``remove_member``.
* cd-79r — the v1 last-owner guard on ``revoke``.
"""

from __future__ import annotations

from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)

__all__ = [
    "count_owner_members_locked",
    "count_owner_members_with_manager_grant_locked",
]


def _lock_owners_group(session: Session, *, workspace_id: str) -> str | None:
    """Acquire the cross-dialect write lock on ``owners@workspace_id``.

    Returns the locked group's id, or ``None`` when the owners group
    does not exist for the workspace (a pathological state — every
    workspace seeds one at bootstrap). Callers treat ``None`` as
    "count zero" so their last-owner guards trip cleanly on the
    corrupted state instead of masking it behind a different error
    shape.

    The lock is scoped to the caller's open transaction; releasing it
    is the caller's job (commit / rollback). Calling this helper twice
    inside one UoW is safe — the second acquisition is a no-op on both
    dialects (Postgres re-issues ``FOR UPDATE`` on a row already locked
    by the same transaction; SQLite is already RESERVED).
    """
    dialect = session.get_bind().dialect.name

    # Locate the owners-group row. We need the id for both branches —
    # Postgres to attach ``FOR UPDATE``, SQLite to issue the
    # lock-acquiring UPDATE.
    owners_stmt = select(PermissionGroup.id).where(
        PermissionGroup.workspace_id == workspace_id,
        PermissionGroup.slug == "owners",
        PermissionGroup.system.is_(True),
    )

    if dialect == "postgresql":
        # Row-level ``FOR UPDATE`` on the owners-group row. Any
        # concurrent transaction that reaches this line blocks until
        # the current one commits or rolls back.
        owners_group_id = session.scalar(owners_stmt.with_for_update())
    else:
        # SQLite (and any non-Postgres dialect): acquire the write
        # lock via a no-op UPDATE. On SQLite this promotes the
        # connection to RESERVED, serialising writers across the whole
        # database; the driver's default ``busy_timeout`` (5 s) gives
        # the loser of an upgrade race time to wait. A conditional
        # ``WHERE`` keeps the write scoped so Postgres (were it to
        # ever fall into this branch) wouldn't burn a row.
        owners_group_id = session.scalar(owners_stmt)
        if owners_group_id is not None:
            session.execute(
                update(PermissionGroup)
                .where(PermissionGroup.id == owners_group_id)
                # No-op self-assignment: ``slug`` is the uniqueness
                # anchor for the group within a workspace, so writing
                # it back to its current value changes nothing but
                # still counts as a row-level write to the lock
                # manager.
                .values(slug=PermissionGroup.slug)
            )

    return owners_group_id


def count_owner_members_locked(
    session: Session,
    *,
    workspace_id: str,
) -> int:
    """Lock the ``owners@workspace_id`` group row and return its member count.

    The returned count reflects the state of
    ``permission_group_member`` at the instant the lock was acquired;
    a concurrent transaction cannot mutate the count until the caller
    commits or rolls back.

    If the owners group does not exist for the given workspace the
    helper returns ``0`` without raising — every workspace bootstraps
    one in :mod:`app.adapters.db.authz.bootstrap` so the absence
    condition is pathological, and the caller's last-owner guard
    will trip on the zero count anyway. Raising here would hide that
    corruption behind a different error shape.

    **Tenant filter.** The helper is called from inside a live
    :class:`~app.tenancy.WorkspaceContext`; both SELECTs run with
    the ORM tenant filter active, so the ``workspace_id`` predicate
    is belt-and-braces but kept explicit to match the rest of the
    identity module's style (a misconfigured filter should fail
    loud, not leak a sibling workspace's count).
    """
    owners_group_id = _lock_owners_group(session, workspace_id=workspace_id)
    if owners_group_id is None:
        return 0

    # Count members under the lock. On Postgres this runs under the
    # ``FOR UPDATE`` row lock; on SQLite it runs after the UPDATE
    # promoted us to RESERVED. Either way, no other transaction can
    # change this count until ours commits.
    count_stmt = (
        select(func.count())
        .select_from(PermissionGroupMember)
        .where(
            PermissionGroupMember.group_id == owners_group_id,
            PermissionGroupMember.workspace_id == workspace_id,
        )
    )
    count = session.scalar(count_stmt)
    # ``select(func.count())`` always returns a scalar (zero when no
    # rows match); ``or 0`` keeps mypy honest against the ``scalar()``
    # Optional return type. An unexpected ``None`` would be treated as
    # "no members", the safe-fails-closed default for both guards.
    return count or 0


def count_owner_members_with_manager_grant_locked(
    session: Session,
    *,
    workspace_id: str,
    exclude_grant_id: str | None = None,
    exclude_user_id: str | None = None,
) -> int:
    """Count owners-group members who would still hold a manager grant.

    Returns the number of distinct users who are BOTH (a) members of
    ``owners@workspace_id`` AND (b) carry at least one ``manager``
    :class:`~app.adapters.db.authz.models.RoleGrant` on the workspace,
    after applying any of the two pending-write exclusions:

    * ``exclude_grant_id`` — a ``role_grant`` row the caller is about
      to delete (used by :func:`role_grants.revoke`). Filters the
      manager-grant EXISTS sub-query so the row in flight does not
      contribute to the post-write tally.
    * ``exclude_user_id`` — a user the caller is about to drop from
      ``owners@workspace_id`` (used by
      :func:`permission_groups.remove_member`). Filters the
      owners-membership predicate so the user in flight does not
      contribute to the post-write tally even though their
      ``permission_group_member`` row is still committed at lock
      acquisition time.

    Both exclusions are independent: a caller may set either, both,
    or neither. With neither set the count is the unconditional
    "manager-holding owners" tally.

    The helper takes the same lock as
    :func:`count_owner_members_locked` so both last-owner guards
    serialise against each other — a concurrent ``remove_member`` and
    ``revoke`` on the same workspace can't slip past each other to a
    state that violates either §02 invariant (cd-mb5n + cd-nj8m +
    cd-j5pu).

    If the owners group does not exist for the given workspace the
    helper returns ``0``; the caller's guard then trips on that
    zero count, matching :func:`count_owner_members_locked`'s
    fail-closed default.

    **Tenant filter.** The helper is called from inside a live
    :class:`~app.tenancy.WorkspaceContext`; the explicit
    ``workspace_id`` predicate on every SELECT is defence-in-depth
    against a misconfigured filter (we want loud failure, not a
    silent sibling-workspace count).
    """
    owners_group_id = _lock_owners_group(session, workspace_id=workspace_id)
    if owners_group_id is None:
        return 0

    # Count distinct users who are owners-group members AND have at
    # least one ``manager`` grant on the workspace, optionally minus
    # the grant about to be revoked AND/OR the user about to be
    # removed from the owners roster. ``DISTINCT user_id`` collapses
    # users with multiple manager grants (e.g. one workspace-wide and
    # one property-scoped) to a single contributing user, matching the
    # invariant's "≥ 1 manager-holding owner" shape.
    grant_predicates = [
        RoleGrant.workspace_id == workspace_id,
        RoleGrant.user_id == PermissionGroupMember.user_id,
        RoleGrant.grant_role == "manager",
    ]
    if exclude_grant_id is not None:
        grant_predicates.append(RoleGrant.id != exclude_grant_id)

    member_predicates = [
        PermissionGroupMember.group_id == owners_group_id,
        PermissionGroupMember.workspace_id == workspace_id,
    ]
    if exclude_user_id is not None:
        member_predicates.append(PermissionGroupMember.user_id != exclude_user_id)

    count_stmt = (
        select(func.count(func.distinct(PermissionGroupMember.user_id)))
        .select_from(PermissionGroupMember)
        .where(*member_predicates)
        .where(select(RoleGrant.id).where(and_(*grant_predicates)).exists())
    )
    count = session.scalar(count_stmt)
    return count or 0
