"""Concurrency tests for the last-owner TOCTOU fix (cd-mb5n).

The v1 guards in
:func:`app.domain.identity.permission_groups.remove_member` (cd-ckr)
and :func:`app.domain.identity.role_grants.revoke` (cd-79r) both
enforce §02 "permission_group" §"Invariants" — "the ``owners`` group
on any scope has at least one active member at all times" — with a
count-then-write pattern. Under concurrent transactions, two
operations can each observe ``owner_count == 2`` and both commit
their delete, leaving the ``owners`` group empty.

These tests spawn two threads that race the two guards against a
real DB (SQLite by default; Postgres when
``CREWDAY_TEST_DB=postgres``) and assert the invariant survives:
at least one transaction must refuse, and the ``owners`` group must
never drop below one member.

Each race scenario runs multiple iterations to flush out flake — a
flaky concurrency fix is worse than no fix, because it ships the
illusion of safety. The iteration count is small (5 per scenario) so
the suite stays under the unit-test budget; the deterministic lock
design keeps every iteration trustworthy.

See:

* cd-mb5n — TOCTOU fix acceptance criteria.
* :mod:`app.domain.identity._owner_guard` — the shared locking
  primitive.
* :mod:`tests.integration.auth.test_magic_link_concurrent` — the
  template this module follows for the isolated-engine + barrier
  pattern.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.authz.repositories import (
    SqlAlchemyPermissionGroupRepository,
    SqlAlchemyRoleGrantRepository,
)
from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.domain.identity import role_grants as role_grants_module
from app.domain.identity._owner_guard import OwnerMemberManagerGrantStatus
from app.domain.identity.permission_groups import (
    WouldOrphanOwnersGroup,
    add_member,
    list_groups,
    remove_member,
)
from app.domain.identity.role_grants import (
    LastOwnerGrantProtected,
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
    """SA-backed ``PermissionGroupRepository`` for cd-duv6 wiring."""
    return SqlAlchemyPermissionGroupRepository(session)


def _rg_repo(session: Session) -> SqlAlchemyRoleGrantRepository:
    """SA-backed ``RoleGrantRepository`` for cd-duv6 wiring."""
    return SqlAlchemyRoleGrantRepository(session)


pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_ITERATIONS = 5

# ``remove_member`` may now raise ``LastOwnerGrantProtected`` for
# cross-path races (cd-j5pu) in addition to the historical
# ``WouldOrphanOwnersGroup`` for roster emptying (cd-ckr). Worker
# threads catch the union so the test loop can classify outcomes.
_RemoveMemberRefusal = (WouldOrphanOwnersGroup, LastOwnerGrantProtected)


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`.

    The race tests each manage their own context inside worker threads
    (via :func:`set_current` on thread entry), so the outer test body
    never holds one.
    """
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    """Re-register workspace-scoped tables this module touches.

    A sibling unit test resets the registry in its autouse fixture; the
    existing integration tests for this package carry the same guard,
    so we mirror it here for parity.
    """
    registry.register("role_grant")
    registry.register("permission_group")
    registry.register("permission_group_member")
    registry.register("audit_log")
    registry.register("user_workspace")


@pytest.fixture
def isolated_engine(db_url: str) -> Iterator[Engine]:
    """Engine dedicated to the race tests.

    Built fresh because the session-scoped ``engine`` fixture is paired
    with the ``db_session`` savepoint-rollback pattern: a
    ``session.commit()`` inside a test commits only to the savepoint,
    not to the outer connection, so sibling worker threads opening
    their own connections never observe the row. Race tests need
    committed state visible across threads, so we build an engine
    whose transactions really do commit.
    """
    from app.adapters.db.session import make_engine

    eng = make_engine(db_url)
    try:
        yield eng
    finally:
        eng.dispose()


def _session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a tenant-filtered session factory bound to ``engine``.

    Worker threads each open a :class:`Session` through this factory
    so the ORM tenant filter is installed — the domain services
    under test expect it on every read.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


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


@dataclass
class _Workspace:
    """Handle for a workspace seeded outside any worker thread."""

    workspace_id: str
    workspace_slug: str
    owner_a_id: str
    owner_b_id: str
    owners_group_id: str
    owner_a_grant_id: str
    owner_b_grant_id: str


