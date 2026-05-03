"""Integration tests for :mod:`app.domain.identity.role_grants`.

Exercises the owner-authority policy + last-owner protection +
cross-workspace scope guard against a real DB with the tenant
filter installed so every function walks the same code paths it
will when called from a FastAPI route handler (cd-rpxd).

Each test:

* Bootstraps a user + workspace (so the ``owners`` system group is
  seeded via :func:`seed_owners_system_group`, which also emits the
  self-bootstrap ``manager`` role grant).
* Sets a :class:`WorkspaceContext` for that workspace so the ORM
  filter and the audit writer both see a live context.
* Calls the domain service and asserts the resulting rows +
  corresponding ``audit_log`` entry / policy error.

See ``docs/specs/05-employees-and-roles.md`` §"Role grants" /
§"Surface grants at a glance" and
``docs/specs/02-domain-model.md`` §"role_grants".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.authz.repositories import (
    SqlAlchemyPermissionGroupRepository,
    SqlAlchemyRoleGrantRepository,
)
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.workspace.models import UserWorkspace
from app.domain.identity.permission_groups import add_member, list_groups
from app.domain.identity.role_grants import (
    CrossWorkspaceProperty,
    GrantRoleInvalid,
    LastOwnerGrantProtected,
    NotAuthorizedForRole,
    RoleGrantNotFound,
    RoleGrantUserNotFound,
    grant,
    list_grants,
    revoke,
)
from app.tenancy import registry, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace


def _pg_repo(session: Session) -> SqlAlchemyPermissionGroupRepository:
    """SA-backed ``PermissionGroupRepository`` for ``add_member`` /
    ``list_groups`` calls in this module's seeding helpers.
    """
    return SqlAlchemyPermissionGroupRepository(session)


def _rg_repo(session: Session) -> SqlAlchemyRoleGrantRepository:
    """SA-backed ``RoleGrantRepository`` for the role-grants service calls.

    The domain services accept the repository Protocol (cd-duv6); the
    integration tests wire the production SA-backed concretion so the
    full SQL round-trip is exercised on every assertion.
    """
    return SqlAlchemyRoleGrantRepository(session)


pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    """Re-register the workspace-scoped tables this test module depends on.

    A sibling unit test
    (``tests/unit/test_tenancy_orm_filter.py``) resets the
    process-wide registry in its autouse fixture. Without re-
    registering here the filter silently no-ops on subsequent tests —
    a soft failure mode we want the test to prove it doesn't rely
    on.
    """
    registry.register("role_grant")
    registry.register("permission_group")
    registry.register("permission_group_member")
    registry.register("property_workspace")
    registry.register("audit_log")


def _ctx_for(workspace_id: str, workspace_slug: str, actor_id: str) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to the given workspace."""
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLA",
    )


_SLUG_COUNTER = 0


def _next_slug() -> str:
    """Return a fresh, validator-compliant workspace slug for the test."""
    global _SLUG_COUNTER
    _SLUG_COUNTER += 1
    return f"rg-test-{_SLUG_COUNTER:05d}"


@pytest.fixture
def env(
    db_session: Session,
) -> Iterator[tuple[Session, WorkspaceContext]]:
    """Yield a ``(session, ctx)`` pair bound to a fresh workspace.

    Builds on the parent conftest's ``db_session`` fixture, which
    wraps every test in a SAVEPOINT transaction that rolls back on
    teardown — no manual scrub needed. Installs the tenant filter
    on the session directly so the ORM filter is active for every
    query the domain service runs, matching the production path.
    """
    install_tenant_filter(db_session)

    slug = _next_slug()
    clock = FrozenClock(_PINNED)

    user = bootstrap_user(
        db_session,
        email=f"{slug}@example.com",
        display_name=f"User {slug}",
        clock=clock,
    )
    ws = bootstrap_workspace(
        db_session,
        slug=slug,
        name=f"WS {slug}",
        owner_user_id=user.id,
        clock=clock,
    )
    ctx = _ctx_for(ws.id, ws.slug, user.id)

    token = set_current(ctx)
    try:
        yield db_session, ctx
    finally:
        reset_current(token)


def _owners_group_id(session: Session, ctx: WorkspaceContext) -> str:
    """Return the caller's seeded ``owners`` group id."""
    for ref in list_groups(_pg_repo(session), ctx):
        if ref.slug == "owners":
            return ref.id
    raise AssertionError("owners group missing — bootstrap should have seeded it")


