"""``role_grant`` CRUD + owner-authority policy.

Role grants are the **surface** model: they say "user U has a
persona on workspace W (optionally narrowed to a property)". A row
does not carry per-action authority — that lives on
``permission_rule`` — but the domain enforces **who may mint which
``grant_role``** right here, because it is part of the workspace's
governance invariants (§05 "Surface grants at a glance").

See ``docs/specs/05-employees-and-roles.md`` §"Role grants" /
§"Surface grants at a glance" / §"Permissions: surface, groups, and
action catalog" and ``docs/specs/02-domain-model.md`` §"role_grants".

Summary of the rules enforced in this module:

* ``grant_role`` must be in :data:`_VALID_GRANT_ROLES`; anything else
  raises :class:`GrantRoleInvalid` before we reach the DB — the
  CHECK constraint is a safety net, not the primary gate.
* **Owner-authority (§05).** Only a member of the scope's ``owners``
  permission group may mint a ``manager`` grant. ``worker`` /
  ``client`` / ``guest`` grants may additionally be minted by a
  caller who already holds an active ``manager`` grant on the
  workspace. Every other caller is rejected with
  :class:`NotAuthorizedForRole`.
* When ``scope_property_id`` is provided, it MUST reference a
  ``property_workspace`` row pinned to the caller's workspace; a
  property from a sibling workspace raises
  :class:`CrossWorkspaceProperty` so a grant cannot silently leak
  across tenants. (The ``role_grant.scope_property_id`` column is a
  soft reference to ``property.id`` today — the promoted FK lands
  with cd-8u5; until then the junction join is the authoritative
  scoping gate.)
* **Last-owner protection.** ``revoke`` refuses to retire a
  ``manager`` grant when doing so would leave ``owners@<workspace>``
  with **zero** members who still hold a live ``manager`` grant on
  the workspace (§02 "permission_group" §"Invariants" —
  administrative-reach invariant, cd-nj8m). Owners-membership and
  manager-grant-holding are two related but distinct concepts:
  losing the last manager-holding owner locks every governance UI
  out of the tenant even when the ``owners`` roster itself is
  populated. Other revokes — non-manager grants, or manager grants
  whose holder is not in ``owners`` — are unconstrained. The test
  matrix in ``tests/integration/identity/test_role_grants.py``
  documents the full V1 boundary.

* **Soft-retire on revoke (cd-x1xh).** ``revoke`` writes
  ``revoked_at`` + ``revoked_by_user_id`` + ``ended_on`` on the row
  instead of deleting it; the partial UNIQUE indexes
  (``uq_role_grant_*``) all carry a ``revoked_at IS NULL`` filter so
  the live partition still has at most one grant per
  ``(user, role, scope)``, and re-granting after revoke lands a
  fresh row beside the soft-retired prior. Read paths consult
  ``revoked_at IS NULL`` to surface only live grants — the audit
  trail survives in the table.

**Capability gates are NOT enforced here.** ``users.grant_role`` /
``users.revoke_grant`` (the §05 action-catalog entries) are the
HTTP router's job (cd-dzp + cd-rpxd). The domain service trusts its
caller on those and only enforces the workspace-governance
invariants listed above. Audit rows still record ``actor_*`` fields
so the trail survives whichever layer made the call.

Every mutation writes one :mod:`app.audit` row in the **same**
transaction as the INSERT / DELETE. The caller owns the
transaction boundary — the service never calls
``session.commit()`` (§01 "Key runtime invariants" #3).

**Architecture.** The module talks to a
:class:`~app.domain.identity.ports.RoleGrantRepository` Protocol
(cd-duv6 / cd-jzfc) — never to the SQLAlchemy model classes directly.
The SA-backed concretion at
:class:`app.adapters.db.authz.repositories.SqlAlchemyRoleGrantRepository`
covers both the ``role_grant`` rows and the ``property_workspace``
junction the cross-workspace property-scope check needs (those
adapters can import each other; only ``app.domain → app.adapters``
is forbidden). The repo also threads its open
:class:`~sqlalchemy.orm.Session` through ``repo.session`` so the
audit writer (``app.audit.write_audit``), the locking primitive
(:func:`app.domain.identity._owner_guard.owner_member_manager_grant_status_locked`),
and the owners-membership lookup
(:func:`app.authz.owners.is_owner_member`) keep using the same UoW.

The owners-membership lookup is delegated to
:func:`app.authz.owners.is_owner_member` so the tenancy middleware
(cd-7y4) and this module share one SELECT shape. That helper still
takes a raw ``Session`` today; once it gains its own Protocol seam
the ``repo.session`` accessor here can drop.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.audit import write_audit
from app.authz.owners import is_owner_member
from app.domain.identity._owner_guard import (
    owner_member_manager_grant_status_locked,
)
from app.domain.identity.ports import (
    RoleGrantRepository,
    RoleGrantRow,
    RoleGrantUserNotFoundError,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "CrossWorkspaceProperty",
    "GrantRoleInvalid",
    "LastOwnerGrantProtected",
    "NotAuthorizedForRole",
    "RoleGrantNotFound",
    "RoleGrantRef",
    "RoleGrantUserNotFound",
    "grant",
    "list_grants",
    "revoke",
]


# Accepted ``grant_role`` values at the domain surface. Matches the
# DB-level CHECK on ``role_grant.grant_role`` (§02 v1 enum); we also
# match the admin UI: only these four ever reach a write here. The
# ``admin`` grant_role (§05 "Admin surface") is a deployment-scope
# concept — not a workspace-scope grant — so it intentionally does
# not appear in this set.
_VALID_GRANT_ROLES: frozenset[str] = frozenset({"manager", "worker", "client", "guest"})


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoleGrantRef:
    """Immutable projection of a ``role_grant`` row.

    Returned by every read and write on :mod:`role_grants`. The
    domain service never hands back SQLAlchemy ``RoleGrant``
    instances — callers manipulate these frozen dataclasses, so a
    second call can't mutate a shared row through the ORM identity
    map.
    """

    id: str
    workspace_id: str
    user_id: str
    grant_role: str
    scope_property_id: str | None
    created_at: datetime
    created_by_user_id: str | None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RoleGrantNotFound(LookupError):
    """The requested grant does not exist in the caller's workspace."""


