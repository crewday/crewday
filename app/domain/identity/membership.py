"""Membership lifecycle — remove, list workspaces, switch workspace.

The invite half of this module (create / introspect / consume / complete /
confirm + the new-user passkey enrolment branch) has moved to its own
focused module at :mod:`app.domain.identity.invite` (cd-vc3r). This file
now owns only the post-acceptance membership operations:

* :func:`remove_member` — delete every ``role_grant`` +
  ``permission_group_member`` row the user holds in the caller's
  workspace, plus every live :class:`Session` scoped to that
  workspace. Refuses the operation if it would empty the ``owners``
  group (reuses
  :class:`app.domain.identity.permission_groups.WouldOrphanOwnersGroup`).
  The derived :class:`UserWorkspace` row drops on the next worker
  tick (see "derived junction" below).
* :func:`list_workspaces_for_user` — what the workspace switcher
  reads.
* :func:`switch_session_workspace` — verify membership + update
  ``Session.workspace_id`` atomically.

**Public surface stability.** The invite-side symbols stay reachable
under :mod:`app.domain.identity.membership` via re-export from the
new :mod:`app.domain.identity.invite` module — callers using
``membership.invite``, ``membership.consume_invite_token``,
``membership.InviteBodyInvalid``, etc., keep working without
churn. The re-exports also include the private helpers
(``_validate_grants`` / ``_aware_utc`` / ``_validate_work_engagement``
/ ``_validate_user_work_roles``) the unit tests reach into for
shape-only checks.

**Atomicity.** Every write path never calls ``session.commit()``;
the caller's UoW owns the transaction boundary. Failures roll back
every downstream insert.

**derived junction.** ``user_workspace`` is documented as a derived
junction (§02). The canonical reconciler lives in
:func:`app.domain.identity.user_workspace_refresh.reconcile_user_workspace`
and runs on the worker (cd-yqm4) every
:data:`~app.worker.scheduler.USER_WORKSPACE_REFRESH_INTERVAL_SECONDS`
seconds; the membership service writes the upstream rows
(``role_grant`` / ``permission_group_member``) and then drives the
*scoped* reconciler
(:func:`app.domain.identity.user_workspace_refresh.reconcile_user_workspace_for`)
in the same transaction so the post-remove redirect sees the
up-to-date junction without waiting on the worker tick.

**Audit.** Every mutation emits one :mod:`app.audit` row in the
same transaction as the write; audit diffs carry hashed email only
(never the plaintext, §15).

**Architecture note.** Like :mod:`app.domain.identity.invite` and the
sibling identity services, this module imports ORM models from
:mod:`app.adapters.db.*`. The import-linter stopgap for
``app.domain.identity.*`` is in place (see :mod:`pyproject.toml`
§"ignore_imports"); cd-duv6 tracks the proper Protocol-seam refactor.

See ``docs/specs/03-auth-and-tokens.md`` §"Additional users
(invite → click-to-accept)" and ``docs/specs/05-employees-and-roles.md``
§"Role grants".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import (
    Session as SessionRow,
)
from app.adapters.db.workspace.models import (
    UserWorkspace,
    Workspace,
)
from app.audit import write_audit

# Private helpers re-exported for ``tests/unit/identity/test_membership.py``,
# which reaches into the shape-validation helpers via attribute access on
# this module (``membership._validate_grants(...)`` etc.). The split moved
# the helpers to :mod:`app.domain.identity.invite`; the re-export keeps
# the test surface stable without forcing test-only import churn.
from app.domain.identity.invite import (  # noqa: F401  (re-exports; see comment above)
    AcceptanceCard,
    AlreadyConsumed,
    ExistingUserAcceptance,
    InvalidToken,
    InviteAlreadyAccepted,
    InviteBodyInvalid,
    InviteExpired,
    InviteIntrospection,
    InviteNotFound,
    InviteOutcome,
    InvitePasskeyAlreadyRegistered,
    InvitePasskeyFinishOutcome,
    InviteSession,
    InviteStateInvalid,
    NewUserAcceptance,
    PasskeySessionRequired,
    PruneStaleInvitesReport,
    PurposeMismatch,
    TokenExpired,
    _aware_utc,
    _now,
    _validate_grants,
    _validate_group_memberships,
    _validate_user_work_roles,
    _validate_work_engagement,
    complete_invite,
    confirm_invite,
    consume_invite_token,
    introspect_invite,
    invite,
    prune_stale_invites,
    register_invite_passkey_finish,
    register_invite_passkey_start,
)
from app.domain.identity.permission_groups import (
    WouldOrphanOwnersGroup,
    write_member_remove_rejected_audit,
)
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock
from app.util.ulid import new_ulid

__all__ = [
    "AcceptanceCard",
    "AlreadyConsumed",
    "ExistingUserAcceptance",
    "InvalidToken",
    "InviteAlreadyAccepted",
    "InviteBodyInvalid",
    "InviteExpired",
    "InviteIntrospection",
    "InviteNotFound",
    "InviteOutcome",
    "InvitePasskeyAlreadyRegistered",
    "InvitePasskeyFinishOutcome",
    "InviteSession",
    "InviteStateInvalid",
    "NewUserAcceptance",
    "NotAMember",
    "PasskeySessionRequired",
    "PruneStaleInvitesReport",
    "PurposeMismatch",
    "TokenExpired",
    "WorkspaceMembership",
    "WouldOrphanOwnersGroup",
    "complete_invite",
    "confirm_invite",
    "consume_invite_token",
    "introspect_invite",
    "invite",
    "list_workspaces_for_user",
    "prune_stale_invites",
    "register_invite_passkey_finish",
    "register_invite_passkey_start",
    "remove_member",
    "switch_session_workspace",
    "write_member_remove_rejected_audit",
]


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkspaceMembership:
    """One entry in :func:`list_workspaces_for_user`'s return."""

    workspace_id: str
    workspace_slug: str
    workspace_name: str


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NotAMember(LookupError):
    """User has no active grant in the target workspace.

    404-equivalent. Raised by :func:`switch_session_workspace` and
    :func:`remove_member` for users the caller is trying to act on
    in a workspace they don't belong to.
    """


