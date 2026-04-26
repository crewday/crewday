"""``property_workspace`` junction service — multi-belonging operations.

The :class:`~app.adapters.db.places.models.PropertyWorkspace` row binds
a property to a workspace (§02 "property_workspace", §04
"Multi-belonging"). The :mod:`app.domain.places.property_service`
module seeds the bootstrap ``owner_workspace`` row at property-create
time; this module owns every subsequent mutation:

* :func:`invite_workspace` — owner workspace mints a ``managed`` /
  ``observer`` row in ``status='invited'``.
* :func:`accept_invite` — accepting workspace flips the ``invited``
  row to ``status='active'``.
* :func:`revoke_workspace` — owner workspace removes a non-owner row
  (live or invited).
* :func:`update_membership_role` — owner workspace flips between
  ``managed`` and ``observer`` (cannot mint ``owner_workspace`` here —
  use :func:`transfer_ownership`).
* :func:`transfer_ownership` — owner workspace hands the
  ``owner_workspace`` slot to a sibling workspace in one transaction;
  the outgoing party is demoted to ``observer`` or revoked.
* :func:`list_memberships` — read every row for one property scoped to
  the caller's reach.

**Authorization model.** Every mutation routes through
:func:`_assert_owner_workspace_owners_member` — the actor must be a
member of the ``owners`` permission group on the property's *owner*
workspace, regardless of the workspace the actor's
:class:`~app.tenancy.WorkspaceContext` is currently pinned to. The
``owners``-only gate matches §22 "Actions"
(``property_workspace_invite.create`` / ``.revoke`` require ``owners``
membership on ``from_workspace_id``) and §02's "owners group is the
governance anchor on every workspace" — operational managers /
workers do **not** carry authority to invite or revoke peer
workspaces. Cross-workspace reads pass through
:func:`~app.tenancy.tenant_agnostic` because the service inherently
spans workspaces — the per-row tenant predicate cannot apply to a
multi-workspace lookup.

The `accept_invite` path is symmetric: the actor must be a member of
the ``owners`` group on the *accepting* workspace (the recipient side
of the invite, which is :attr:`accepting_workspace_id`). This matches
§22 "``property_workspace_invite.accept`` requires ``owners``
membership on the accepting workspace". :func:`list_memberships` is
the only read surface; it narrows to "actor must hold any active
worker-or-higher role grant on at least one of the property's linked
workspaces" — anything else collapses to :class:`MembershipNotFound`
(404), matching §01 "tenant surface is not enumerable". Read access
is intentionally broader than write access: a worker on a managed
workspace can *see* the sharing tab, but only ``owners`` of the owner
workspace can mutate it.

**Invariants.**

* Exactly one ``owner_workspace`` row per property at all times. Both
  :func:`revoke_workspace` and :func:`update_membership_role` refuse
  to touch the owner row; :func:`transfer_ownership` is the only path
  that re-points it, and it does so in one transaction so the
  invariant is always observable.
* :func:`update_membership_role` cannot mint ``owner_workspace``;
  promotion to owner is :func:`transfer_ownership`'s exclusive
  surface.
* :func:`accept_invite` is idempotent on a row that is already
  ``active`` — a second accept of the same invite is a no-op rather
  than an error (the caller may legitimately replay on a flaky
  network).

**Audit.** Every mutation writes one
:func:`~app.audit.write_audit` row in the same transaction, with a
``before`` / ``after`` JSON diff so operators can reconstruct the
change. The entity kind is ``property_workspace``; actions:

* ``invited`` — :func:`invite_workspace`
* ``accepted`` — :func:`accept_invite`
* ``revoked`` — :func:`revoke_workspace`
* ``role_changed`` — :func:`update_membership_role`
* ``share_changed`` — :func:`update_share_guest_identity`
* ``ownership_transferred`` — :func:`transfer_ownership`

**Transaction boundary.** The service never calls ``session.commit()``;
the caller's Unit-of-Work owns transaction boundaries (§01 "Key
runtime invariants" #3). Every mutation flushes so the audit row's
FK reference to ``entity_id`` sees the new state.

See ``docs/specs/04-properties-and-stays.md`` §"Multi-belonging
(sharing across workspaces)", ``docs/specs/02-domain-model.md``
§"property_workspace", ``docs/specs/15-security-privacy.md``
§"Cross-workspace visibility".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.places.models import PropertyWorkspace
from app.audit import write_audit
from app.authz.owners import is_owner_member
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock

__all__ = [
    "CannotRevokeOwner",
    "InvalidMembershipRole",
    "InvalidMembershipStatus",
    "MembershipAlreadyExists",
    "MembershipNotFound",
    "MembershipRead",
    "MembershipRole",
    "MembershipStatus",
    "NotOwnerWorkspaceMember",
    "NotWorkspaceMember",
    "OwnerWorkspaceMissing",
    "TransferDemoteAction",
    "accept_invite",
    "invite_workspace",
    "list_memberships",
    "revoke_workspace",
    "transfer_ownership",
    "update_membership_role",
    "update_share_guest_identity",
]


# ---------------------------------------------------------------------------
# Enums (string literals — keep parity with the DB CHECK constraints)
# ---------------------------------------------------------------------------


# Role surface accepted by the membership service. ``owner_workspace``
# is intentionally absent: promotion to owner is :func:`transfer_ownership`'s
# exclusive surface, so any caller that asks for it via
# :func:`update_membership_role` or :func:`invite_workspace` gets
# :class:`InvalidMembershipRole` before reaching the DB.
MembershipRole = Literal["managed_workspace", "observer_workspace"]
_NON_OWNER_ROLES: frozenset[str] = frozenset(
    {"managed_workspace", "observer_workspace"}
)

MembershipStatus = Literal["invited", "active"]
_VALID_STATUSES: frozenset[str] = frozenset({"invited", "active"})

# What :func:`transfer_ownership` does with the *outgoing* owner row.
TransferDemoteAction = Literal["observer", "revoke"]
_VALID_DEMOTE_ACTIONS: frozenset[str] = frozenset({"observer", "revoke"})

# Role grants that satisfy the broader :func:`list_memberships` read
# gate ("actor must reach at least one of the linked workspaces").
# Excludes ``guest`` (read-only on a single property, never workspace-
# membership) and ``client`` (org-scoped billing surface, not
# operational membership). Write paths use the stricter ``owners``-
# group gate via :func:`is_owner_member` instead.
_AUTHORISED_GRANT_ROLES: frozenset[str] = frozenset({"manager", "worker"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MembershipNotFound(LookupError):
    """The (property, workspace) pair does not have a junction row.

    404-equivalent. Surfaces from :func:`accept_invite`,
    :func:`revoke_workspace`, :func:`update_membership_role`, and
    :func:`update_share_guest_identity` when the targeted row is
    absent. The HTTP layer maps :class:`LookupError` to 404.
    """


class OwnerWorkspaceMissing(LookupError):
    """The property has no ``owner_workspace`` row.

    Should never happen on a healthy database — every property is
    bootstrapped with an owner row at create time, and the
    :func:`transfer_ownership` invariant keeps exactly one in place.
    Surfaces as 404 if a caller asks about a property id that does
    not exist at all.
    """


class NotOwnerWorkspaceMember(PermissionError):
    """The acting user is not a member of the owner workspace's ``owners`` group.

    403-equivalent. Raised by the four mutations that demand
    owner-workspace authority (invite, revoke, update_membership_role,
    transfer_ownership) and by :func:`update_share_guest_identity`.
    The check uses the property's actual ``owner_workspace`` row, *not*
    the caller's :class:`WorkspaceContext` — an ``owners`` member whose
    ctx is pinned to the agency workspace cannot invite via that ctx
    if the property is owned by a different workspace where they hold
    no ``owners`` membership.

    Authority pinned to the ``owners`` permission group (§22, §02
    "permission_group" §"governance anchor"), not merely to a
    worker-or-higher role grant: operational managers / workers run
    the workspace day-to-day but do **not** have authority to widen
    or narrow which sibling workspaces share the property.
    """


class NotWorkspaceMember(PermissionError):
    """The acting user is not a member of the targeted workspace's ``owners`` group.

    403-equivalent. Raised by :func:`accept_invite` when the actor is
    not an ``owners``-group member of the *accepting* workspace
    (matches §22 "``property_workspace_invite.accept`` requires
    ``owners`` membership on the accepting workspace"), and by
    :func:`list_memberships` when the actor cannot reach any of the
    rows' workspaces under the broader read gate.
    """


class CannotRevokeOwner(ValueError):
    """:func:`revoke_workspace` cannot remove the owner row.

    422-equivalent. Use :func:`transfer_ownership` to re-point the
    owner before revoking the outgoing one.
    """


class MembershipAlreadyExists(ValueError):
    """A junction row already exists for ``(property_id, workspace_id)``.

    409-equivalent. The composite PK rejects a duplicate insert at the
    DB level; the service runs a pre-flight SELECT so the surface
    error message stays canonical instead of an
    ``IntegrityError``.
    """


class InvalidMembershipRole(ValueError):
    """The caller passed a role outside :data:`MembershipRole`.

    422-equivalent. Notably, ``owner_workspace`` is rejected here —
    use :func:`transfer_ownership`. The CHECK constraint on
    ``membership_role`` is the safety net; this is the primary gate.
    """


class InvalidMembershipStatus(ValueError):
    """The persisted ``status`` value is outside the accepted enum.

    Defence-in-depth. The DB CHECK rejects unknown values; this fires
    only if the constraint is somehow disabled or a write skipped the
    service layer.
    """


# ---------------------------------------------------------------------------
# Read projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MembershipRead:
    """Immutable read projection of a ``property_workspace`` row.

    The composite PK ``(property_id, workspace_id)`` is the row's
    natural identity; ``label``, ``membership_role``, ``status``,
    ``share_guest_identity``, ``created_at`` ride along.
    """

    property_id: str
    workspace_id: str
    label: str
    membership_role: str
    status: str
    share_guest_identity: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_view(row: PropertyWorkspace) -> MembershipRead:
    """Project a loaded :class:`PropertyWorkspace` row into a read view."""
    return MembershipRead(
        property_id=row.property_id,
        workspace_id=row.workspace_id,
        label=row.label,
        membership_role=row.membership_role,
        status=row.status,
        share_guest_identity=row.share_guest_identity,
        created_at=row.created_at,
    )


def _view_to_diff_dict(view: MembershipRead) -> dict[str, Any]:
    """Flatten a :class:`MembershipRead` into a JSON-safe audit payload."""
    return {
        "property_id": view.property_id,
        "workspace_id": view.workspace_id,
        "label": view.label,
        "membership_role": view.membership_role,
        "status": view.status,
        "share_guest_identity": view.share_guest_identity,
        "created_at": view.created_at.isoformat(),
    }


def _load_all_rows(
    session: Session,
    *,
    property_id: str,
) -> list[PropertyWorkspace]:
    """Return every junction row for ``property_id``, ordered for stability.

    Reads are tenant-agnostic because the membership service is
    inherently cross-workspace: the caller's
    :class:`WorkspaceContext` workspace_id may not equal the row's
    workspace_id in legitimate use. Authorization is enforced
    separately via :func:`_assert_owner_workspace_owners_member` /
    :func:`_assert_owners_member` (write paths) or
    :func:`_user_can_reach_workspace` (the broader
    :func:`list_memberships` read gate) before any caller sees the
    result.
    """
    # cd-hsk membership service spans workspaces by design.
    # justification: per-row tenant predicate cannot apply to a multi-
    # workspace lookup; auth enforced at service level.
    with tenant_agnostic():
        rows = session.scalars(
            select(PropertyWorkspace)
            .where(PropertyWorkspace.property_id == property_id)
            .order_by(
                # Owner row first, then alphabetical workspace_id for a
                # stable list shape (workspace_id is a ULID — sortable).
                PropertyWorkspace.membership_role.asc(),
                PropertyWorkspace.workspace_id.asc(),
            )
        ).all()
    return list(rows)


def _load_row(
    session: Session,
    *,
    property_id: str,
    workspace_id: str,
) -> PropertyWorkspace | None:
    """Return the row for ``(property_id, workspace_id)`` or ``None``.

    Tenant-agnostic for the same reason as :func:`_load_all_rows`.
    """
    # cd-hsk reads a sibling workspace's junction row by composite PK.
    # justification: workspace_id may not match the caller's ctx; auth
    # is enforced separately by the calling service before any mutation.
    with tenant_agnostic():
        return session.scalars(
            select(PropertyWorkspace).where(
                PropertyWorkspace.property_id == property_id,
                PropertyWorkspace.workspace_id == workspace_id,
            )
        ).one_or_none()


def _find_owner_row(
    rows: Sequence[PropertyWorkspace],
) -> PropertyWorkspace:
    """Return the single ``owner_workspace`` row from ``rows`` or raise.

    The §02 invariant pins exactly one owner row per property; an
    empty / absent owner here means the property does not exist at
    all (every property is seeded with an owner at create time).
    """
    for row in rows:
        if row.membership_role == "owner_workspace":
            return row
    raise OwnerWorkspaceMissing(
        f"property has no owner_workspace row (property_id={rows[0].property_id!r})"
        if rows
        else "property has no rows"
    )


def _user_can_reach_workspace(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
) -> bool:
    """Return ``True`` iff ``user_id`` holds a worker-or-higher grant.

    Used **only** by the :func:`list_memberships` read gate — write
    paths route through :func:`is_owner_member` for the stricter
    ``owners``-group check. "Active" today means "row exists" — the
    v1 ``role_grant`` schema has no expiry / soft-delete column
    (revoke is a hard DELETE; see :mod:`app.domain.identity.role_grants`).
    When that lands the predicate widens to also exclude tombstoned
    rows.

    Guest / client grants are deliberately excluded:

    * ``guest`` is single-property and read-only — not a basis for
      workspace-membership operations.
    * ``client`` is the billing-org surface (§22 client login) — also
      not a workspace-membership signal.
    """
    # cross-workspace authorization read; ctx workspace may differ
    # from the workspace we are checking authority on.
    # justification: sibling workspace member reading the sharing list.
    with tenant_agnostic():
        stmt = (
            select(RoleGrant.id)
            .where(
                RoleGrant.workspace_id == workspace_id,
                RoleGrant.user_id == user_id,
                RoleGrant.grant_role.in_(_AUTHORISED_GRANT_ROLES),
                RoleGrant.scope_kind == "workspace",
            )
            .limit(1)
        )
        return session.scalars(stmt).first() is not None


def _assert_owner_workspace_owners_member(
    session: Session,
    *,
    owner_workspace_id: str,
    user_id: str,
) -> None:
    """Raise :class:`NotOwnerWorkspaceMember` if the actor is not in ``owners``.

    The check runs against the property's actual ``owner_workspace``
    — *not* the caller's :class:`WorkspaceContext`. An ``owners``
    member whose ctx is pinned to the agency workspace can still
    invite / revoke / transfer only when the property's owner row
    points at a workspace where they hold ``owners`` membership.

    Routes through :func:`app.authz.owners.is_owner_member` — the
    canonical "is U an ``owners@<workspace>`` member?" lookup —
    rather than the caller's :attr:`WorkspaceContext.actor_was_owner_member`
    flag: that flag is a snapshot of *the caller's pinned workspace*
    set once at request entry, but the multi-belonging service must
    check the *property's owner workspace*, which is generally a
    different one. ``is_owner_member`` performs the check directly
    against the supplied ``owner_workspace_id``.
    """
    if not is_owner_member(
        session,
        workspace_id=owner_workspace_id,
        user_id=user_id,
    ):
        raise NotOwnerWorkspaceMember(
            f"user {user_id!r} is not a member of the 'owners' group on "
            f"owner workspace {owner_workspace_id!r}"
        )


def _assert_owners_member(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
) -> None:
    """Raise :class:`NotWorkspaceMember` if the actor is not an ``owners`` member.

    Used by :func:`accept_invite` to gate the recipient side. §22
    "``property_workspace_invite.accept``" pins this to the
    accepting workspace's ``owners`` group — a worker on the
    accepting workspace cannot consent to the share on behalf of
    their workspace.
    """
    if not is_owner_member(
        session,
        workspace_id=workspace_id,
        user_id=user_id,
    ):
        raise NotWorkspaceMember(
            f"user {user_id!r} is not a member of the 'owners' group on "
            f"workspace {workspace_id!r}"
        )


def _lock_for_transfer(
    session: Session,
    *,
    property_id: str,
    outgoing_workspace_id: str,
    incoming_workspace_id: str,
) -> None:
    """Acquire row locks on the two rows :func:`transfer_ownership` mutates.

    Loads both junction rows with ``SELECT ... FOR UPDATE`` and
    ``populate_existing()`` so a concurrent transfer on the same
    property (a) serialises on the row lock and (b) the calling
    session sees the post-lock data instead of the cached snapshot
    from the earlier non-locking ``_load_all_rows`` read. Without
    ``populate_existing`` the identity map would hand back the old
    object unchanged after the lock acquisition, and the caller
    would re-write the property with stale state.

    Lock-acquisition order is sorted by workspace_id (ULID) so two
    callers can never deadlock by acquiring the locks in opposite
    order. SQLite ignores ``FOR UPDATE``, but its
    ``BEGIN IMMEDIATE`` / whole-database write serialisation pins the
    invariant by other means; emitting the clause is harmless.
    """
    # Sorted ascending so the lock-acquisition order is deterministic
    # across callers — a transfer X→Y and a transfer Y→X otherwise
    # deadlock on each other.
    ordered = sorted({outgoing_workspace_id, incoming_workspace_id})
    # transfer_ownership locks both junction rows across two workspaces.
    # justification: per-row tenant predicate would refuse the cross-
    # workspace read; authorization is asserted before this helper.
    with tenant_agnostic():
        for workspace_id in ordered:
            session.scalars(
                select(PropertyWorkspace)
                .where(
                    PropertyWorkspace.property_id == property_id,
                    PropertyWorkspace.workspace_id == workspace_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one_or_none()


def _validate_status(value: str) -> MembershipStatus:
    """Narrow a loaded status string to the :data:`MembershipStatus` literal.

    Defence-in-depth: the DB CHECK rejects unknown values; this gate
    fires only if the constraint somehow misbehaved.
    """
    if value not in _VALID_STATUSES:
        raise InvalidMembershipStatus(f"unknown property_workspace.status {value!r}")
    # Narrowing: ``MembershipStatus`` is the union of the two valid
    # strings; a guard above proves we're in that set.
    if value == "invited":
        return "invited"
    return "active"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_memberships(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
) -> Sequence[MembershipRead]:
    """Return every junction row for ``property_id``, scoped to the caller's reach.

    The actor must hold an active worker-or-higher role grant on at
    least one of the property's linked workspaces. The read gate is
    intentionally broader than the write gate — a worker on a
    managed sibling can read the sharing tab, but only ``owners`` of
    the owner workspace may mutate it. A property the actor cannot
    reach collapses to :class:`MembershipNotFound` rather than a 403,
    matching the "tenant surface is not enumerable" stance — the
    response shape does not distinguish "no such property" from
    "property exists in a workspace you can't see".
    """
    rows = _load_all_rows(session, property_id=property_id)
    if not rows:
        raise MembershipNotFound(property_id)

    # Authorize: the actor must reach at least one of the rows'
    # workspaces. Walk the rows in order; first match wins. The read
    # gate is intentionally broader than the write gate — a worker
    # on a managed sibling can read the sharing tab even though only
    # the owner workspace's ``owners`` members may mutate it.
    authorised = any(
        _user_can_reach_workspace(
            session, workspace_id=row.workspace_id, user_id=ctx.actor_id
        )
        for row in rows
    )
    if not authorised:
        # Collapse "you can't see this property" to "no such property"
        # so the listing API does not enumerate workspaces the caller
        # is not a member of.
        raise MembershipNotFound(property_id)

    return [_row_to_view(row) for row in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def invite_workspace(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    target_workspace_id: str,
    role: MembershipRole,
    share_guest_identity: bool = False,
    clock: Clock | None = None,
) -> MembershipRead:
    """Mint a non-owner junction row in ``status='invited'``.

    Authorization: the actor must be a member of the ``owners``
    permission group on the property's *owner* workspace (regardless
    of the workspace the caller's :class:`WorkspaceContext` is
    pinned to). Operational managers / workers do **not** carry
    authority to invite peer workspaces — §22 pins
    ``property_workspace_invite.create`` to ``owners`` membership.

    Pre-flight: a ``MembershipAlreadyExists`` row collision raises
    before flush so the surface error is canonical instead of an
    ``IntegrityError``. The composite PK is the safety net.

    Records one ``property_workspace.invited`` audit row with the
    minted row as the ``after`` diff. ``before`` is omitted because
    the row didn't exist.
    """
    if role not in _NON_OWNER_ROLES:
        raise InvalidMembershipRole(
            f"invite_workspace cannot mint role {role!r}; "
            "use transfer_ownership for owner_workspace"
        )
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    rows = _load_all_rows(session, property_id=property_id)
    if not rows:
        raise OwnerWorkspaceMissing(property_id)
    owner_row = _find_owner_row(rows)
    _assert_owner_workspace_owners_member(
        session, owner_workspace_id=owner_row.workspace_id, user_id=ctx.actor_id
    )

    # Pre-flight duplicate check — the composite PK would raise an
    # IntegrityError at flush, but the canonical message is friendlier.
    if any(r.workspace_id == target_workspace_id for r in rows):
        raise MembershipAlreadyExists(
            f"property {property_id!r} already linked to workspace "
            f"{target_workspace_id!r}"
        )

    # ``label`` defaults to the owner row's label so the recipient
    # gets a sensible starting display name. They can rename it
    # later through their own membership UI.
    new_row = PropertyWorkspace(
        property_id=property_id,
        workspace_id=target_workspace_id,
        label=owner_row.label,
        membership_role=role,
        share_guest_identity=share_guest_identity,
        status="invited",
        created_at=now,
    )
    session.add(new_row)
    session.flush()

    view = _row_to_view(new_row)
    write_audit(
        session,
        ctx,
        entity_kind="property_workspace",
        entity_id=property_id,
        action="invited",
        diff={"after": _view_to_diff_dict(view)},
        clock=resolved_clock,
    )
    return view


def accept_invite(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    accepting_workspace_id: str,
    clock: Clock | None = None,
) -> MembershipRead:
    """Flip an ``invited`` row to ``status='active'``.

    Authorization: the actor must be a member of the ``owners``
    permission group on the *accepting* workspace (the recipient
    side of the invite). Owner-workspace authority is *not* required
    — the invite already encoded the owner's consent at mint time.
    Pinning the recipient gate to ``owners`` matches §22
    ``property_workspace_invite.accept`` — operational managers /
    workers cannot consent to a share on behalf of their workspace.

    Idempotent: a row that is already ``active`` is returned
    unchanged with no audit row written. Replays from a flaky
    network are silent.

    The owner-workspace row cannot be "accepted" — it is always
    bootstrapped ``active``. Calling :func:`accept_invite` on the
    owner row raises :class:`MembershipNotFound` (the row exists
    but is not pending) — the surface mirrors "no invite to accept".
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    _assert_owners_member(
        session, workspace_id=accepting_workspace_id, user_id=ctx.actor_id
    )

    row = _load_row(
        session, property_id=property_id, workspace_id=accepting_workspace_id
    )
    if row is None:
        raise MembershipNotFound(
            f"no invitation for property {property_id!r} on workspace "
            f"{accepting_workspace_id!r}"
        )

    if row.membership_role == "owner_workspace":
        # Owner rows are never ``invited``; if a caller tries to
        # accept their own owner row, surface it as "no pending
        # invitation" rather than silently returning success.
        raise MembershipNotFound(
            f"owner_workspace row for property {property_id!r} cannot be accepted"
        )

    current_status = _validate_status(row.status)
    if current_status == "active":
        # Idempotent replay — return the current state without
        # writing a duplicate audit row.
        return _row_to_view(row)

    before = _row_to_view(row)
    row.status = "active"
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="property_workspace",
        entity_id=property_id,
        action="accepted",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    # ``now`` is not stamped on the row today — the v1 schema has no
    # ``accepted_at`` / ``updated_at`` column. The audit row carries
    # the timestamp for forensics; the schema gains a column when
    # the wider §22 invite work lands.
    del now
    return after