class RoleGrantUserNotFound(LookupError):
    """``user_id`` does not reference a live ``user`` row.

    404-equivalent. Raised when the caller asks to mint a grant for a
    user id that no ``user`` row carries — the ``user`` table is
    tenant-agnostic, so this is a pure existence probe and is
    independent of whether the user is currently a member of the
    workspace. The HTTP router maps this to a ``user_not_found``
    envelope rather than letting the FK violation surface as a 500.
    """


class GrantRoleInvalid(ValueError):
    """``grant_role`` is not one of :data:`_VALID_GRANT_ROLES`.

    422-equivalent — raised before any DB write so the CHECK
    constraint never trips on a value the service never meant to
    accept.
    """


class NotAuthorizedForRole(PermissionError):
    """The caller may not mint the requested ``grant_role``.

    403-equivalent. Raised when the owner-authority rules (§05) would
    reject the mint: only ``owners@<workspace>`` may grant
    ``manager``; ``worker`` / ``client`` / ``guest`` grants require
    the caller to be in ``owners@<workspace>`` **or** hold an active
    ``manager`` role grant.
    """


class CrossWorkspaceProperty(ValueError):
    """``scope_property_id`` names a property not linked to this workspace.

    422-equivalent — a property-scoped grant may only reference a
    property the caller's workspace already owns or shares through
    ``property_workspace``. Anything else silently widens the grant
    across tenants.
    """