# ---------------------------------------------------------------------------
# remove_member
# ---------------------------------------------------------------------------


def remove_member(
    session: DbSession,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    clock: Clock | None = None,
) -> None:
    """Strip every grant + permission_group membership + session for ``user_id``.

    Spec §03 / §05: the workspace admin clicks "remove from workspace"
    on a user's profile. Owners can remove anyone except the last
    owner; the last-owner guard reuses
    :class:`app.domain.identity.permission_groups.WouldOrphanOwnersGroup`
    so the invariant definition lives in one place (§02
    "permission_group" §"Invariants").

    Writes (in one transaction):

    1. Delete every :class:`RoleGrant` for ``(workspace, user)``.
    2. Delete every :class:`PermissionGroupMember` for
       ``(workspace, user)``. If the user is the sole owner, the
       guard refuses BEFORE the DELETE; the caller's UoW keeps the
       rows intact.
    3. Delete every :class:`Session` row whose ``workspace_id``
       matches the caller's workspace.

    The derived :class:`UserWorkspace` row is dropped inline via
    :func:`reconcile_user_workspace_for` so a removed user does not
    keep a stale ``guest``-fallback :class:`WorkspaceContext` for up
    to one worker tick; the cd-yqm4 worker still owns steady-state
    reconciliation but the security-critical drop on the removal
    path runs synchronously.

    Audit: one ``user.removed`` row with the list of deleted grant
    ids + group memberships + session count (PII-hash only). On the
    last-owner refusal, the router writes a fresh-UoW audit row via
    :func:`write_member_remove_rejected_audit` (already exported
    from :mod:`app.domain.identity.permission_groups`) and the
    primary UoW rolls back.
    """
    resolved_now = _now(clock)

    # Resolve the owners group to run the last-owner guard. The
    # guard mirrors :mod:`app.domain.identity.permission_groups` so
    # both the remove_member entry point and direct group mutations
    # reject the same shape.
    owners_group = session.scalar(
        select(PermissionGroup).where(
            PermissionGroup.workspace_id == ctx.workspace_id,
            PermissionGroup.slug == "owners",
            PermissionGroup.system.is_(True),
        )
    )
    if owners_group is None:
        # Every workspace has an owners group; a missing row means
        # somebody bypassed bootstrap. Fail loud so the operator
        # can investigate rather than silently leave a workspace
        # ungoverned.
        raise InviteStateInvalid(f"workspace {ctx.workspace_id!r} has no owners group")

    membership = session.get(PermissionGroupMember, (owners_group.id, user_id))
    if membership is not None:
        # Last-owner guard: mirror the shape in
        # :mod:`app.domain.identity.permission_groups` so both entry
        # points enforce the same invariant (§02 "permission_group"
        # §"Invariants"). ``func.count()`` avoids loading every row
        # and stays honest about the membership head count without
        # a subsequent materialisation.
        from sqlalchemy import func as sa_func

        total_owner_members = (
            session.scalar(
                select(sa_func.count())
                .select_from(PermissionGroupMember)
                .where(PermissionGroupMember.group_id == owners_group.id)
            )
            or 0
        )
        if total_owner_members <= 1:
            raise WouldOrphanOwnersGroup(
                f"cannot remove the last member of the 'owners' group; "
                f"workspace_id={ctx.workspace_id!r} user_id={user_id!r}"
            )

    # Gather forensic fields before the DELETE — the rows disappear
    # in the next statement and the audit row needs their ids.
    grant_rows = list(
        session.scalars(
            select(RoleGrant).where(
                RoleGrant.workspace_id == ctx.workspace_id,
                RoleGrant.user_id == user_id,
            )
        ).all()
    )
    group_member_rows = list(
        session.scalars(
            select(PermissionGroupMember).where(
                PermissionGroupMember.workspace_id == ctx.workspace_id,
                PermissionGroupMember.user_id == user_id,
            )
        ).all()
    )
    if not grant_rows and not group_member_rows:
        # No live membership — the caller targeted a user who was
        # never part of this workspace (or was already removed).
        # Audit the refusal for forensics but don't raise; the HTTP
        # router maps an empty delete as 404 / 204 per its own
        # vocabulary. We raise :class:`NotAMember` so the caller has
        # the choice.
        raise NotAMember(
            f"user {user_id!r} has no grants in workspace {ctx.workspace_id!r}"
        )

    deleted_grant_ids = [row.id for row in grant_rows]
    deleted_group_ids = [row.group_id for row in group_member_rows]

    session.execute(
        delete(RoleGrant)
        .where(
            RoleGrant.workspace_id == ctx.workspace_id,
            RoleGrant.user_id == user_id,
        )
        .execution_options(synchronize_session="fetch")
    )
    session.execute(
        delete(PermissionGroupMember)
        .where(
            PermissionGroupMember.workspace_id == ctx.workspace_id,
            PermissionGroupMember.user_id == user_id,
        )
        .execution_options(synchronize_session="fetch")
    )

    # Revoke every session scoped to this workspace. A session with
    # ``workspace_id IS NULL`` (user is signed in but hasn't picked
    # a workspace) stays — it's identity-level, not membership-level.
    # justification: ``session`` is user-scoped, filter by workspace_id explicitly.
    with tenant_agnostic():
        # Pre-count so the audit row carries the number accurately
        # (DML ``rowcount`` depends on the driver: -1 on SQLite when
        # ``synchronize_session="fetch"`` flattens the returning-rows
        # path, and the generic :class:`Result` stub in SQLAlchemy's
        # typing doesn't surface it anyway).
        from sqlalchemy import func as sa_func

        sessions_revoked = (
            session.scalar(
                select(sa_func.count())
                .select_from(SessionRow)
                .where(
                    SessionRow.user_id == user_id,
                    SessionRow.workspace_id == ctx.workspace_id,
                )
            )
            or 0
        )
        session.execute(
            delete(SessionRow)
            .where(
                SessionRow.user_id == user_id,
                SessionRow.workspace_id == ctx.workspace_id,
            )
            .execution_options(synchronize_session="fetch")
        )

    session.flush()

    # Drop the derived ``user_workspace`` row synchronously — without
    # this the removed user keeps a stale membership for up to one
    # cd-yqm4 worker tick (5 min by default), and the tenancy resolver
    # would happily build a ``guest``-fallback :class:`WorkspaceContext`
    # against the now-empty ``role_grant`` set. Deferred import —
    # see the matching note in :func:`_activate_invite`.
    from app.domain.identity.user_workspace_refresh import (
        reconcile_user_workspace_for,
    )

    reconcile_user_workspace_for(
        session,
        user_id=user_id,
        workspace_id=ctx.workspace_id,
        now=resolved_now,
    )

    write_audit(
        session,
        ctx,
        entity_kind="user",
        entity_id=user_id,
        action="user.removed",
        diff={
            # PII minimisation (§15): forensic joins travel via
            # ``user_id``, which is identity-anchored and non-PII.
            # The email lives on the :class:`User` row and never
            # rides the audit diff for remove; audit readers that
            # want the hash can join to the invite trail.
            "user_id": user_id,
            "deleted_grant_ids": deleted_grant_ids,
            "deleted_group_memberships": deleted_group_ids,
            "sessions_revoked": sessions_revoked,
        },
        clock=clock,
    )


