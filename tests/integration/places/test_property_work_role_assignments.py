"""Integration tests for the property-work-role-assignment domain service.

Exercises :mod:`app.domain.places.property_work_role_assignments`
end-to-end against a real DB with the tenant filter installed so every
function walks the same code path the FastAPI route handler uses.

Each test:

* Bootstraps a user + workspace via
  :func:`tests.factories.identity.bootstrap_workspace`.
* Seeds the prerequisite chain: ``property`` + ``property_workspace``
  junction, ``work_role``, ``user_work_role`` (the parent of the
  assignment).
* Sets a :class:`WorkspaceContext` so the ORM filter and the audit
  writer both see a live context.
* Calls the domain service and asserts the resulting rows + audit
  entries.

See ``docs/specs/05-employees-and-roles.md`` §"Property work role
assignment", ``docs/specs/02-domain-model.md``
§"property_work_role_assignment".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.payroll.models import PayRule
from app.adapters.db.places.models import (
    Property,
    PropertyWorkRoleAssignment,
    PropertyWorkspace,
)
from app.adapters.db.workspace.models import UserWorkRole, WorkRole
from app.domain.places.property_work_role_assignments import (
    PropertyWorkRoleAssignmentCreate,
    PropertyWorkRoleAssignmentInvariantViolated,
    PropertyWorkRoleAssignmentNotFound,
    PropertyWorkRoleAssignmentUpdate,
    create_property_work_role_assignment,
    delete_property_work_role_assignment,
    get_property_work_role_assignment,
    list_property_work_role_assignments,
    update_property_work_role_assignment,
)
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


_SLUG_COUNTER = 0


def _next_slug() -> str:
    """Return a fresh, validator-compliant workspace slug for the test."""
    global _SLUG_COUNTER
    _SLUG_COUNTER += 1
    return f"pwra-{_SLUG_COUNTER:05d}"


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
    """Re-register tables this test module depends on.

    Mirrors :mod:`tests.integration.places.test_property_crud` — a
    sibling unit test resets the process-wide registry, so registering
    again here keeps the ORM filter active even when test order
    interleaves.
    """
    registry.register("property_workspace")
    registry.register("property_work_role_assignment")
    registry.register("user_work_role")
    registry.register("work_role")
    registry.register("audit_log")


def _ctx_for(workspace_id: str, workspace_slug: str, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLP",
    )


def _seed_property(
    session: Session, *, workspace_id: str, name: str = "Villa Sud"
) -> str:
    """Seed a ``property`` row + ``property_workspace`` junction."""
    prop = Property(
        id=new_ulid(),
        name=name,
        kind="vacation",
        address=f"{name} address",
        address_json={"country": "FR"},
        country="FR",
        locale=None,
        default_currency=None,
        timezone="Europe/Paris",
        lat=None,
        lon=None,
        client_org_id=None,
        owner_user_id=None,
        tags_json=[],
        welcome_defaults_json={},
        property_notes_md="",
        created_at=_PINNED,
        updated_at=_PINNED,
        deleted_at=None,
    )
    session.add(prop)
    session.add(
        PropertyWorkspace(
            property_id=prop.id,
            workspace_id=workspace_id,
            label=name,
            membership_role="owner_workspace",
            created_at=_PINNED,
        )
    )
    session.flush()
    return prop.id


def _seed_work_role(session: Session, *, workspace_id: str, key: str = "maid") -> str:
    role = WorkRole(
        id=new_ulid(),
        workspace_id=workspace_id,
        key=key,
        name=key.title(),
        description_md="",
        default_settings_json={},
        icon_name="",
        created_at=_PINNED,
        deleted_at=None,
    )
    session.add(role)
    session.flush()
    return role.id


def _seed_pay_rule(session: Session, *, user_id: str, workspace_id: str) -> str:
    """Seed a minimal :class:`PayRule` row for FK-bound tests."""
    rule = PayRule(
        id=new_ulid(),
        workspace_id=workspace_id,
        user_id=user_id,
        currency="EUR",
        base_cents_per_hour=1500,
        effective_from=_PINNED,
        created_at=_PINNED,
    )
    session.add(rule)
    session.flush()
    return rule.id


def _seed_user_work_role(
    session: Session,
    *,
    user_id: str,
    workspace_id: str,
    work_role_id: str,
) -> str:
    row = UserWorkRole(
        id=new_ulid(),
        user_id=user_id,
        workspace_id=workspace_id,
        work_role_id=work_role_id,
        started_on=date(2026, 4, 1),
        ended_on=None,
        pay_rule_id=None,
        created_at=_PINNED,
        deleted_at=None,
    )
    session.add(row)
    session.flush()
    return row.id


@pytest.fixture
def env(
    db_session: Session,
) -> Iterator[tuple[Session, WorkspaceContext, str, str]]:
    """Yield ``(session, ctx, prop_id, user_work_role_id)`` for one workspace.

    Bootstraps the full chain (workspace → property + junction →
    work_role → user_work_role) so each test can call the service
    without re-seeding. Builds on the parent conftest's ``db_session``
    fixture — rolled back on teardown.
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
        prop_id = _seed_property(db_session, workspace_id=ws.id)
        role_id = _seed_work_role(db_session, workspace_id=ws.id)
        uwr_id = _seed_user_work_role(
            db_session,
            user_id=user.id,
            workspace_id=ws.id,
            work_role_id=role_id,
        )
        yield db_session, ctx, prop_id, uwr_id
    finally:
        reset_current(token)