class LastOwnerGrantProtected(ValueError):
    """Refuse to revoke the last ``manager`` grant held by an owner.

    409-equivalent. Removing this row would leave ``owners@<workspace>``
    with **zero** members who still carry a live ``manager`` grant on
    the workspace — every governance UI would be out of reach even
    though the ``owners`` roster itself remains populated. §02
    "permission_group" §"Invariants" forbids that state (cd-nj8m).
    The caller must mint a replacement ``manager`` grant on another
    owners-group member (or move ``owners`` membership to a user who
    already holds one) before revoking this seat.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_ref(row: RoleGrantRow) -> RoleGrantRef:
    """Project a seam-level :class:`RoleGrantRow` into the public ref.

    The repo already returned an immutable, frozen value object; we
    re-pack it into the public :class:`RoleGrantRef` shape so callers
    continue to receive the dataclass they were already typing
    against. Same field-by-field projection — no behaviour change.
    """
    return RoleGrantRef(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        grant_role=row.grant_role,
        scope_property_id=row.scope_property_id,
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
    )


def _load_grant(
    repo: RoleGrantRepository, ctx: WorkspaceContext, *, grant_id: str
) -> RoleGrantRow:
    """Load ``grant_id`` scoped to the caller's workspace or raise.

    The ORM tenant filter already constrains SELECTs to the active
    :class:`~app.tenancy.WorkspaceContext`, but the repo also asserts
    ``workspace_id`` explicitly so a misconfigured context fails
    loudly instead of silently returning a sibling workspace's row.
    """
    row = repo.get_grant(workspace_id=ctx.workspace_id, grant_id=grant_id)
    if row is None:
        raise RoleGrantNotFound(grant_id)
    return row


def _assert_authorized_to_grant(
    repo: RoleGrantRepository, ctx: WorkspaceContext, *, grant_role: str
) -> None:
    """Raise :class:`NotAuthorizedForRole` if the caller can't mint ``grant_role``.

    Owner-authority matrix (§05):

    * ``manager`` — only ``owners@<workspace>`` members.
    * ``worker`` / ``client`` / ``guest`` — ``owners@<workspace>`` OR
      an active ``manager`` role grant in the workspace.
    """
    if is_owner_member(
        repo.session, workspace_id=ctx.workspace_id, user_id=ctx.actor_id
    ):
        return
    if grant_role == "manager":
        raise NotAuthorizedForRole(
            "only members of 'owners' may grant the manager role"
        )
    # Non-owner: still OK if they already hold the manager surface.
    if repo.has_active_manager_grant(
        workspace_id=ctx.workspace_id, user_id=ctx.actor_id
    ):
        return
    raise NotAuthorizedForRole(
        f"caller is not authorized to mint a {grant_role!r} grant"
    )


def _assert_scope_property_in_workspace(
    repo: RoleGrantRepository,
    ctx: WorkspaceContext,
    *,
    scope_property_id: str,
) -> None:
    """Fail if ``scope_property_id`` isn't linked to the caller's workspace.

    The check runs against ``property_workspace`` — the junction
    table is the authoritative "this property belongs to this
    workspace" relation (§02 "property_workspace"). The ``property``
    table itself is tenant-agnostic and therefore cannot be filtered
    through the ORM tenant filter directly; the junction is
    workspace-scoped, so its own tenant predicate runs automatically.
    """
    if not repo.is_property_in_workspace(
        workspace_id=ctx.workspace_id, property_id=scope_property_id
    ):
        raise CrossWorkspaceProperty(
            f"property {scope_property_id!r} is not linked to this workspace"
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_grants(
    repo: RoleGrantRepository,
    ctx: WorkspaceContext,
    *,
    user_id: str | None = None,
    scope_property_id: str | None = None,
) -> Sequence[RoleGrantRef]:
    """Return every role grant in the caller's workspace, optionally filtered.

    Ordered by ``created_at`` ascending (with ``id`` as a stable
    tiebreaker inside the same millisecond) so the seeded owner
    grant always leads and subsequent mints appear in the order the
    workspace emitted them.

    ``user_id`` / ``scope_property_id`` are pure equality filters —
    the callers who need "grants for this user regardless of
    property" pass ``user_id`` alone; "grants on this property
    regardless of user" pass ``scope_property_id`` alone; passing
    both narrows to the intersection.
    """
    rows = repo.list_grants(
        workspace_id=ctx.workspace_id,
        user_id=user_id,
        scope_property_id=scope_property_id,
    )
    return [_row_to_ref(row) for row in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def grant(
    repo: RoleGrantRepository,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    grant_role: str,
    scope_property_id: str | None = None,
    clock: Clock | None = None,
) -> RoleGrantRef:
    """Mint a fresh ``role_grant`` row for ``user_id``.

    Enforces owner-authority (§05 "Surface grants at a glance") and
    the property-scope sanity rule (``scope_property_id`` must live
    in the caller's workspace through ``property_workspace``). Every
    successful mint emits one ``audit_log`` row with action
    ``granted``.

    Raises:

    * :class:`GrantRoleInvalid` — ``grant_role`` is not in
      :data:`_VALID_GRANT_ROLES`.
    * :class:`NotAuthorizedForRole` — caller is not a member of
      ``owners@<workspace>`` and does not hold a ``manager`` grant
      sufficient for the requested role.
    * :class:`CrossWorkspaceProperty` — ``scope_property_id`` does
      not reference a property linked to the caller's workspace.
    * :class:`RoleGrantUserNotFound` — ``user_id`` does not reference
      a live ``user`` row. Raised by the pre-flight existence probe
      (cheap path) and by the seam-level fallback that catches the
      deferred FK violation if the user is archived between the
      probe and the flush under READ COMMITTED Postgres.

    ``clock`` is optional; tests pin ``created_at`` via a
    :class:`~app.util.clock.FrozenClock`.
    """
    if grant_role not in _VALID_GRANT_ROLES:
        raise GrantRoleInvalid(grant_role)

    _assert_authorized_to_grant(repo, ctx, grant_role=grant_role)

    if scope_property_id is not None:
        _assert_scope_property_in_workspace(
            repo, ctx, scope_property_id=scope_property_id
        )

    # Pre-flight existence probe so an unknown ``user_id`` lands a
    # clean 404 ``user_not_found`` instead of a deferred FK
    # ``IntegrityError`` at flush time on
    # ``role_grant.user_id -> user.id``. The seam-level
    # ``RoleGrantUserNotFoundError`` (raised inside ``insert_grant``'s
    # SAVEPOINT) is the race-safety fallback under READ COMMITTED
    # Postgres if the user is archived between the probe and the insert.
    if not repo.user_exists(user_id=user_id):
        raise RoleGrantUserNotFound(user_id)

    now = (clock if clock is not None else SystemClock()).now()
    try:
        row = repo.insert_grant(
            grant_id=new_ulid(),
            workspace_id=ctx.workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_property_id=scope_property_id,
            created_at=now,
            created_by_user_id=ctx.actor_id,
        )
    except RoleGrantUserNotFoundError as exc:
        raise RoleGrantUserNotFound(user_id) from exc

    write_audit(
        repo.session,
        ctx,
        entity_kind="role_grant",
        entity_id=row.id,
        action="granted",
        diff={
            "user_id": user_id,
            "grant_role": grant_role,
            "scope_property_id": scope_property_id,
        },
        clock=clock,
    )
    return _row_to_ref(row)


def revoke(
    repo: RoleGrantRepository,
    ctx: WorkspaceContext,
    *,
    grant_id: str,
    clock: Clock | None = None,
) -> None:
    """Soft-retire the role grant identified by ``grant_id``.

    cd-x1xh moved revocation to the §02 soft-retire shape: the row
    is **preserved** with ``revoked_at`` + ``revoked_by_user_id`` +
    ``ended_on`` stamped instead of being hard-deleted. Live-grant
    read paths filter on ``revoked_at IS NULL`` so the user no
    longer sees the surface; the row remains for audit and a
    re-grant on the same ``(user, role, scope)`` triple lands a
    fresh row alongside (the partial UNIQUE indexes carry the
    same ``revoked_at IS NULL`` filter).

    Raises:

    * :class:`RoleGrantNotFound` — no live row in the caller's
      workspace with that id (the repo's live-only read collapses
      already-revoked rows to ``None`` so a double-revoke is the
      same 404 as an unknown id).
    * :class:`LastOwnerGrantProtected` — the grant is a ``manager``
      grant whose removal would leave ``owners@<workspace>`` with
      zero members holding a live ``manager`` grant (§02 admin-reach
      invariant, cd-nj8m). Mint a replacement ``manager`` grant on
      another owners-group member before revoking this seat.

    ``clock`` is optional; tests pin the revoke timestamp + the audit
    row's ``created_at`` via a :class:`~app.util.clock.FrozenClock`.
    """
    row = _load_grant(repo, ctx, grant_id=grant_id)

    # V1 pragmatic rule (see module docstring): only ``manager`` revokes
    # can ever reduce the count of "owners-group members holding a live
    # manager grant" — worker / client / guest revokes never affect the
    # governance anchor and always pass.
    if row.grant_role == "manager":
        # :func:`owner_member_manager_grant_status_locked` takes the
        # same lock on the owners-group row as
        # :func:`count_owner_members_locked` does, then checks whether
        # this grant holder is still in ``owners@<ws>`` and counts the
        # users who are BOTH in ``owners@<ws>`` AND hold at least one
        # ``manager`` grant — excluding the row we're about to revoke.
        # The shared lock serialises the membership-removal and
        # grant-revoke guards so a concurrent ``remove_member`` /
        # ``revoke`` on the same workspace cannot race us to a state
        # where the count silently tips to zero (cd-mb5n + cd-nj8m).
        owner_status = owner_member_manager_grant_status_locked(
            repo.session,
            workspace_id=ctx.workspace_id,
            user_id=row.user_id,
            exclude_grant_id=grant_id,
        )
        if owner_status.manager_holding_owner_count == 0 and (
            owner_status.is_owner_member or not owner_status.owners_group_exists
        ):
            raise LastOwnerGrantProtected(
                "cannot remove last manager grant on owners group; "
                "mint a replacement manager grant on another owners "
                "member first"
            )

    # Snapshot the fields the audit row needs; we also carry
    # ``scope_property_id`` into the audit payload so operational
    # forensics ("which property grant was retired?") can reconstruct
    # the soft-retired row at a glance without joining back to the
    # earlier ``granted`` entry.
    user_id = row.user_id
    grant_role = row.grant_role
    scope_property_id = row.scope_property_id

    now = (clock if clock is not None else SystemClock()).now()
    repo.soft_revoke_grant(
        workspace_id=ctx.workspace_id,
        grant_id=grant_id,
        revoked_at=now,
        revoked_by_user_id=ctx.actor_id,
        # ``ended_on`` is the spec-mandated effective-period close
        # (§02 "role_grants" §"ended_on"). UTC date matches the rest
        # of the v1 storage convention; "today_local_or_utc" is
        # workspace-tz-driven on the rendering side, not here.
        ended_on=now.date(),
    )

    write_audit(
        repo.session,
        ctx,
        entity_kind="role_grant",
        entity_id=grant_id,
        action="revoked",
        diff={
            "user_id": user_id,
            "grant_role": grant_role,
            "scope_property_id": scope_property_id,
        },
        clock=clock,
    )