# ---------------------------------------------------------------------------
# list_workspaces_for_user + switch_session_workspace
# ---------------------------------------------------------------------------


def list_workspaces_for_user(
    session: DbSession,
    *,
    user_id: str,
) -> Sequence[WorkspaceMembership]:
    """Return every workspace ``user_id`` is a member of.

    Drives the workspace switcher UI (§14) and the
    ``GET /api/v1/me/workspaces`` route. Reads the derived
    :class:`UserWorkspace` junction directly — the user_workspace
    derive-refresh worker (cd-yqm4) keeps it in sync.

    No tenant filter: the user spans multiple workspaces and this
    call deliberately aggregates across them. We run it under
    :func:`tenant_agnostic` so the ORM filter doesn't narrow the
    result to the caller's current workspace.
    """
    with tenant_agnostic():
        rows = session.execute(
            select(UserWorkspace, Workspace)
            .join(Workspace, Workspace.id == UserWorkspace.workspace_id)
            .where(UserWorkspace.user_id == user_id)
            .order_by(Workspace.slug.asc())
        ).all()
    return [
        WorkspaceMembership(
            workspace_id=ws.id,
            workspace_slug=ws.slug,
            workspace_name=ws.name,
        )
        for _, ws in rows
    ]


def switch_session_workspace(
    session: DbSession,
    *,
    session_id: str,
    user_id: str,
    workspace_id: str,
    clock: Clock | None = None,
) -> None:
    """Update ``Session.workspace_id`` after verifying membership.

    Spec §03 "Sessions": a single passkey session hops between
    workspaces. The row's ``user_id`` stays pinned; only
    ``workspace_id`` moves, gated by an explicit membership check
    (:class:`UserWorkspace` row exists for the pair).

    Raises:

    * :class:`NotAMember` — the user has no :class:`UserWorkspace`
      row for ``workspace_id``.
    * :class:`InviteNotFound` — no :class:`Session` row for
      ``session_id`` / ``user_id`` combination. (Reused symbol
      avoids a bespoke ``SessionNotFound`` when the router already
      distinguishes 401 vs 404 on this family.)
    """
    resolved_now = _now(clock)
    # Verify the user is actually a member of the target workspace.
    with tenant_agnostic():
        member = session.get(UserWorkspace, (user_id, workspace_id))
    if member is None:
        raise NotAMember(
            f"user {user_id!r} is not a member of workspace {workspace_id!r}"
        )

    # justification: ``session`` is user-scoped; no tenant predicate applies.
    with tenant_agnostic():
        row = session.get(SessionRow, session_id)
    if row is None or row.user_id != user_id:
        raise InviteNotFound(session_id)

    old_workspace_id = row.workspace_id
    row.workspace_id = workspace_id
    row.last_seen_at = resolved_now
    session.flush()

    # Synthesise a ctx attributing the audit row to the actor + the
    # new workspace — the event belongs to the workspace the session
    # moved to so dashboard queries ("what did I do in workspace
    # X?") surface the hop.
    ctx = WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="",
        actor_id=user_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(clock=clock),
    )
    write_audit(
        session,
        ctx,
        entity_kind="session",
        entity_id=session_id,
        action="session.workspace_switched",
        diff={
            "user_id": user_id,
            "old_workspace_id": old_workspace_id,
            "new_workspace_id": workspace_id,
        },
        clock=clock,
    )