def _bootstrap_two_owner_workspace(
    factory: sessionmaker[Session], *, slug_suffix: str
) -> _Workspace:
    """Seed a workspace with two owner members, each with a manager grant.

    The setup mirrors the "two admins both trying to prune the
    ``owners`` group" scenario called out in cd-mb5n's description.
    Returns a :class:`_Workspace` handle so worker threads can act
    on specific owner / grant ids without re-looking them up under
    contention.
    """
    clock = FrozenClock(_PINNED)

    with factory() as session:
        owner_a = bootstrap_user(
            session,
            email=f"owner-a-{slug_suffix}@example.com",
            display_name=f"Owner A {slug_suffix}",
            clock=clock,
        )
        owner_b = bootstrap_user(
            session,
            email=f"owner-b-{slug_suffix}@example.com",
            display_name=f"Owner B {slug_suffix}",
            clock=clock,
        )
        workspace = bootstrap_workspace(
            session,
            slug=f"last-owner-race-{slug_suffix}",
            name=f"Race {slug_suffix}",
            owner_user_id=owner_a.id,
            clock=clock,
        )
        ctx = _ctx_for(workspace.id, workspace.slug, owner_a.id)
        token = set_current(ctx)
        try:
            # Promote owner B to ``owners@<ws>`` and mint them a
            # manager grant — parity with owner A so the workspace
            # enters the race with 2 owners x 1 manager grant each.
            owners_group_id: str | None = None
            for ref in list_groups(_pg_repo(session), ctx):
                if ref.slug == "owners":
                    owners_group_id = ref.id
                    break
            assert owners_group_id is not None

            add_member(
                _pg_repo(session),
                ctx,
                group_id=owners_group_id,
                user_id=owner_b.id,
                clock=clock,
            )
            grant_b = grant(
                _rg_repo(session),
                ctx,
                user_id=owner_b.id,
                grant_role="manager",
                clock=clock,
            )

            # Owner A already holds a manager grant from the bootstrap
            # path; look it up so the test can target it directly.
            a_manager_grants = [
                g
                for g in list_grants(_rg_repo(session), ctx, user_id=owner_a.id)
                if g.grant_role == "manager"
            ]
            assert len(a_manager_grants) == 1
            grant_a = a_manager_grants[0]

            session.commit()
        finally:
            reset_current(token)

        return _Workspace(
            workspace_id=workspace.id,
            workspace_slug=workspace.slug,
            owner_a_id=owner_a.id,
            owner_b_id=owner_b.id,
            owners_group_id=owners_group_id,
            owner_a_grant_id=grant_a.id,
            owner_b_grant_id=grant_b.id,
        )


def _scrub_workspace(factory: sessionmaker[Session], workspace_id: str) -> None:
    """Delete every row belonging to the test workspace.

    Each race iteration builds a fresh workspace so contention state
    doesn't leak across iterations. ``bootstrap_workspace`` commits
    rows across several tables (user, workspace, user_workspace,
    permission_group, permission_group_member, role_grant,
    audit_log); we wipe them all by workspace id so the next
    iteration sees a clean slate.

    Runs under :func:`tenant_agnostic` because the race scenarios
    commit some rows that violate the tenant filter's expectations
    (e.g. an empty ``owners`` group is what we're probing for). The
    scrub never assumes the workspace is in a valid governance
    state.
    """
    with factory() as session, tenant_agnostic():
        for model in (
            AuditLog,
            RoleGrant,
            PermissionGroupMember,
            PermissionGroup,
            UserWorkspace,
        ):
            rows = session.scalars(
                select(model).where(model.workspace_id == workspace_id)
            ).all()
            for r in rows:
                session.delete(r)
        ws = session.get(Workspace, workspace_id)
        if ws is not None:
            session.delete(ws)
        # Users are tenant-agnostic. The race tests give each user a
        # unique email so a leak across iterations is harmless, but
        # we sweep them to keep the DB compact across the 10 race
        # iterations run per file.
        for user in session.scalars(
            select(User).where(User.email.like(f"owner-%-{workspace_id}%@example.com"))
        ).all():
            session.delete(user)
        session.commit()


