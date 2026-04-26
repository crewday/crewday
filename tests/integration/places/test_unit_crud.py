"""Integration tests for :mod:`app.domain.places.unit_service` (cd-y62).

Exercises the full create / update / soft-delete / list round-trip
against a real DB with the tenant filter installed so every domain
function walks the same code paths it will when called from a
FastAPI route handler.

Each test:

* Bootstraps a user + workspace via
  :func:`tests.factories.identity.bootstrap_workspace`.
* Sets a :class:`WorkspaceContext` for that workspace so the ORM
  filter and the audit writer both see a live context.
* Calls :func:`create_property` to seed the property + default unit,
  then exercises the unit service against the resulting state.

See ``docs/specs/04-properties-and-stays.md`` §"Unit" /
§"Invariants".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.places.models import Property, Unit
from app.domain.places.property_service import (
    PropertyCreate,
    PropertyView,
    create_property,
)
from app.domain.places.unit_service import (
    LastUnitProtected,
    UnitCreate,
    UnitNameTaken,
    UnitNotFound,
    UnitUpdate,
    create_unit,
    get_unit,
    list_units,
    soft_delete_unit,
    update_unit,
)
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


_SLUG_COUNTER = 0


def _next_slug() -> str:
    """Return a fresh, validator-compliant workspace slug for the test."""
    global _SLUG_COUNTER
    _SLUG_COUNTER += 1
    return f"un-crud-{_SLUG_COUNTER:05d}"


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
    """Re-register workspace-scoped tables this test module depends on.

    A sibling unit test resets the process-wide registry in its
    autouse fixture; mirror :mod:`test_property_crud` and re-register
    here so the filter is honest under any execution order.
    """
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
        audit_correlation_id="01HWA00000000000000000CRLU",
    )


@pytest.fixture
def env(
    db_session: Session,
) -> Iterator[tuple[Session, WorkspaceContext]]:
    """Yield a ``(session, ctx)`` pair bound to a fresh workspace."""
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


def _make_property(
    session: Session,
    ctx: WorkspaceContext,
    *,
    name: str = "Villa Sud",
    clock: FrozenClock,
) -> PropertyView:
    body = PropertyCreate.model_validate(
        {
            "name": name,
            "kind": "vacation",
            "address": "12 Chemin des Oliviers, Antibes",
            "address_json": {
                "line1": "12 Chemin des Oliviers",
                "city": "Antibes",
                "country": "FR",
            },
            "country": "FR",
            "timezone": "Europe/Paris",
        }
    )
    return create_property(session, ctx, body=body, clock=clock)


# ---------------------------------------------------------------------------
# Auto-create default unit on property bootstrap
# ---------------------------------------------------------------------------


class TestPropertyBootstrapAutoCreatesUnit:
    """Property create → exactly one default unit named after the property."""

    def test_default_unit_lands_with_property(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        prop = _make_property(session, ctx, name="Villa Sud", clock=clock)

        units = list_units(session, ctx, property_id=prop.id)
        assert len(units) == 1
        only = units[0]
        assert only.name == "Villa Sud"
        assert only.ordinal == 0
        assert only.property_id == prop.id

        # The DB row carries both ``name`` and the legacy ``label``
        # mirrored — adapters that still read ``label`` keep working.
        rows = session.scalars(select(Unit).where(Unit.property_id == prop.id)).all()
        assert len(rows) == 1
        assert rows[0].name == "Villa Sud"
        assert rows[0].label == "Villa Sud"

        # Audit row recorded for the unit create alongside the
        # property + property_workspace audits.
        unit_audits = session.scalars(
            select(AuditLog).where(
                AuditLog.entity_kind == "unit",
                AuditLog.entity_id == only.id,
            )
        ).all()
        assert len(unit_audits) == 1
        assert unit_audits[0].action == "create"


# ---------------------------------------------------------------------------
# create_unit
# ---------------------------------------------------------------------------


class TestCreateUnit:
    """``create_unit`` — duplicate-name + cross-workspace gates against a real DB."""

    def test_round_trip_create(self, env: tuple[Session, WorkspaceContext]) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        prop = _make_property(session, ctx, name="Apt 3", clock=clock)

        view = create_unit(
            session,
            ctx,
            property_id=prop.id,
            body=UnitCreate.model_validate(
                {
                    "name": "Room 1",
                    "ordinal": 1,
                    "default_checkin_time": "16:00",
                    "default_checkout_time": "10:00",
                    "max_guests": 2,
                }
            ),
            clock=clock,
        )

        row = session.get(Unit, view.id)
        assert row is not None
        assert row.name == "Room 1"
        assert row.ordinal == 1
        assert row.property_id == prop.id

        audits = session.scalars(
            select(AuditLog).where(
                AuditLog.entity_kind == "unit",
                AuditLog.entity_id == view.id,
                AuditLog.action == "create",
            )
        ).all()
        assert len(audits) == 1

    def test_duplicate_name_rejected(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        prop = _make_property(session, ctx, name="Villa Sud", clock=clock)

        # Bootstrap unit is "Villa Sud"; adding a sibling with the same
        # name must raise UnitNameTaken before the partial UNIQUE fires.
        with pytest.raises(UnitNameTaken):
            create_unit(
                session,
                ctx,
                property_id=prop.id,
                body=UnitCreate.model_validate({"name": "Villa Sud", "ordinal": 1}),
                clock=clock,
            )

    def test_create_cross_workspace_denied(
        self, env: tuple[Session, WorkspaceContext], db_session: Session
    ) -> None:
        session, ctx_a = env
        clock = FrozenClock(_PINNED)
        prop = _make_property(session, ctx_a, clock=clock)

        # Fresh workspace B in the same DB session.
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
            with pytest.raises(UnitNotFound):
                create_unit(
                    session,
                    ctx_b,
                    property_id=prop.id,
                    body=UnitCreate.model_validate({"name": "Hijack"}),
                    clock=clock,
                )
        finally:
            reset_current(token)

        # Property is unchanged from A's perspective: still one unit.
        token = set_current(ctx_a)
        try:
            assert len(list_units(session, ctx_a, property_id=prop.id)) == 1
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# update_unit
# ---------------------------------------------------------------------------


class TestUpdateUnit:
    """``update_unit`` — happy path, name collision, cross-workspace 404."""

    def test_round_trip_update(self, env: tuple[Session, WorkspaceContext]) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        prop = _make_property(session, ctx, name="Villa Sud", clock=clock)
        units = list_units(session, ctx, property_id=prop.id)
        target = units[0]

        later = FrozenClock(_PINNED.replace(hour=14))
        updated = update_unit(
            session,
            ctx,
            unit_id=target.id,
            body=UnitUpdate.model_validate(
                {"name": "Main suite", "ordinal": 0, "max_guests": 4}
            ),
            clock=later,
        )
        assert updated.name == "Main suite"
        assert updated.max_guests == 4

        row = session.get(Unit, target.id)
        assert row is not None
        assert row.name == "Main suite"
        assert row.label == "Main suite"  # legacy mirror in sync

    def test_update_cross_workspace_denied(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx_a = env
        clock = FrozenClock(_PINNED)
        prop = _make_property(session, ctx_a, clock=clock)
        units = list_units(session, ctx_a, property_id=prop.id)
        target = units[0]

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
            with pytest.raises(UnitNotFound):
                update_unit(
                    session,
                    ctx_b,
                    unit_id=target.id,
                    body=UnitUpdate.model_validate({"name": "Hijack"}),
                    clock=clock,
                )
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# soft_delete_unit
# ---------------------------------------------------------------------------


class TestSoftDeleteUnit:
    """``soft_delete_unit`` honours the §04 last-unit invariant."""

    def test_cannot_delete_last_unit(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        prop = _make_property(session, ctx, clock=clock)
        only = list_units(session, ctx, property_id=prop.id)[0]

        with pytest.raises(LastUnitProtected):
            soft_delete_unit(session, ctx, unit_id=only.id, clock=clock)

        # Row still live.
        row = session.get(Unit, only.id)
        assert row is not None
        assert row.deleted_at is None

    def test_can_delete_when_sibling_exists(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        prop = _make_property(session, ctx, clock=clock)
        sibling = create_unit(
            session,
            ctx,
            property_id=prop.id,
            body=UnitCreate.model_validate({"name": "Room 1", "ordinal": 1}),
            clock=clock,
        )

        deleted = soft_delete_unit(session, ctx, unit_id=sibling.id, clock=clock)
        assert deleted.deleted_at is not None

        # Re-create with the same name — the partial UNIQUE excludes
        # tombstones so this must succeed.
        again = create_unit(
            session,
            ctx,
            property_id=prop.id,
            body=UnitCreate.model_validate({"name": "Room 1", "ordinal": 1}),
            clock=clock,
        )
        assert again.id != sibling.id


# ---------------------------------------------------------------------------
# list / get
# ---------------------------------------------------------------------------


class TestListAndGetUnit:
    """Read paths — ordering, filters, cross-workspace gate."""

    def test_list_orders_by_ordinal(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)
        prop = _make_property(session, ctx, name="Villa", clock=clock)
        create_unit(
            session,
            ctx,
            property_id=prop.id,
            body=UnitCreate.model_validate({"name": "Z", "ordinal": 5}),
            clock=clock,
        )
        create_unit(
            session,
            ctx,
            property_id=prop.id,
            body=UnitCreate.model_validate({"name": "A", "ordinal": 1}),
            clock=clock,
        )
        rows = list_units(session, ctx, property_id=prop.id)
        assert [r.ordinal for r in rows] == [0, 1, 5]

    def test_get_unknown_unit_404(self, env: tuple[Session, WorkspaceContext]) -> None:
        session, ctx = env
        with pytest.raises(UnitNotFound):
            get_unit(session, ctx, unit_id="01HWA00000000000000GHOST1")


# ---------------------------------------------------------------------------
# Atomicity — property + default-unit bootstrap
# ---------------------------------------------------------------------------


class TestPropertyDefaultUnitAtomicity:
    """The property bootstrap + default unit insert must be all-or-nothing.

    :func:`create_property` calls :func:`create_default_unit_for_property`
    in the same Unit-of-Work *after* flushing the property +
    ``property_workspace`` rows. The whole transaction is governed
    by the caller's UoW, so a failure inside the unit insert must
    leave the property uncommitted. The test simulates a failure by
    monkeypatching :func:`_insert_unit_row` to raise mid-flight.
    """

    def test_unit_insert_failure_rolls_back_property(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        clock = FrozenClock(_PINNED)

        # Use a savepoint so we can roll back without poisoning the
        # outer test transaction managed by ``db_session``. The
        # ``with session.begin_nested()`` block re-raises after
        # rollback, which is what a real UoW would do.
        with (
            patch(
                "app.domain.places.property_service.create_default_unit_for_property",
                side_effect=RuntimeError("simulated unit insert failure"),
            ),
            pytest.raises(RuntimeError, match="simulated unit insert failure"),
            session.begin_nested(),
        ):
            create_property(
                session,
                ctx,
                body=PropertyCreate.model_validate(
                    {
                        "name": "Atomic Villa",
                        "kind": "vacation",
                        "address": "1 Atomic Way",
                        "address_json": {"country": "FR"},
                        "country": "FR",
                        "timezone": "Europe/Paris",
                    }
                ),
                clock=clock,
            )

        # After the savepoint rolled back, no property with the
        # bootstrap name should be visible. Bypass the ORM tenant
        # filter by querying the table directly with a SQL ``WHERE
        # name`` — if any row sneaked through, the test fails loud.
        rows = session.scalars(
            select(Property).where(Property.name == "Atomic Villa")
        ).all()
        assert rows == []

        # And of course no orphan unit either.
        unit_rows = session.scalars(
            select(Unit).where(Unit.name == "Atomic Villa")
        ).all()
        assert unit_rows == []