def revoke_workspace(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    target_workspace_id: str,
    clock: Clock | None = None,
) -> MembershipRead:
    """Remove a non-owner junction row.

    Authorization: actor must be a member of the ``owners``
    permission group on the property's owner workspace (§22).

    Refuses to remove the ``owner_workspace`` row — that path is
    :func:`transfer_ownership`, not revoke.

    Records one ``property_workspace.revoked`` audit row with the
    removed row as ``before``. The post-delete view is also returned
    so the caller can echo it back.
    """
    resolved_clock = clock if clock is not None else SystemClock()

    rows = _load_all_rows(session, property_id=property_id)
    if not rows:
        raise OwnerWorkspaceMissing(property_id)
    owner_row = _find_owner_row(rows)
    _assert_owner_workspace_owners_member(
        session, owner_workspace_id=owner_row.workspace_id, user_id=ctx.actor_id
    )

    target_row: PropertyWorkspace | None = None
    for r in rows:
        if r.workspace_id == target_workspace_id:
            target_row = r
            break
    if target_row is None:
        raise MembershipNotFound(
            f"property {property_id!r} not linked to workspace {target_workspace_id!r}"
        )
    if target_row.membership_role == "owner_workspace":
        raise CannotRevokeOwner(
            "owner_workspace row cannot be revoked; "
            "use transfer_ownership to re-point ownership first"
        )

    before = _row_to_view(target_row)
    session.delete(target_row)
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="property_workspace",
        entity_id=property_id,
        action="revoked",
        diff={"before": _view_to_diff_dict(before)},
        clock=resolved_clock,
    )
    return before