def _owners_member_count(factory: sessionmaker[Session], workspace_id: str) -> int:
    """Return the current ``owners@<workspace_id>`` member count.

    Used by the race tests to assert the post-commit invariant — the
    ``owners`` group must never drop below one member regardless of
    how the race resolved. Wrapped in :func:`tenant_agnostic` because
    the test reads across workspaces without setting a context.
    """
    with factory() as session, tenant_agnostic():
        stmt = (
            select(PermissionGroupMember)
            .join(
                PermissionGroup,
                PermissionGroup.id == PermissionGroupMember.group_id,
            )
            .where(
                PermissionGroup.workspace_id == workspace_id,
                PermissionGroup.slug == "owners",
                PermissionGroup.system.is_(True),
            )
        )
        return len(session.scalars(stmt).all())


def _owners_with_manager_grant_count(
    factory: sessionmaker[Session], workspace_id: str
) -> int:
    """Return the count of owners-group members who hold a manager grant.

    Probes the §02 "administrative reach" invariant directly: a
    workspace whose count drops to zero is administratively
    decapitated even when the ``owners`` roster itself is populated
    (cd-nj8m). Wrapped in :func:`tenant_agnostic` so the test can
    inspect the post-commit state without juggling a context.
    """
    with factory() as session, tenant_agnostic():
        stmt = (
            select(PermissionGroupMember.user_id)
            .join(
                PermissionGroup,
                PermissionGroup.id == PermissionGroupMember.group_id,
            )
            .join(
                RoleGrant,
                (RoleGrant.user_id == PermissionGroupMember.user_id)
                & (RoleGrant.workspace_id == workspace_id)
                & (RoleGrant.grant_role == "manager")
                & (RoleGrant.revoked_at.is_(None)),
            )
            .where(
                PermissionGroup.workspace_id == workspace_id,
                PermissionGroup.slug == "owners",
                PermissionGroup.system.is_(True),
            )
            .distinct()
        )
        return len(session.scalars(stmt).all())


@dataclass
class _RaceResult:
    """Aggregate outcomes across two racing threads.

    The race workers (:func:`_remove_member_worker`,
    :func:`_revoke_worker`) append to the same :class:`_RaceResult`
    under a mutex so the parent test can inspect the combined
    successes / errors without juggling two lists.
    """

    successes: list[str]
    errors: list[Exception]
    lock: threading.Lock


def _new_race_result() -> _RaceResult:
    return _RaceResult(successes=[], errors=[], lock=threading.Lock())


def _remove_member_worker(
    *,
    factory: sessionmaker[Session],
    ws: _Workspace,
    actor_id: str,
    target_user_id: str,
    start: threading.Barrier,
    result: _RaceResult,
) -> None:
    """Worker body: race :func:`remove_member` on the owners group.

    Opens its own :class:`Session`, sets a :class:`WorkspaceContext`
    tied to ``actor_id``, waits on ``start`` so both workers execute
    the service call as close to simultaneously as :mod:`threading`
    allows, commits on success, rolls back on either
    :class:`WouldOrphanOwnersGroup` (roster invariant, cd-ckr /
    cd-mb5n) or :class:`LastOwnerGrantProtected` (administrative-
    reach invariant on the membership-removal path, cd-j5pu).
    Outcomes land on ``result``'s shared lists under its mutex.
    """
    try:
        with factory() as session:
            ctx = _ctx_for(ws.workspace_id, ws.workspace_slug, actor_id)
            token = set_current(ctx)
            try:
                start.wait()
                try:
                    remove_member(
                        _pg_repo(session),
                        ctx,
                        group_id=ws.owners_group_id,
                        user_id=target_user_id,
                        clock=FrozenClock(_PINNED),
                    )
                    session.commit()
                    with result.lock:
                        result.successes.append(f"remove:{target_user_id}")
                except _RemoveMemberRefusal as exc:
                    session.rollback()
                    with result.lock:
                        result.errors.append(exc)
            finally:
                reset_current(token)
    except Exception as exc:  # pragma: no cover - harness
        with result.lock:
            result.errors.append(exc)