# ---------------------------------------------------------------------------
# Round-trip CRUD
# ---------------------------------------------------------------------------


class TestCreate:
    """Create persists row + audit; invariants block bad payloads."""

    def test_round_trip_create(
        self, env: tuple[Session, WorkspaceContext, str, str]
    ) -> None:
        session, ctx, prop_id, uwr_id = env
        clock = FrozenClock(_PINNED)

        view = create_property_work_role_assignment(
            session,
            ctx,
            body=PropertyWorkRoleAssignmentCreate(
                user_work_role_id=uwr_id,
                property_id=prop_id,
            ),
            clock=clock,
        )

        # Row landed under the caller's workspace.
        row = session.get(PropertyWorkRoleAssignment, view.id)
        assert row is not None
        assert row.workspace_id == ctx.workspace_id
        assert row.user_work_role_id == uwr_id
        assert row.property_id == prop_id
        assert row.deleted_at is None

        # Audit row lands in the same transaction.
        audits = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == view.id)
        ).all()
        assert len(audits) == 1
        assert audits[0].action == "property_work_role_assignment.created"
        assert audits[0].entity_kind == "property_work_role_assignment"

    def test_property_not_in_workspace_raises(
        self, env: tuple[Session, WorkspaceContext, str, str]
    ) -> None:
        """A property with no ``property_workspace`` link is invisible."""
        session, ctx, _prop_id, uwr_id = env
        # Seed a bare property — no junction row binds it to ``ctx``.
        orphan = Property(
            id=new_ulid(),
            name="Orphan",
            kind="vacation",
            address="—",
            address_json={"country": "FR"},
            country="FR",
            locale=None,
            default_currency=None,
            timezone="Europe/Paris",
            lat=None,
            lon=None,
            client_org_id=None,
            owner_user_id=None,
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
            deleted_at=None,
        )
        session.add(orphan)
        session.flush()

        with pytest.raises(PropertyWorkRoleAssignmentInvariantViolated) as exc_info:
            create_property_work_role_assignment(
                session,
                ctx,
                body=PropertyWorkRoleAssignmentCreate(
                    user_work_role_id=uwr_id,
                    property_id=orphan.id,
                ),
                clock=FrozenClock(_PINNED),
            )
        assert "not linked" in str(exc_info.value)

    def test_user_work_role_not_in_workspace_raises(
        self, env: tuple[Session, WorkspaceContext, str, str]
    ) -> None:
        session, ctx, prop_id, _uwr_id = env

        with pytest.raises(PropertyWorkRoleAssignmentInvariantViolated) as exc_info:
            create_property_work_role_assignment(
                session,
                ctx,
                body=PropertyWorkRoleAssignmentCreate(
                    user_work_role_id="01HWUNKNOWN000000000000000",
                    property_id=prop_id,
                ),
                clock=FrozenClock(_PINNED),
            )
        assert "user_work_role" in str(exc_info.value)

    def test_create_with_unknown_pay_rule_raises_invariant(
        self, env: tuple[Session, WorkspaceContext, str, str]
    ) -> None:
        """``property_pay_rule_id`` pointing at no pay_rule → invariant.

        The FK constraint fires at flush time; the service collapses
        the IntegrityError into
        :class:`PropertyWorkRoleAssignmentInvariantViolated` with a
        non-duplicate message (HTTP layer maps it to 422, not 409).
        """
        session, ctx, prop_id, uwr_id = env

        with pytest.raises(PropertyWorkRoleAssignmentInvariantViolated) as exc_info:
            create_property_work_role_assignment(
                session,
                ctx,
                body=PropertyWorkRoleAssignmentCreate(
                    user_work_role_id=uwr_id,
                    property_id=prop_id,
                    property_pay_rule_id="01HWPAYRULEMISSING0000000",
                ),
                clock=FrozenClock(_PINNED),
            )
        # Must NOT carry the duplicate substring — that is reserved for
        # the partial UNIQUE flavour and the HTTP layer keys on it for
        # the 409 / 422 split.
        assert "already exists" not in str(exc_info.value)
        assert "rejected by the database" in str(exc_info.value)

    def test_duplicate_active_collapses_to_invariant(
        self, env: tuple[Session, WorkspaceContext, str, str]
    ) -> None:
        """Re-creating a live ``(role, property)`` pair raises invariant."""
        session, ctx, prop_id, uwr_id = env
        clock = FrozenClock(_PINNED)

        create_property_work_role_assignment(
            session,
            ctx,
            body=PropertyWorkRoleAssignmentCreate(
                user_work_role_id=uwr_id,
                property_id=prop_id,
            ),
            clock=clock,
        )

        with pytest.raises(PropertyWorkRoleAssignmentInvariantViolated) as exc_info:
            create_property_work_role_assignment(
                session,
                ctx,
                body=PropertyWorkRoleAssignmentCreate(
                    user_work_role_id=uwr_id,
                    property_id=prop_id,
                ),
                clock=clock,
            )
        # The HTTP layer keys on this substring to map → 409.
        assert "already exists" in str(exc_info.value)