def update_membership_role(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    target_workspace_id: str,
    role: MembershipRole,
    clock: Clock | None = None,
) -> MembershipRead:
    """Flip a non-owner row between ``managed`` and ``observer``.

    Authorization: actor must be a member of the ``owners``
    permission group on the property's owner workspace (§22).

    Cannot mint ``owner_workspace`` here — promotion to owner is
    :func:`transfer_ownership`'s exclusive surface, so a caller who
    asks for ``owner_workspace`` gets :class:`InvalidMembershipRole`
    before reaching the DB. Cannot demote the existing owner either:
    the targeted row's role is checked first so an attempt to
    "update" the owner raises :class:`CannotRevokeOwner` — owner
    flips go through :func:`transfer_ownership`.

    A no-op (role already equal) is silent — no audit row, returns
    the unchanged view.

    Records one ``property_workspace.role_changed`` audit row with
    before / after diffs.
    """
    if role not in _NON_OWNER_ROLES:
        raise InvalidMembershipRole(
            f"update_membership_role cannot set role {role!r}; "
            "use transfer_ownership for owner_workspace"
        )
    resolved_clock = clock if clock is not None else SystemClock()

    rows = _load_all_rows(session, property_id=property_id)
    if not rows:
        raise OwnerWorkspaceMissing(property_id)
    owner_row = _find_owner_row(rows)
    _assert_owner_workspace_owners_member(
        session, owner_workspace_id=owner_row.workspace_id, user_id=ctx.actor_id
    )

    target_row: PropertyWorkspace | None = None
    for r in rows:
        if r.workspace_id == target_workspace_id:
            target_row = r
            break
    if target_row is None:
        raise MembershipNotFound(
            f"property {property_id!r} not linked to workspace {target_workspace_id!r}"
        )
    if target_row.membership_role == "owner_workspace":
        raise CannotRevokeOwner(
            "owner_workspace row cannot be re-roled; "
            "use transfer_ownership to re-point ownership"
        )

    if target_row.membership_role == role:
        return _row_to_view(target_row)

    before = _row_to_view(target_row)
    target_row.membership_role = role
    session.flush()
    after = _row_to_view(target_row)

    write_audit(
        session,
        ctx,
        entity_kind="property_workspace",
        entity_id=property_id,
        action="role_changed",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


def update_share_guest_identity(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    target_workspace_id: str,
    share_guest_identity: bool,
    clock: Clock | None = None,
) -> MembershipRead:
    """Toggle the ``share_guest_identity`` flag on a non-owner row.

    Authorization: actor must be a member of the ``owners``
    permission group on the property's owner workspace (§22). The
    owner row is **immutable** on this field — see the §02
    "property_workspace" note "Only editable on non-owner rows" —
    so an attempt to flip it raises :class:`CannotRevokeOwner`.

    Refuses to touch the owner row — the owner sees its own data
    unconditionally (§15 "Cross-workspace visibility"); the column
    on the owner row is meaningless. The CHECK constraint pins the
    column at ``False`` on the owner row by convention; the service
    surfaces an attempt to flip it as :class:`CannotRevokeOwner` so
    the error matches the "owner row is special" semantic across
    siblings.

    A no-op (flag already equal) is silent — no audit row, returns
    the unchanged view.

    Records one ``property_workspace.share_changed`` audit row.
    """
    resolved_clock = clock if clock is not None else SystemClock()

    rows = _load_all_rows(session, property_id=property_id)
    if not rows:
        raise OwnerWorkspaceMissing(property_id)
    owner_row = _find_owner_row(rows)
    _assert_owner_workspace_owners_member(
        session, owner_workspace_id=owner_row.workspace_id, user_id=ctx.actor_id
    )

    target_row: PropertyWorkspace | None = None
    for r in rows:
        if r.workspace_id == target_workspace_id:
            target_row = r
            break
    if target_row is None:
        raise MembershipNotFound(
            f"property {property_id!r} not linked to workspace {target_workspace_id!r}"
        )
    if target_row.membership_role == "owner_workspace":
        raise CannotRevokeOwner(
            "owner_workspace row's share_guest_identity is meaningless; "
            "the owner sees its own data unconditionally"
        )

    if target_row.share_guest_identity == share_guest_identity:
        return _row_to_view(target_row)

    before = _row_to_view(target_row)
    target_row.share_guest_identity = share_guest_identity
    session.flush()
    after = _row_to_view(target_row)

    write_audit(
        session,
        ctx,
        entity_kind="property_workspace",
        entity_id=property_id,
        action="share_changed",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


def transfer_ownership(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    new_owner_workspace_id: str,
    demote_to: TransferDemoteAction,
    clock: Clock | None = None,
) -> MembershipRead:
    """Re-point ``owner_workspace`` in one transaction.

    Authorization: actor must be a member of the ``owners``
    permission group on the *current* owner workspace (§22). The
    incoming workspace's consent is encoded by its prior
    ``invite + accept`` — :func:`transfer_ownership` only fires on a
    sibling that is already linked.

    Two-step flush in one transaction:

    1. The outgoing owner row is either demoted to
       ``observer_workspace`` (``demote_to='observer'``) or hard-
       deleted (``demote_to='revoke'``).
    2. The incoming sibling row is promoted to ``owner_workspace``
       and its status flipped to ``active`` (a transferred-to-but-
       still-invited row would violate the §02 "exactly one active
       owner" invariant).

    Both writes flush together so the §02 "exactly one
    ``owner_workspace`` per property" invariant is observable: the
    intermediate state where neither row carries the role would
    only be visible inside the same UoW, which the caller controls.

    **Concurrency.** The outgoing owner row and the incoming sibling
    row are loaded with ``SELECT ... FOR UPDATE`` so two concurrent
    transfers on the same property serialise on the row lock. Without
    this, READ-COMMITTED Postgres could let two transfers each see
    the same owner snapshot and re-point it to two different
    sibling workspaces, briefly producing two owner rows (the §02
    invariant would be observable as broken to a third reader).
    SQLite ignores ``FOR UPDATE`` at the SQL layer but its
    BEGIN-IMMEDIATE/whole-database serialisation already pins the
    invariant.

    The new owner must already be a sibling on the property — this
    function does not mint fresh junctions. Run :func:`invite_workspace`
    + :func:`accept_invite` first so the recipient has consented.

    Records one ``property_workspace.ownership_transferred`` audit
    row with the new owner row as ``after`` and the outgoing row as
    ``before``.
    """
    if demote_to not in _VALID_DEMOTE_ACTIONS:
        raise InvalidMembershipRole(
            f"demote_to must be one of {sorted(_VALID_DEMOTE_ACTIONS)}; "
            f"got {demote_to!r}"
        )
    resolved_clock = clock if clock is not None else SystemClock()

    rows = _load_all_rows(session, property_id=property_id)
    if not rows:
        raise OwnerWorkspaceMissing(property_id)
    owner_row = _find_owner_row(rows)
    _assert_owner_workspace_owners_member(
        session, owner_workspace_id=owner_row.workspace_id, user_id=ctx.actor_id
    )

    if owner_row.workspace_id == new_owner_workspace_id:
        # Self-transfer is a no-op — nothing to do, no audit row.
        return _row_to_view(owner_row)

    new_owner_row: PropertyWorkspace | None = None
    for r in rows:
        if r.workspace_id == new_owner_workspace_id:
            new_owner_row = r
            break
    if new_owner_row is None:
        raise MembershipNotFound(
            f"property {property_id!r} not linked to incoming owner workspace "
            f"{new_owner_workspace_id!r}; invite + accept first"
        )

    # Re-load the two rows under SELECT ... FOR UPDATE so a sibling
    # transaction trying the same transfer blocks until we commit.
    # Two-row lock acquisition order is (outgoing, incoming) sorted by
    # workspace_id ULID — every caller sees the same order, which
    # rules out two transfers deadlocking on each other.
    _lock_for_transfer(
        session,
        property_id=property_id,
        outgoing_workspace_id=owner_row.workspace_id,
        incoming_workspace_id=new_owner_workspace_id,
    )
    # The post-lock view of the owner row may have shifted under us
    # (a sibling transfer just committed first) — re-read and refuse
    # the operation if the property's owner is no longer who we
    # authorised against. Surfacing this as MembershipNotFound matches
    # "the inbound state we authorised on no longer exists".
    refreshed_owner = _load_row(
        session, property_id=property_id, workspace_id=owner_row.workspace_id
    )
    if refreshed_owner is None or refreshed_owner.membership_role != "owner_workspace":
        raise OwnerWorkspaceMissing(
            f"property {property_id!r} owner row changed under transfer; retry"
        )
    refreshed_incoming = _load_row(
        session, property_id=property_id, workspace_id=new_owner_workspace_id
    )
    if refreshed_incoming is None:
        raise MembershipNotFound(
            f"property {property_id!r} not linked to incoming owner workspace "
            f"{new_owner_workspace_id!r}; invite + accept first"
        )
    owner_row = refreshed_owner
    new_owner_row = refreshed_incoming

    before_old = _row_to_view(owner_row)
    before_new = _row_to_view(new_owner_row)

    # Step 1: handle the outgoing owner.
    if demote_to == "observer":
        owner_row.membership_role = "observer_workspace"
    else:  # revoke
        session.delete(owner_row)

    # Step 2: promote the incoming sibling. Pinning ``status='active'``
    # ensures the §02 invariant ("exactly one ACTIVE owner") holds
    # even if the new row was previously ``invited`` — a transfer is
    # a deliberate hand-over, not an invite-style two-sided consent.
    new_owner_row.membership_role = "owner_workspace"
    new_owner_row.status = "active"
    session.flush()

    after_new = _row_to_view(new_owner_row)

    diff: dict[str, Any] = {
        "before": {
            "outgoing_owner": _view_to_diff_dict(before_old),
            "incoming_sibling": _view_to_diff_dict(before_new),
        },
        "after": {
            "outgoing_owner": (
                None
                if demote_to == "revoke"
                else _view_to_diff_dict(_row_to_view(owner_row))
            ),
            "incoming_owner": _view_to_diff_dict(after_new),
            "demote_to": demote_to,
        },
    }
    write_audit(
        session,
        ctx,
        entity_kind="property_workspace",
        entity_id=property_id,
        action="ownership_transferred",
        diff=diff,
        clock=resolved_clock,
    )
    return after_new