def _revoke_worker(
    *,
    factory: sessionmaker[Session],
    workspace_id: str,
    workspace_slug: str,
    actor_id: str,
    grant_id: str,
    start: threading.Barrier,
    result: _RaceResult,
) -> None:
    """Worker body: race :func:`revoke` on a manager grant.

    Mirror of :func:`_remove_member_worker` for the revoke guard;
    a :class:`LastOwnerGrantProtected` lands on ``result.errors``,
    a successful revoke on ``result.successes``.
    """
    try:
        with factory() as session:
            ctx = _ctx_for(workspace_id, workspace_slug, actor_id)
            token = set_current(ctx)
            try:
                start.wait()
                try:
                    revoke(
                        _rg_repo(session),
                        ctx,
                        grant_id=grant_id,
                        clock=FrozenClock(_PINNED),
                    )
                    session.commit()
                    with result.lock:
                        result.successes.append(f"revoke:{grant_id}")
                except LastOwnerGrantProtected as exc:
                    session.rollback()
                    with result.lock:
                        result.errors.append(exc)
            finally:
                reset_current(token)
    except Exception as exc:  # pragma: no cover - harness
        with result.lock:
            result.errors.append(exc)


class TestRemoveMemberRace:
    """Two threads race :func:`remove_member` on a 2-owner workspace.

    Pre-state: owners group has members A and B. One thread removes
    A, the other removes B. Without the lock, both see
    ``owner_count == 2`` and both DELETE, leaving the group empty
    (§02 invariant broken). With the lock, one serialises before the
    other; the second re-reads ``owner_count == 1`` and raises
    :class:`WouldOrphanOwnersGroup`.

    This is the primary scenario cd-mb5n exists to fix — two
    concurrent admins hitting "remove from owners" on different
    owners at the same moment.
    """

    def test_exactly_one_refusal_under_concurrency(
        self, isolated_engine: Engine
    ) -> None:
        factory = _session_factory(isolated_engine)

        for i in range(_ITERATIONS):
            ws = _bootstrap_two_owner_workspace(factory, slug_suffix=f"rm-{i:02d}")

            start = threading.Barrier(2)
            result = _new_race_result()

            # Thread A removes owner B (acting as owner A).
            # Thread B removes owner A (acting as owner B).
            # Each thread acts under its own owner's WorkspaceContext
            # so the tenant filter and audit writer both see a live
            # ctx on every call.
            t1 = threading.Thread(
                target=_remove_member_worker,
                kwargs={
                    "factory": factory,
                    "ws": ws,
                    "actor_id": ws.owner_a_id,
                    "target_user_id": ws.owner_b_id,
                    "start": start,
                    "result": result,
                },
            )
            t2 = threading.Thread(
                target=_remove_member_worker,
                kwargs={
                    "factory": factory,
                    "ws": ws,
                    "actor_id": ws.owner_b_id,
                    "target_user_id": ws.owner_a_id,
                    "start": start,
                    "result": result,
                },
            )
            t1.start()
            t2.start()
            t1.join(timeout=15)
            t2.join(timeout=15)

            try:
                # Invariant: the owners group never empties.
                assert _owners_member_count(factory, ws.workspace_id) >= 1, (
                    f"iteration {i}: owners group emptied — "
                    f"successes={result.successes}, errors={result.errors}"
                )
                # Exactly one delete succeeded; the other refused.
                assert len(result.successes) == 1, (
                    f"iteration {i}: expected 1 success, got "
                    f"{len(result.successes)} (successes={result.successes}, "
                    f"errors={result.errors})"
                )
                assert any(
                    isinstance(e, WouldOrphanOwnersGroup) for e in result.errors
                ), (
                    f"iteration {i}: expected a WouldOrphanOwnersGroup refusal, "
                    f"got errors={result.errors}"
                )
            finally:
                _scrub_workspace(factory, ws.workspace_id)