def _add_second_user(session: Session, *, suffix: str, clock: FrozenClock) -> str:
    """Insert a second user row and return its id.

    ``user`` is tenant-agnostic (see
    :mod:`app.adapters.db.identity`) so no :func:`tenant_agnostic`
    guard is required here.
    """
    return bootstrap_user(
        session,
        email=f"second-{suffix}@example.com",
        display_name=f"Second {suffix}",
        clock=clock,
    ).id


def _materialise_user_workspace(
    session: Session, *, user_id: str, workspace_id: str, clock: FrozenClock
) -> None:
    """Ensure a ``user_workspace`` junction row exists for the given pair.

    Workspace-scoped queries against ``user_workspace`` fail closed
    when the filter can't find a matching row. Our test harness
    mints grants for users who aren't yet materialised via the
    production derive-job, so we insert the row by hand to keep
    downstream reads honest. The junction is tenant-agnostic-enough
    — its PK is ``(user_id, workspace_id)`` and the row is created
    outside the ORM filter because we wrap it in
    :func:`tenant_agnostic`.
    """
    # justification: seeding the derived ``user_workspace`` row in
    # tests before the production derive-job exists; the junction is
    # small and not used by any assertion in this module.
    with tenant_agnostic():
        session.add(
            UserWorkspace(
                user_id=user_id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=clock.now(),
            )
        )
        session.flush()


def _add_property_to_workspace(
    session: Session, *, workspace_id: str, clock: FrozenClock
) -> str:
    """Insert a property linked to ``workspace_id`` via ``property_workspace``.

    Returns the fresh ``property.id``. The ``property`` table is
    tenant-agnostic (one property can belong to many workspaces —
    see ``app.adapters.db.places``), so the insert runs inside a
    :func:`tenant_agnostic` block; the junction itself is workspace-
    scoped and its write is caught by the active context.
    """
    property_id = new_ulid()
    # justification: ``property`` is tenant-agnostic by design; the
    # junction write below carries the workspace boundary.
    with tenant_agnostic():
        session.add(
            Property(
                id=property_id,
                address="1 Test Street",
                timezone="Europe/Paris",
                tags_json=[],
                created_at=clock.now(),
            )
        )
        session.flush()
    session.add(
        PropertyWorkspace(
            property_id=property_id,
            workspace_id=workspace_id,
            label="Test property",
            membership_role="owner_workspace",
            created_at=clock.now(),
        )
    )
    session.flush()
    return property_id


# ---------------------------------------------------------------------------
# grant_role validation
# ---------------------------------------------------------------------------