class TestUpdate:
    """Partial update touches only sent fields + writes one audit row."""

    def test_round_trip_update(
        self, env: tuple[Session, WorkspaceContext, str, str]
    ) -> None:
        session, ctx, prop_id, uwr_id = env
        clock = FrozenClock(_PINNED)
        view = create_property_work_role_assignment(
            session,
            ctx,
            body=PropertyWorkRoleAssignmentCreate(
                user_work_role_id=uwr_id,
                property_id=prop_id,
                schedule_ruleset_id="01HWRULESET00000000000000",
            ),
            clock=clock,
        )

        # ``property_pay_rule_id`` is FK-bound — seed a real pay_rule
        # row so the round-trip exercises the actual DB constraint.
        pay_rule_id = _seed_pay_rule(
            session, user_id=ctx.actor_id, workspace_id=ctx.workspace_id
        )
        later = FrozenClock(_PINNED.replace(hour=13))
        updated = update_property_work_role_assignment(
            session,
            ctx,
            assignment_id=view.id,
            body=PropertyWorkRoleAssignmentUpdate(
                property_pay_rule_id=pay_rule_id,
            ),
            clock=later,
        )
        assert updated.property_pay_rule_id == pay_rule_id
        assert updated.schedule_ruleset_id == "01HWRULESET00000000000000"

        # An updated audit row joins the original create row.
        audits = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == view.id)
        ).all()
        actions = sorted(a.action for a in audits)
        assert actions == [
            "property_work_role_assignment.created",
            "property_work_role_assignment.updated",
        ]

    def test_zero_delta_skips_audit(
        self, env: tuple[Session, WorkspaceContext, str, str]
    ) -> None:
        """A patch that does not change any field skips the audit write."""
        session, ctx, prop_id, uwr_id = env
        clock = FrozenClock(_PINNED)
        view = create_property_work_role_assignment(
            session,
            ctx,
            body=PropertyWorkRoleAssignmentCreate(
                user_work_role_id=uwr_id,
                property_id=prop_id,
                schedule_ruleset_id="01HWRULESET00000000000000",
            ),
            clock=clock,
        )

        update_property_work_role_assignment(
            session,
            ctx,
            assignment_id=view.id,
            body=PropertyWorkRoleAssignmentUpdate(
                schedule_ruleset_id="01HWRULESET00000000000000",
            ),
            clock=clock,
        )

        audits = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == view.id)
        ).all()
        assert [a.action for a in audits] == ["property_work_role_assignment.created"]

    def test_update_with_unknown_pay_rule_raises_invariant(
        self, env: tuple[Session, WorkspaceContext, str, str]
    ) -> None:
        """PATCH with a non-existent ``property_pay_rule_id`` → invariant.

        The FK fires at flush; the service rolls back and raises
        :class:`PropertyWorkRoleAssignmentInvariantViolated` so the
        HTTP layer maps it to 422 (not a 500 leak).
        """
        session, ctx, prop_id, uwr_id = env
        clock = FrozenClock(_PINNED)
        view = create_property_work_role_assignment(
            session,
            ctx,
            body=PropertyWorkRoleAssignmentCreate(
                user_work_role_id=uwr_id,
                property_id=prop_id,
            ),
            clock=clock,
        )

        with pytest.raises(PropertyWorkRoleAssignmentInvariantViolated) as exc_info:
            update_property_work_role_assignment(
                session,
                ctx,
                assignment_id=view.id,
                body=PropertyWorkRoleAssignmentUpdate(
                    property_pay_rule_id="01HWPAYRULEMISSING0000000",
                ),
                clock=clock,
            )
        assert "rejected by the database" in str(exc_info.value)

    def test_update_unknown_raises_not_found(
        self, env: tuple[Session, WorkspaceContext, str, str]
    ) -> None:
        session, ctx, _prop_id, _uwr_id = env
        with pytest.raises(PropertyWorkRoleAssignmentNotFound):
            update_property_work_role_assignment(
                session,
                ctx,
                assignment_id="01HWUNKNOWN000000000000000",
                body=PropertyWorkRoleAssignmentUpdate(
                    schedule_ruleset_id="01HWRULESET00000000000000",
                ),
                clock=FrozenClock(_PINNED),
            )