class TestRevokeSoloOwnerRace:
    """Two threads race :func:`revoke` on the sole owner's manager grant.

    Pre-state: workspace has 1 owner (A) with 1 manager grant. Both
    threads call ``revoke(A-manager-grant)`` simultaneously. Even
    without the lock both would refuse (``owner_count == 1`` → both
    threads raise), but the lock must not deadlock or spuriously
    permit one of them under contention. This guards against a
    regression where the cross-dialect UPDATE-to-lock primitive
    trips over itself when two transactions both reach it (e.g. a
    SQLite ``SQLITE_BUSY`` on upgrade, or a Postgres deadlock on
    ``FOR UPDATE`` acquisition).

    The shared locking primitive is the point cd-mb5n exists to
    enforce; this test demonstrates the revoke guard still behaves
    correctly under the same primitive.
    """

    def test_both_refuse_cleanly(self, isolated_engine: Engine) -> None:
        factory = _session_factory(isolated_engine)

        for i in range(_ITERATIONS):
            # Single-owner workspace — use the standard bootstrap
            # since we don't need owner B for this scenario.
            clock = FrozenClock(_PINNED)
            with factory() as session:
                owner = bootstrap_user(
                    session,
                    email=f"solo-{i:02d}@example.com",
                    display_name=f"Solo {i:02d}",
                    clock=clock,
                )
                workspace = bootstrap_workspace(
                    session,
                    slug=f"solo-race-{i:02d}-{new_ulid()[-6:].lower()}",
                    name=f"Solo {i:02d}",
                    owner_user_id=owner.id,
                    clock=clock,
                )
                ctx = _ctx_for(workspace.id, workspace.slug, owner.id)
                token = set_current(ctx)
                try:
                    a_manager_grants = [
                        g
                        for g in list_grants(_rg_repo(session), ctx, user_id=owner.id)
                        if g.grant_role == "manager"
                    ]
                    assert len(a_manager_grants) == 1
                    grant_id = a_manager_grants[0].id
                    session.commit()
                finally:
                    reset_current(token)

            start = threading.Barrier(2)
            result = _new_race_result()

            t1 = threading.Thread(
                target=_revoke_worker,
                kwargs={
                    "factory": factory,
                    "workspace_id": workspace.id,
                    "workspace_slug": workspace.slug,
                    "actor_id": owner.id,
                    "grant_id": grant_id,
                    "start": start,
                    "result": result,
                },
            )
            t2 = threading.Thread(
                target=_revoke_worker,
                kwargs={
                    "factory": factory,
                    "workspace_id": workspace.id,
                    "workspace_slug": workspace.slug,
                    "actor_id": owner.id,
                    "grant_id": grant_id,
                    "start": start,
                    "result": result,
                },
            )
            t1.start()
            t2.start()
            t1.join(timeout=15)
            t2.join(timeout=15)

            try:
                # Invariant: no successes — the sole owner's grant
                # is protected by the serial guard, and the lock must
                # not spuriously flip that to success.
                assert result.successes == [], (
                    f"iteration {i}: sole-owner grant was revoked "
                    f"(successes={result.successes}, errors={result.errors})"
                )
                # Both threads refused with the typed exception.
                assert len(result.errors) == 2, (
                    f"iteration {i}: expected 2 refusals, got "
                    f"{len(result.errors)} (errors={result.errors})"
                )
                assert all(
                    isinstance(e, LastOwnerGrantProtected) for e in result.errors
                ), (
                    f"iteration {i}: got non-LastOwnerGrantProtected "
                    f"errors ({result.errors})"
                )
            finally:
                _scrub_workspace(factory, workspace.id)