class TestGrantRoleValidation:
    """``grant_role`` must be one of the accepted v1 values."""

    def test_invalid_grant_role_raises(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        target = _add_second_user(session, suffix="bad", clock=FrozenClock(_PINNED))
        with pytest.raises(GrantRoleInvalid) as exc:
            grant(
                _rg_repo(session),
                ctx,
                user_id=target,
                grant_role="bogus",
                clock=FrozenClock(_PINNED),
            )
        assert "bogus" in str(exc.value)

    @pytest.mark.parametrize("role", ["manager", "worker", "client", "guest"])
    def test_accepted_grant_roles(
        self, env: tuple[Session, WorkspaceContext], role: str
    ) -> None:
        """Every listed role survives the enum gate when minted by an owner."""
        session, ctx = env
        target = _add_second_user(session, suffix=role, clock=FrozenClock(_PINNED))
        ref = grant(
            _rg_repo(session),
            ctx,
            user_id=target,
            grant_role=role,
            clock=FrozenClock(_PINNED),
        )
        assert ref.grant_role == role

    def test_unknown_user_id_raises_user_not_found(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """An unknown ``user_id`` lands the typed 404 path, not a 500.

        Previously, the FK ``role_grant.user_id -> user.id`` would only
        trip at flush time and surface as :class:`IntegrityError` (HTTP
        500). The pre-flight existence probe now raises
        :class:`RoleGrantUserNotFound` so the router maps it to a 404
        ``user_not_found`` envelope.
        """
        session, ctx = env
        with pytest.raises(RoleGrantUserNotFound) as exc:
            grant(
                _rg_repo(session),
                ctx,
                user_id="0",
                grant_role="worker",
                clock=FrozenClock(_PINNED),
            )
        assert "0" in str(exc.value)
        # No row landed (the pre-flight short-circuits before the INSERT
        # runs, and even under the IntegrityError fallback the SAVEPOINT
        # in :meth:`insert_grant` would have rolled it back).
        leaked = session.scalars(
            select(RoleGrant).where(RoleGrant.user_id == "0")
        ).all()
        assert leaked == []

    def test_archived_user_id_raises_user_not_found(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """Archived users are tombstoned and reject the same as unknown ids.

        ``user.archived_at IS NOT NULL`` means the identity has been
        soft-deleted; the FK row physically remains so the insert
        would silently succeed. Mirrors the admin-side precedent at
        :func:`app.api.admin.admins._resolve_user` which collapses
        archived rows into ``user_not_found`` so a fresh authority
        cannot land on a tombstoned identity.
        """
        session, ctx = env
        target = _add_second_user(
            session, suffix="archived", clock=FrozenClock(_PINNED)
        )
        # Archive the user row directly — no service helper exists at
        # this layer, and the DB-level ``archived_at`` flip is the
        # canonical tombstone form.
        from app.adapters.db.identity.models import User

        user_row = session.scalars(select(User).where(User.id == target)).one()
        user_row.archived_at = _PINNED
        session.flush()

        with pytest.raises(RoleGrantUserNotFound):
            grant(
                _rg_repo(session),
                ctx,
                user_id=target,
                grant_role="worker",
                clock=FrozenClock(_PINNED),
            )
        leaked = session.scalars(
            select(RoleGrant).where(RoleGrant.user_id == target)
        ).all()
        assert leaked == []


# ---------------------------------------------------------------------------
# Owner-authority policy
# ---------------------------------------------------------------------------


class TestOwnerAuthority:
    """Only ``owners@<workspace>`` may mint ``manager`` grants (§05)."""

    def test_owner_can_grant_manager(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """The bootstrap user is an owner, so minting manager works."""
        session, ctx = env
        target = _add_second_user(session, suffix="mgr", clock=FrozenClock(_PINNED))
        ref = grant(
            _rg_repo(session),
            ctx,
            user_id=target,
            grant_role="manager",
            clock=FrozenClock(_PINNED),
        )
        assert ref.user_id == target
        assert ref.grant_role == "manager"
        assert ref.scope_property_id is None
        assert ref.created_by_user_id == ctx.actor_id

    def test_non_owner_non_manager_cannot_grant_anything(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """A caller with no owners-membership and no manager grant is rejected."""
        session, ctx = env
        clock = FrozenClock(_PINNED)
        outsider = _add_second_user(session, suffix="outside", clock=clock)
        _materialise_user_workspace(
            session, user_id=outsider, workspace_id=ctx.workspace_id, clock=clock
        )
        # Rewire ctx's actor to the outsider — they're not in
        # ``owners@<ws>`` and hold no role grant yet.
        outsider_ctx = _ctx_for(ctx.workspace_id, ctx.workspace_slug, outsider)
        token = set_current(outsider_ctx)
        try:
            target = _add_second_user(session, suffix="target", clock=clock)
            with pytest.raises(NotAuthorizedForRole):
                grant(
                    _rg_repo(session),
                    outsider_ctx,
                    user_id=target,
                    grant_role="worker",
                    clock=clock,
                )
        finally:
            reset_current(token)

    def test_manager_not_owner_cannot_grant_manager(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """A bare ``manager`` grant holder may NOT mint further manager grants."""
        session, ctx = env
        clock = FrozenClock(_PINNED)
        manager = _add_second_user(session, suffix="mgr-only", clock=clock)
        _materialise_user_workspace(
            session, user_id=manager, workspace_id=ctx.workspace_id, clock=clock
        )
        # Owner mints them a ``manager`` grant.
        grant(
            _rg_repo(session),
            ctx,
            user_id=manager,
            grant_role="manager",
            clock=clock,
        )
        # Now switch the acting user to that non-owner manager.
        manager_ctx = _ctx_for(ctx.workspace_id, ctx.workspace_slug, manager)
        token = set_current(manager_ctx)
        try:
            target = _add_second_user(session, suffix="mgr-target", clock=clock)
            with pytest.raises(NotAuthorizedForRole) as exc:
                grant(
                    _rg_repo(session),
                    manager_ctx,
                    user_id=target,
                    grant_role="manager",
                    clock=clock,
                )
            assert "manager" in str(exc.value).lower()
        finally:
            reset_current(token)

    def test_manager_not_owner_can_grant_worker(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """A ``manager`` non-owner may still mint ``worker`` (and the other
        sub-manager roles)."""
        session, ctx = env
        clock = FrozenClock(_PINNED)
        manager = _add_second_user(session, suffix="mgr-w", clock=clock)
        _materialise_user_workspace(
            session, user_id=manager, workspace_id=ctx.workspace_id, clock=clock
        )
        grant(
            _rg_repo(session),
            ctx,
            user_id=manager,
            grant_role="manager",
            clock=clock,
        )

        manager_ctx = _ctx_for(ctx.workspace_id, ctx.workspace_slug, manager)
        token = set_current(manager_ctx)
        try:
            for role in ("worker", "client", "guest"):
                target = _add_second_user(session, suffix=f"w-{role}", clock=clock)
                ref = grant(
                    _rg_repo(session),
                    manager_ctx,
                    user_id=target,
                    grant_role=role,
                    clock=clock,
                )
                assert ref.grant_role == role
                assert ref.created_by_user_id == manager
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Property-scope sanity
# ---------------------------------------------------------------------------


class TestPropertyScope:
    """``scope_property_id`` must live in the caller's workspace."""

    def test_same_workspace_property_allowed(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        property_id = _add_property_to_workspace(
            session, workspace_id=ctx.workspace_id, clock=clock
        )
        target = _add_second_user(session, suffix="prop-ok", clock=clock)
        ref = grant(
            _rg_repo(session),
            ctx,
            user_id=target,
            grant_role="worker",
            scope_property_id=property_id,
            clock=clock,
        )
        assert ref.scope_property_id == property_id

    def test_cross_workspace_property_rejected(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """A property tied to a sibling workspace is refused."""
        session, ctx = env
        clock = FrozenClock(_PINNED)

        # Seed a sibling workspace and a property that lives only there.
        other_owner = _add_second_user(session, suffix="other-owner", clock=clock)
        sibling = bootstrap_workspace(
            session,
            slug=_next_slug(),
            name="Sibling",
            owner_user_id=other_owner,
            clock=clock,
        )
        foreign_property = _add_property_to_workspace(
            session, workspace_id=sibling.id, clock=clock
        )

        target = _add_second_user(session, suffix="prop-bad", clock=clock)
        with pytest.raises(CrossWorkspaceProperty):
            grant(
                _rg_repo(session),
                ctx,
                user_id=target,
                grant_role="worker",
                scope_property_id=foreign_property,
                clock=clock,
            )

    def test_unknown_property_id_rejected(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """A property id that doesn't exist at all is also ``CrossWorkspaceProperty``.

        The check runs against ``property_workspace``; an unknown id
        fails the ``EXISTS`` the same way a sibling-workspace id
        does, and the caller's UX need is identical ("this property
        isn't yours"). Narrowing the error shape between the two
        cases would force a second DB round-trip for no benefit.
        """
        session, ctx = env
        target = _add_second_user(
            session, suffix="prop-ghost", clock=FrozenClock(_PINNED)
        )
        with pytest.raises(CrossWorkspaceProperty):
            grant(
                _rg_repo(session),
                ctx,
                user_id=target,
                grant_role="worker",
                scope_property_id="01HWA00000000000000000NONE",
                clock=FrozenClock(_PINNED),
            )


# ---------------------------------------------------------------------------
# list_grants
# ---------------------------------------------------------------------------


class TestListGrants:
    """``list_grants`` honours the optional ``user_id`` / property filter."""

    def test_list_returns_bootstrap_plus_new(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        worker = _add_second_user(session, suffix="ls-w", clock=clock)
        grant(
            _rg_repo(session),
            ctx,
            user_id=worker,
            grant_role="worker",
            clock=clock,
        )
        rows = list_grants(_rg_repo(session), ctx)
        # Bootstrap emits a single ``manager`` grant for the workspace
        # creator; adding one worker grant brings the total to two.
        assert len(rows) == 2
        roles = {r.grant_role for r in rows}
        assert roles == {"manager", "worker"}

    def test_filter_by_user_id(self, env: tuple[Session, WorkspaceContext]) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        worker = _add_second_user(session, suffix="flt-w", clock=clock)
        grant(
            _rg_repo(session),
            ctx,
            user_id=worker,
            grant_role="worker",
            clock=clock,
        )
        rows = list_grants(_rg_repo(session), ctx, user_id=worker)
        assert [r.grant_role for r in rows] == ["worker"]
        assert all(r.user_id == worker for r in rows)

    def test_filter_by_scope_property_id(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        property_id = _add_property_to_workspace(
            session, workspace_id=ctx.workspace_id, clock=clock
        )
        worker = _add_second_user(session, suffix="flt-p", clock=clock)
        grant(
            _rg_repo(session),
            ctx,
            user_id=worker,
            grant_role="worker",
            scope_property_id=property_id,
            clock=clock,
        )
        # Workspace-wide grant for the same user — must not appear in
        # the property-filtered list.
        grant(
            _rg_repo(session),
            ctx,
            user_id=worker,
            grant_role="client",
            clock=clock,
        )
        rows = list_grants(_rg_repo(session), ctx, scope_property_id=property_id)
        assert [r.grant_role for r in rows] == ["worker"]
        assert all(r.scope_property_id == property_id for r in rows)


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


class TestRevoke:
    """``revoke`` soft-retires the row, protects the last owner, and audits."""

    def test_revoke_worker_soft_retires_row(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """cd-x1xh: revoke is a soft-retire, not a hard-delete.

        The row stays in the table with ``revoked_at`` /
        ``revoked_by_user_id`` / ``ended_on`` stamped; live-grant read
        paths filter on ``revoked_at IS NULL`` so the grant is gone
        from the application's perspective without losing audit trail.
        """
        session, ctx = env
        clock = FrozenClock(_PINNED)
        target = _add_second_user(session, suffix="rvk-w", clock=clock)
        ref = grant(
            _rg_repo(session),
            ctx,
            user_id=target,
            grant_role="worker",
            clock=clock,
        )
        revoke(_rg_repo(session), ctx, grant_id=ref.id, clock=clock)

        # Live-grant view: the row is gone.
        live = list_grants(_rg_repo(session), ctx, user_id=target)
        assert [g.id for g in live] == []

        # Physical row: still present with the soft-retire stamps.
        row = session.scalar(select(RoleGrant).where(RoleGrant.id == ref.id))
        assert row is not None
        assert row.revoked_at is not None
        assert row.revoked_by_user_id == ctx.actor_id
        assert row.ended_on == _PINNED.date()

    def test_revoke_emits_audit(self, env: tuple[Session, WorkspaceContext]) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        target = _add_second_user(session, suffix="rvk-a", clock=clock)
        ref = grant(
            _rg_repo(session),
            ctx,
            user_id=target,
            grant_role="client",
            clock=clock,
        )
        revoke(_rg_repo(session), ctx, grant_id=ref.id, clock=clock)

        rows = _all_audit_for(session, entity_id=ref.id)
        actions = [r.action for r in rows]
        assert "granted" in actions
        assert "revoked" in actions
        revoked = next(r for r in rows if r.action == "revoked")
        assert revoked.entity_kind == "role_grant"
        assert revoked.diff == {
            "user_id": target,
            "grant_role": "client",
            "scope_property_id": None,
        }

    def test_revoke_property_scoped_records_scope_in_audit(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """Revoke of a property-scoped grant carries the scope in the audit diff.

        The ``scope_property_id`` field rides the ``revoked`` audit
        row so downstream forensics can reconstruct which property
        grant disappeared without walking back to the earlier
        ``granted`` entry (the revoke row's ``entity_id`` points at a
        deleted row).
        """
        session, ctx = env
        clock = FrozenClock(_PINNED)
        property_id = _add_property_to_workspace(
            session, workspace_id=ctx.workspace_id, clock=clock
        )
        target = _add_second_user(session, suffix="rvk-prop", clock=clock)
        ref = grant(
            _rg_repo(session),
            ctx,
            user_id=target,
            grant_role="worker",
            scope_property_id=property_id,
            clock=clock,
        )
        revoke(_rg_repo(session), ctx, grant_id=ref.id, clock=clock)

        rows = _all_audit_for(session, entity_id=ref.id)
        revoked = next(r for r in rows if r.action == "revoked")
        assert revoked.diff == {
            "user_id": target,
            "grant_role": "worker",
            "scope_property_id": property_id,
        }

    def test_revoke_unknown_grant_raises(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        with pytest.raises(RoleGrantNotFound):
            revoke(
                _rg_repo(session),
                ctx,
                grant_id="01HWA00000000000000000NONE",
                clock=FrozenClock(_PINNED),
            )

    def test_last_owner_manager_grant_protected(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """Cannot revoke the only owner's ``manager`` grant.

        The bootstrap seeds exactly one ``owners@<ws>`` member (the
        workspace creator). Their ``manager`` grant is the one we
        try to revoke — which would leave the workspace with an
        owner who no longer carries the manager surface, a
        governance-lockout condition.
        """
        session, ctx = env
        # The bootstrap grant is the one to revoke.
        grants = list_grants(_rg_repo(session), ctx, user_id=ctx.actor_id)
        manager_grants = [g for g in grants if g.grant_role == "manager"]
        assert len(manager_grants) == 1
        bootstrap_grant = manager_grants[0]

        with pytest.raises(LastOwnerGrantProtected):
            revoke(
                _rg_repo(session),
                ctx,
                grant_id=bootstrap_grant.id,
                clock=FrozenClock(_PINNED),
            )

    def test_revoke_manager_with_multiple_owners_succeeds(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """When a second owner exists, the original can still be revoked.

        Adds a second user to ``owners@<ws>`` first, then mints
        them a manager grant (required because our last-owner rule
        is triggered by the **target** user's owners-membership —
        the caller is separate). The *caller* stays the bootstrap
        owner so they retain authority to revoke.
        """
        session, ctx = env
        clock = FrozenClock(_PINNED)
        second_owner = _add_second_user(session, suffix="co-owner", clock=clock)
        _materialise_user_workspace(
            session,
            user_id=second_owner,
            workspace_id=ctx.workspace_id,
            clock=clock,
        )
        owners_group_id = _owners_group_id(session, ctx)
        add_member(
            _pg_repo(session),
            ctx,
            group_id=owners_group_id,
            user_id=second_owner,
            clock=clock,
        )
        # Mint a manager grant for the new owner so the revoke has a
        # target that's owner-adjacent.
        second_grant = grant(
            _rg_repo(session),
            ctx,
            user_id=second_owner,
            grant_role="manager",
            clock=clock,
        )

        # With two owners, we may safely revoke the second's manager
        # grant — the workspace still has an owner with a manager grant.
        revoke(_rg_repo(session), ctx, grant_id=second_grant.id, clock=clock)
        # Soft-retire (cd-x1xh): row stays, but is gone from live-grant
        # reads. Confirm both halves so a regression of either side
        # surfaces.
        live = list_grants(_rg_repo(session), ctx, user_id=second_owner)
        assert all(g.id != second_grant.id for g in live)
        row = session.scalar(select(RoleGrant).where(RoleGrant.id == second_grant.id))
        assert row is not None
        assert row.revoked_at is not None

    def test_revoke_worker_of_only_owner_succeeds(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """Last-owner protection is scoped to ``manager`` revokes only.

        A worker / client / guest grant on the sole owner never
        threatens the governance anchor, so revoke proceeds even
        when the target user is the only ``owners@<ws>`` member.
        """
        session, ctx = env
        clock = FrozenClock(_PINNED)
        # Give the bootstrap owner a second (non-manager) grant.
        extra = grant(
            _rg_repo(session),
            ctx,
            user_id=ctx.actor_id,
            grant_role="client",
            clock=clock,
        )
        revoke(_rg_repo(session), ctx, grant_id=extra.id, clock=clock)

        # Soft-retire (cd-x1xh): the row is preserved but absent from
        # live-grant reads.
        live_extra = [
            g
            for g in list_grants(_rg_repo(session), ctx, user_id=ctx.actor_id)
            if g.id == extra.id
        ]
        assert live_extra == []
        row = session.scalar(select(RoleGrant).where(RoleGrant.id == extra.id))
        assert row is not None
        assert row.revoked_at is not None

    def test_regrant_after_revoke_lands_fresh_row(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """cd-x1xh: the partial UNIQUE filters out soft-revoked rows.

        After a soft-revoke, granting the same ``(user, role,
        scope?)`` triple again must land a brand-new row — the partial
        UNIQUE's WHERE clause excludes the revoked one. This proves
        re-grant doesn't 409 against the dead row, and that the
        revoked row stays put alongside the fresh live one (history
        preservation).
        """
        session, ctx = env
        clock = FrozenClock(_PINNED)
        target = _add_second_user(session, suffix="regrant", clock=clock)
        first = grant(
            _rg_repo(session),
            ctx,
            user_id=target,
            grant_role="worker",
            clock=clock,
        )
        revoke(_rg_repo(session), ctx, grant_id=first.id, clock=clock)
        # Same triple as before — the partial UNIQUE permits it
        # because the prior row is now soft-revoked.
        second = grant(
            _rg_repo(session),
            ctx,
            user_id=target,
            grant_role="worker",
            clock=clock,
        )
        assert second.id != first.id

        live = list_grants(_rg_repo(session), ctx, user_id=target)
        assert [g.id for g in live] == [second.id]

        # Both rows survive in the table; the revoked one carries the
        # soft-retire stamps.
        all_rows = session.scalars(
            select(RoleGrant)
            .where(RoleGrant.user_id == target)
            .order_by(RoleGrant.created_at.asc(), RoleGrant.id.asc())
        ).all()
        assert {row.id for row in all_rows} == {first.id, second.id}
        first_row = next(r for r in all_rows if r.id == first.id)
        second_row = next(r for r in all_rows if r.id == second.id)
        assert first_row.revoked_at is not None
        assert second_row.revoked_at is None

    def test_duplicate_live_workspace_wide_grant_rejected_by_partial_unique(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """cd-x1xh: two live workspace-wide grants on the same triple are rejected.

        Standard SQL NULL-distinct UNIQUE semantics would let two
        ``(workspace, user, role, NULL)`` rows slip through —
        ``COALESCE(scope_property_id, '')`` inside the index collapses
        the NULL to a sentinel so the UNIQUE binds across NULL and
        non-NULL property scopes alike. We assert at the DB level (raw
        INSERT bypassing the domain service) so the test fails on a
        regression even if the domain layer adds its own pre-flight.
        """
        from sqlalchemy.exc import IntegrityError

        session, ctx = env
        clock = FrozenClock(_PINNED)
        target = _add_second_user(session, suffix="dup", clock=clock)
        # First insert via the domain service, so the grant lands with
        # the right shape and the audit/owner-authority paths run.
        grant(
            _rg_repo(session),
            ctx,
            user_id=target,
            grant_role="worker",
            clock=clock,
        )
        session.flush()
        # Sibling row with the same ``(workspace, user, role)`` triple
        # and ``scope_property_id IS NULL`` — would silently land
        # without the COALESCE-based partial UNIQUE.
        session.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=ctx.workspace_id,
                user_id=target,
                grant_role="worker",
                scope_kind="workspace",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=ctx.actor_id,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


# ---------------------------------------------------------------------------
# Audit emission (grant side)
# ---------------------------------------------------------------------------


class TestGrantAudit:
    """Every successful ``grant`` emits one audit row with the spec shape."""

    def test_grant_emits_audit(self, env: tuple[Session, WorkspaceContext]) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        target = _add_second_user(session, suffix="audit", clock=clock)
        ref = grant(
            _rg_repo(session),
            ctx,
            user_id=target,
            grant_role="worker",
            clock=clock,
        )
        rows = _all_audit_for(session, entity_id=ref.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.entity_kind == "role_grant"
        assert row.action == "granted"
        assert row.diff == {
            "user_id": target,
            "grant_role": "worker",
            "scope_property_id": None,
        }
        assert row.actor_id == ctx.actor_id

    def test_grant_with_property_records_scope_in_audit(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        property_id = _add_property_to_workspace(
            session, workspace_id=ctx.workspace_id, clock=clock
        )
        target = _add_second_user(session, suffix="prop-audit", clock=clock)
        ref = grant(
            _rg_repo(session),
            ctx,
            user_id=target,
            grant_role="worker",
            scope_property_id=property_id,
            clock=clock,
        )
        rows = _all_audit_for(session, entity_id=ref.id)
        assert len(rows) == 1
        assert rows[0].diff == {
            "user_id": target,
            "grant_role": "worker",
            "scope_property_id": property_id,
        }


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _all_audit_for(session: Session, *, entity_id: str) -> list[AuditLog]:
    """Return every audit row for ``entity_id`` ordered by creation."""
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == entity_id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
    )