class TestSoftDelete:
    """Soft-delete stamps ``deleted_at`` and excludes from default lookup."""

    def test_round_trip_soft_delete(
        self, env: tuple[Session, WorkspaceContext, str, str]
    ) -> None:
        session, ctx, prop_id, uwr_id = env
        clock = FrozenClock(_PINNED)
        view = create_property_work_role_assignment(
            session,
            ctx,
            body=PropertyWorkRoleAssignmentCreate(
                user_work_role_id=uwr_id,
                property_id=prop_id,
            ),
            clock=clock,
        )

        later = FrozenClock(_PINNED.replace(hour=14))
        delete_property_work_role_assignment(
            session, ctx, assignment_id=view.id, clock=later
        )

        # Hidden from the default get + list paths.
        with pytest.raises(PropertyWorkRoleAssignmentNotFound):
            get_property_work_role_assignment(session, ctx, assignment_id=view.id)
        assert list_property_work_role_assignments(session, ctx, limit=10) == []

        # Re-pin succeeds — partial UNIQUE excludes tombstones.
        repin = create_property_work_role_assignment(
            session,
            ctx,
            body=PropertyWorkRoleAssignmentCreate(
                user_work_role_id=uwr_id,
                property_id=prop_id,
            ),
            clock=later,
        )
        assert repin.id != view.id

        # Audit chain has create + delete + create on the original id /
        # new id respectively.
        audits = session.scalars(
            select(AuditLog).where(
                AuditLog.action == "property_work_role_assignment.deleted"
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].entity_id == view.id


class TestListFilters:
    """Filtering narrows the listing to a property / user_work_role."""

    def test_filter_by_property(
        self, env: tuple[Session, WorkspaceContext, str, str]
    ) -> None:
        session, ctx, prop_id, uwr_id = env
        clock = FrozenClock(_PINNED)
        prop_b = _seed_property(session, workspace_id=ctx.workspace_id, name="Apt 3B")

        first = create_property_work_role_assignment(
            session,
            ctx,
            body=PropertyWorkRoleAssignmentCreate(
                user_work_role_id=uwr_id,
                property_id=prop_id,
            ),
            clock=clock,
        )
        second = create_property_work_role_assignment(
            session,
            ctx,
            body=PropertyWorkRoleAssignmentCreate(
                user_work_role_id=uwr_id,
                property_id=prop_b,
            ),
            clock=clock,
        )

        listed_a = list_property_work_role_assignments(
            session, ctx, limit=10, property_id=prop_id
        )
        assert [v.id for v in listed_a] == [first.id]

        listed_b = list_property_work_role_assignments(
            session, ctx, limit=10, property_id=prop_b
        )
        assert [v.id for v in listed_b] == [second.id]

    def test_filter_by_user_work_role(
        self, env: tuple[Session, WorkspaceContext, str, str]
    ) -> None:
        session, ctx, prop_id, uwr_id = env
        clock = FrozenClock(_PINNED)
        # Seed a second work_role + user_work_role so we have two distinct
        # parents pointing at the same property.
        role_b = _seed_work_role(session, workspace_id=ctx.workspace_id, key="cook")
        uwr_b = _seed_user_work_role(
            session,
            user_id=ctx.actor_id,
            workspace_id=ctx.workspace_id,
            work_role_id=role_b,
        )

        first = create_property_work_role_assignment(
            session,
            ctx,
            body=PropertyWorkRoleAssignmentCreate(
                user_work_role_id=uwr_id,
                property_id=prop_id,
            ),
            clock=clock,
        )
        second = create_property_work_role_assignment(
            session,
            ctx,
            body=PropertyWorkRoleAssignmentCreate(
                user_work_role_id=uwr_b,
                property_id=prop_id,
            ),
            clock=clock,
        )

        listed_a = list_property_work_role_assignments(
            session, ctx, limit=10, user_work_role_id=uwr_id
        )
        assert [v.id for v in listed_a] == [first.id]
        listed_b = list_property_work_role_assignments(
            session, ctx, limit=10, user_work_role_id=uwr_b
        )
        assert [v.id for v in listed_b] == [second.id]


class TestCrossWorkspace:
    """A row in workspace A is invisible from workspace B."""

    def test_cross_workspace_invisible(
        self,
        env: tuple[Session, WorkspaceContext, str, str],
        db_session: Session,
    ) -> None:
        session, ctx_a, prop_id, uwr_id = env
        clock = FrozenClock(_PINNED)
        view = create_property_work_role_assignment(
            session,
            ctx_a,
            body=PropertyWorkRoleAssignmentCreate(
                user_work_role_id=uwr_id,
                property_id=prop_id,
            ),
            clock=clock,
        )

        # Stand up a second workspace in the same DB session.
        slug_b = _next_slug()
        user_b = bootstrap_user(
            session,
            email=f"{slug_b}@example.com",
            display_name=f"User {slug_b}",
            clock=clock,
        )
        ws_b = bootstrap_workspace(
            session,
            slug=slug_b,
            name=f"WS {slug_b}",
            owner_user_id=user_b.id,
            clock=clock,
        )
        ctx_b = _ctx_for(ws_b.id, ws_b.slug, user_b.id)

        token = set_current(ctx_b)
        try:
            assert list_property_work_role_assignments(session, ctx_b, limit=10) == []
            with pytest.raises(PropertyWorkRoleAssignmentNotFound):
                get_property_work_role_assignment(session, ctx_b, assignment_id=view.id)
            with pytest.raises(PropertyWorkRoleAssignmentNotFound):
                update_property_work_role_assignment(
                    session,
                    ctx_b,
                    assignment_id=view.id,
                    body=PropertyWorkRoleAssignmentUpdate(
                        # Soft-ref column — short-circuits on NotFound
                        # before any FK validation. The point of this
                        # branch is the tenancy gate.
                        schedule_ruleset_id="01HWRULESET00000000000000",
                    ),
                    clock=clock,
                )
            with pytest.raises(PropertyWorkRoleAssignmentNotFound):
                delete_property_work_role_assignment(
                    session,
                    ctx_b,
                    assignment_id=view.id,
                    clock=clock,
                )
        finally:
            reset_current(token)