class TestRevokeManagerGrantRace:
    """Two threads race :func:`revoke` on different owners' manager grants.

    Pre-state: ``owners@<ws>`` has members A and B; each holds one
    ``manager`` grant on the workspace. Two threads simultaneously
    call ``revoke(A-manager-grant)`` and ``revoke(B-manager-grant)``.

    Without the broader §02 invariant — "owners has at least one
    member who carries a live manager grant" — both threads serialise
    on the cd-mb5n lock, both observe ``owners_member_count == 2``
    (membership doesn't change when only a grant is deleted), both
    proceed, and the workspace is left with 2 owners-group members
    and **0** manager grants. Nobody can administer it.

    With the cd-nj8m fix in place, the count under the lock counts
    owners who would *still* hold a manager grant after the pending
    delete. The first revoke takes the count from 2 → 1, the second
    re-reads 1 → 0 and refuses with :class:`LastOwnerGrantProtected`.

    This is the exact scenario cd-nj8m exists to fix.
    """

    def test_exactly_one_revoke_succeeds_under_concurrency(
        self, isolated_engine: Engine
    ) -> None:
        factory = _session_factory(isolated_engine)

        for i in range(_ITERATIONS):
            ws = _bootstrap_two_owner_workspace(factory, slug_suffix=f"mgr-{i:02d}")

            start = threading.Barrier(2)
            result = _new_race_result()

            # Thread A revokes owner B's manager grant (acting as A).
            # Thread B revokes owner A's manager grant (acting as B).
            # Each worker holds its own owner's :class:`WorkspaceContext`
            # so the audit row's actor field reflects the human who
            # initiated the revoke, not a shared bystander.
            t1 = threading.Thread(
                target=_revoke_worker,
                kwargs={
                    "factory": factory,
                    "workspace_id": ws.workspace_id,
                    "workspace_slug": ws.workspace_slug,
                    "actor_id": ws.owner_a_id,
                    "grant_id": ws.owner_b_grant_id,
                    "start": start,
                    "result": result,
                },
            )
            t2 = threading.Thread(
                target=_revoke_worker,
                kwargs={
                    "factory": factory,
                    "workspace_id": ws.workspace_id,
                    "workspace_slug": ws.workspace_slug,
                    "actor_id": ws.owner_b_id,
                    "grant_id": ws.owner_a_grant_id,
                    "start": start,
                    "result": result,
                },
            )
            t1.start()
            t2.start()
            t1.join(timeout=15)
            t2.join(timeout=15)

            try:
                # Invariant (§02 admin-reach): the workspace always has
                # at least one owners-group member holding a manager
                # grant after the race settles.
                assert (
                    _owners_with_manager_grant_count(factory, ws.workspace_id) >= 1
                ), (
                    f"iteration {i}: workspace decapitated — "
                    f"successes={result.successes}, errors={result.errors}"
                )
                # Exactly one revoke succeeded; the other refused.
                assert len(result.successes) == 1, (
                    f"iteration {i}: expected 1 success, got "
                    f"{len(result.successes)} (successes={result.successes}, "
                    f"errors={result.errors})"
                )
                assert any(
                    isinstance(e, LastOwnerGrantProtected) for e in result.errors
                ), (
                    f"iteration {i}: expected a LastOwnerGrantProtected refusal, "
                    f"got errors={result.errors}"
                )
                # Membership is untouched — only one of the two
                # ``manager`` grants was deleted.
                assert _owners_member_count(factory, ws.workspace_id) == 2, (
                    f"iteration {i}: owners-group membership unexpectedly "
                    f"changed (count="
                    f"{_owners_member_count(factory, ws.workspace_id)})"
                )
            finally:
                _scrub_workspace(factory, ws.workspace_id)


class TestCrossPathRace:
    """Cross-path race: ``revoke`` x ``remove_member`` on a 2-owner workspace.

    Pre-state: ``owners@<ws>`` has members A and B; each holds one
    ``manager`` grant on the workspace. Two threads run concurrently:

    * Thread A: ``revoke(A-manager-grant)``.
    * Thread B: ``remove_member(B from owners)``.

    Without the cross-path fix:

    * Thread A acquires the lock first, sees admin-reach == 2 (A and
      B are both manager-holding owners), counts excluding A's grant
      → 1 ≥ 1, proceeds, deletes A's grant. Commits.
    * Thread B acquires the lock, sees roster == 2 (B is still in
      owners) → passes the roster check; the historical
      ``remove_member`` did NOT consult the admin-reach helper, so
      it proceeded to drop B from owners.
    * Final state: A is in ``owners@<ws>`` without a manager grant,
      B holds a manager grant but is no longer in ``owners``. The
      workspace is administratively decapitated (admin reach == 0)
      even though both invariants looked safe to each thread in
      isolation.

    With cd-j5pu in place, the ``remove_member`` guard now also
    consults :func:`count_owner_members_with_manager_grant_locked`
    via ``exclude_user_id=B``. Whichever thread loses the shared
    lock race re-reads the post-write state and refuses with the
    appropriate typed exception, so exactly one operation succeeds
    and the workspace stays administrable.
    """

    def test_exactly_one_succeeds_under_concurrency(
        self, isolated_engine: Engine
    ) -> None:
        factory = _session_factory(isolated_engine)

        for i in range(_ITERATIONS):
            ws = _bootstrap_two_owner_workspace(factory, slug_suffix=f"cx-{i:02d}")

            start = threading.Barrier(2)
            result = _new_race_result()

            # Thread A revokes A's own manager grant (acting as A).
            # Thread B removes B from ``owners@<ws>`` (acting as A so
            # the actor in B's :class:`WorkspaceContext` is a still-
            # valid manager-grant-holding owner regardless of which
            # thread wins the race; using B as the actor would risk
            # auditing a row whose actor was decapitated mid-flight).
            t1 = threading.Thread(
                target=_revoke_worker,
                kwargs={
                    "factory": factory,
                    "workspace_id": ws.workspace_id,
                    "workspace_slug": ws.workspace_slug,
                    "actor_id": ws.owner_a_id,
                    "grant_id": ws.owner_a_grant_id,
                    "start": start,
                    "result": result,
                },
            )
            t2 = threading.Thread(
                target=_remove_member_worker,
                kwargs={
                    "factory": factory,
                    "ws": ws,
                    "actor_id": ws.owner_a_id,
                    "target_user_id": ws.owner_b_id,
                    "start": start,
                    "result": result,
                },
            )
            t1.start()
            t2.start()
            t1.join(timeout=15)
            t2.join(timeout=15)

            try:
                # Invariant (§02 admin-reach): the workspace always
                # has at least one owners-group member holding a
                # manager grant after the race settles.
                assert (
                    _owners_with_manager_grant_count(factory, ws.workspace_id) >= 1
                ), (
                    f"iteration {i}: workspace decapitated — "
                    f"successes={result.successes}, errors={result.errors}"
                )
                # Invariant (§02 roster): owners stays populated.
                assert _owners_member_count(factory, ws.workspace_id) >= 1, (
                    f"iteration {i}: owners group emptied — "
                    f"successes={result.successes}, errors={result.errors}"
                )
                # Exactly one operation succeeded; the other refused.
                assert len(result.successes) == 1, (
                    f"iteration {i}: expected 1 success, got "
                    f"{len(result.successes)} (successes={result.successes}, "
                    f"errors={result.errors})"
                )
                # Whichever path lost the lock race surfaced the
                # administrative-reach guard — :class:`LastOwnerGrantProtected`
                # is raised by both ``revoke`` (cd-nj8m) and
                # ``remove_member`` (cd-j5pu) when the post-write
                # state would have admin-reach == 0.
                assert any(
                    isinstance(e, LastOwnerGrantProtected) for e in result.errors
                ), (
                    f"iteration {i}: expected a LastOwnerGrantProtected refusal, "
                    f"got errors={result.errors}"
                )
            finally:
                _scrub_workspace(factory, ws.workspace_id)


class TestRevokeRemovedOwnerRace:
    """Race ``revoke(A-grant)`` with ``remove_member(A from owners)``.

    Pre-state: ``owners@<ws>`` has members A and B; both hold a live
    ``manager`` grant. Two threads run concurrently:

    * Thread 1: ``revoke(A-manager-grant)``.
    * Thread 2: ``remove_member(A from owners)``.

    If the revoke path decides A is an owner before taking the shared
    owners-group lock, then the membership-removal path can commit
    first and leave only B in ``owners``. A stale revoke guard then
    sees the post-removal count as one and can raise a spurious
    :class:`LastOwnerGrantProtected`, even though B remains as a
    manager-holding owner and the revoke is safe. The revoke path must
    read A's owners-membership under the same lock as the count.
    """

    def test_no_spurious_last_owner_refusal_when_target_left_owners(
        self, isolated_engine: Engine
    ) -> None:
        factory = _session_factory(isolated_engine)

        for i in range(_ITERATIONS):
            ws = _bootstrap_two_owner_workspace(factory, slug_suffix=f"same-{i:02d}")

            start = threading.Barrier(2)
            result = _new_race_result()

            t1 = threading.Thread(
                target=_revoke_worker,
                kwargs={
                    "factory": factory,
                    "workspace_id": ws.workspace_id,
                    "workspace_slug": ws.workspace_slug,
                    "actor_id": ws.owner_b_id,
                    "grant_id": ws.owner_a_grant_id,
                    "start": start,
                    "result": result,
                },
            )
            t2 = threading.Thread(
                target=_remove_member_worker,
                kwargs={
                    "factory": factory,
                    "ws": ws,
                    "actor_id": ws.owner_b_id,
                    "target_user_id": ws.owner_a_id,
                    "start": start,
                    "result": result,
                },
            )
            t1.start()
            t2.start()
            t1.join(timeout=15)
            t2.join(timeout=15)

            try:
                assert not any(
                    isinstance(e, LastOwnerGrantProtected) for e in result.errors
                ), (
                    f"iteration {i}: revoke/remove same-owner race raised a "
                    f"spurious LastOwnerGrantProtected "
                    f"(successes={result.successes}, errors={result.errors})"
                )
                assert result.errors == [], (
                    f"iteration {i}: expected both operations to succeed, "
                    f"got successes={result.successes}, errors={result.errors}"
                )
                assert len(result.successes) == 2, (
                    f"iteration {i}: expected revoke and remove to succeed, "
                    f"got successes={result.successes}, errors={result.errors}"
                )
                assert _owners_member_count(factory, ws.workspace_id) == 1, (
                    f"iteration {i}: expected B to remain as the sole owner, "
                    f"got count={_owners_member_count(factory, ws.workspace_id)}"
                )
                assert (
                    _owners_with_manager_grant_count(factory, ws.workspace_id) == 1
                ), (
                    f"iteration {i}: expected B to remain as manager-holding owner, "
                    f"successes={result.successes}, errors={result.errors}"
                )
            finally:
                _scrub_workspace(factory, ws.workspace_id)

    def test_revoke_snapshot_sees_target_removed_before_lock(
        self, isolated_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        factory = _session_factory(isolated_engine)
        ws = _bootstrap_two_owner_workspace(factory, slug_suffix="same-forced")
        result = _new_race_result()
        revoke_reached_snapshot = threading.Event()
        removal_done = threading.Event()

        original_status = role_grants_module.owner_member_manager_grant_status_locked

        def delayed_status(
            session: Session,
            *,
            workspace_id: str,
            user_id: str,
            exclude_grant_id: str | None = None,
        ) -> OwnerMemberManagerGrantStatus:
            revoke_reached_snapshot.set()
            if not removal_done.wait(timeout=15):
                raise AssertionError("timed out waiting for target owner removal")
            return original_status(
                session,
                workspace_id=workspace_id,
                user_id=user_id,
                exclude_grant_id=exclude_grant_id,
            )

        monkeypatch.setattr(
            role_grants_module,
            "owner_member_manager_grant_status_locked",
            delayed_status,
        )

        def remove_target_after_revoke_reaches_snapshot() -> None:
            try:
                if not revoke_reached_snapshot.wait(timeout=15):
                    raise AssertionError("timed out waiting for revoke snapshot hook")
                with factory() as session:
                    ctx = _ctx_for(ws.workspace_id, ws.workspace_slug, ws.owner_b_id)
                    token = set_current(ctx)
                    try:
                        remove_member(
                            _pg_repo(session),
                            ctx,
                            group_id=ws.owners_group_id,
                            user_id=ws.owner_a_id,
                            clock=FrozenClock(_PINNED),
                        )
                        session.commit()
                        with result.lock:
                            result.successes.append(f"remove:{ws.owner_a_id}")
                    finally:
                        reset_current(token)
            except Exception as exc:  # pragma: no cover - harness
                with result.lock:
                    result.errors.append(exc)
            finally:
                removal_done.set()

        t1 = threading.Thread(
            target=_revoke_worker,
            kwargs={
                "factory": factory,
                "workspace_id": ws.workspace_id,
                "workspace_slug": ws.workspace_slug,
                "actor_id": ws.owner_b_id,
                "grant_id": ws.owner_a_grant_id,
                "start": threading.Barrier(1),
                "result": result,
            },
        )
        t2 = threading.Thread(target=remove_target_after_revoke_reaches_snapshot)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        try:
            assert not t1.is_alive(), "revoke worker deadlocked"
            assert not t2.is_alive(), "remove-member worker deadlocked"
            assert not any(
                isinstance(e, LastOwnerGrantProtected) for e in result.errors
            ), (
                "revoke used a stale pre-lock owners-membership decision "
                f"(successes={result.successes}, errors={result.errors})"
            )
            assert result.errors == [], (
                f"expected forced removal-before-lock sequence to succeed, "
                f"got successes={result.successes}, errors={result.errors}"
            )
            assert len(result.successes) == 2
            assert _owners_member_count(factory, ws.workspace_id) == 1
            assert _owners_with_manager_grant_count(factory, ws.workspace_id) == 1
        finally:
            _scrub_workspace(factory, ws.workspace_id)
