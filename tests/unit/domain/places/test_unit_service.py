"""Unit tests for :mod:`app.domain.places.unit_service` (cd-y62).

Mirrors the in-memory SQLite bootstrap in
``tests/unit/places/test_property_service.py`` and
``tests/unit/domain/places/test_membership_service.py``: a fresh
engine per test, pull every sibling ``models`` module onto the
shared ``Base.metadata``, run ``Base.metadata.create_all``, drive
the domain code with a :class:`FrozenClock`.

Covers the cd-y62 acceptance criteria:

* Property create auto-creates one default unit named after the
  property with ``ordinal = 0``.
* :func:`create_unit` rejects a duplicate name within the same
  property with :class:`UnitNameTaken`.
* :func:`update_unit` rejects renaming onto an existing sibling.
* :func:`soft_delete_unit` refuses to retire the last live unit
  with :class:`LastUnitProtected`.
* Cross-workspace access on every read + write surface collapses
  to :class:`UnitNotFound` (404).
* Every mutation writes one audit row in the same transaction.
* :func:`list_units` returns rows in ``(ordinal, id)`` order and
  honours the ``deleted`` filter.

See ``docs/specs/04-properties-and-stays.md`` §"Unit" /
§"Invariants".
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.places.models import Unit
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
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
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture(name="engine_unit")
def fixture_engine() -> Iterator[Engine]:
    """In-memory SQLite engine, schema created from ``Base.metadata``."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(name="session_unit")
def fixture_session(engine_unit: Engine) -> Iterator[Session]:
    """Per-test session — no tenant filter installed.

    The unit service routes every workspace check through an
    explicit ``property_workspace`` join, so the in-memory tests do
    not need the ORM tenant filter to exercise the gates.
    """
    factory = sessionmaker(
        bind=engine_unit,
        expire_on_commit=False,
        class_=Session,
    )
    with factory() as s:
        yield s


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _ctx(
    *,
    workspace_id: str,
    slug: str,
    actor_id: str = "01HWA00000000000000000USR",
) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to ``workspace_id``."""
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL2",
    )


def _make_workspace(session: Session, *, slug: str) -> str:
    """Insert a :class:`Workspace` row and return its id.

    The unit service does not gate on the ``owners`` permission
    group (only the property + membership services do), so the
    workspace fixture is the bare minimum — no permission_group /
    role_grant seeding needed.
    """
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


def _create_property(
    session: Session,
    *,
    workspace_id: str,
    slug: str,
    name: str = "Villa Sud",
    clock: FrozenClock,
) -> PropertyView:
    """Bootstrap a property + owner_workspace junction + default unit."""
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
    return create_property(
        session,
        _ctx(workspace_id=workspace_id, slug=slug),
        body=body,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPropertyAutoCreatesDefaultUnit:
    """Property bootstrap → exactly one default unit, named after the property."""

    def test_property_create_seeds_default_unit(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_unit, slug="dft-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="dft-ws",
            name="Villa Sud",
            clock=frozen_clock,
        )

        units = list_units(
            session_unit, _ctx(workspace_id=ws, slug="dft-ws"), property_id=prop.id
        )
        assert len(units) == 1
        only = units[0]
        assert only.name == prop.name == "Villa Sud"
        assert only.ordinal == 0
        assert only.property_id == prop.id
        assert only.deleted_at is None

    def test_property_create_writes_unit_audit(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_unit, slug="aud-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="aud-ws",
            clock=frozen_clock,
        )
        units = list_units(
            session_unit, _ctx(workspace_id=ws, slug="aud-ws"), property_id=prop.id
        )
        only = units[0]
        audits = session_unit.scalars(
            select(AuditLog).where(
                AuditLog.entity_kind == "unit",
                AuditLog.entity_id == only.id,
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].action == "create"
        assert audits[0].diff["after"]["name"] == prop.name


class TestCreateUnit:
    """``create_unit`` happy path + name-collision + cross-workspace gate."""

    def test_create_second_unit(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_unit, slug="cs-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="cs-ws",
            name="Apt 3",
            clock=frozen_clock,
        )

        view = create_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="cs-ws"),
            property_id=prop.id,
            body=UnitCreate.model_validate(
                {
                    "name": "Room 1",
                    "ordinal": 1,
                    "default_checkin_time": "16:00",
                    "default_checkout_time": "10:00",
                    "max_guests": 2,
                    "notes_md": "King bed.",
                }
            ),
            clock=frozen_clock,
        )
        assert view.name == "Room 1"
        assert view.ordinal == 1
        assert view.default_checkin_time == "16:00"
        assert view.default_checkout_time == "10:00"
        assert view.max_guests == 2
        assert view.notes_md == "King bed."

        # Two live units now (the bootstrap + the new one).
        units = list_units(
            session_unit, _ctx(workspace_id=ws, slug="cs-ws"), property_id=prop.id
        )
        assert {u.name for u in units} == {"Apt 3", "Room 1"}

    def test_duplicate_name_rejected(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_unit, slug="dup-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="dup-ws",
            name="Villa Sud",
            clock=frozen_clock,
        )
        # The bootstrap unit is named "Villa Sud"; another insert with
        # the same name must raise UnitNameTaken (before the partial
        # UNIQUE fires at flush).
        with pytest.raises(UnitNameTaken):
            create_unit(
                session_unit,
                _ctx(workspace_id=ws, slug="dup-ws"),
                property_id=prop.id,
                body=UnitCreate.model_validate({"name": "Villa Sud", "ordinal": 1}),
                clock=frozen_clock,
            )

    def test_create_cross_workspace_denied(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        """A unit insert against a property in another workspace 404s."""
        ws_a = _make_workspace(session_unit, slug="cw-a")
        ws_b = _make_workspace(session_unit, slug="cw-b")
        prop = _create_property(
            session_unit,
            workspace_id=ws_a,
            slug="cw-a",
            clock=frozen_clock,
        )

        with pytest.raises(UnitNotFound):
            create_unit(
                session_unit,
                _ctx(workspace_id=ws_b, slug="cw-b"),
                property_id=prop.id,
                body=UnitCreate.model_validate({"name": "Room 1"}),
                clock=frozen_clock,
            )

    def test_blank_name_rejected_at_dto(self) -> None:
        """Pydantic catches a blank name before the service runs."""
        with pytest.raises(ValidationError):
            UnitCreate.model_validate({"name": "   "})

    def test_invalid_checkin_time_rejected_at_dto(self) -> None:
        """Bad ``HH:MM`` shape is a 422 at DTO ingress."""
        with pytest.raises(ValidationError):
            UnitCreate.model_validate({"name": "Room", "default_checkin_time": "25:00"})

    def test_unicode_name_round_trips(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        """Accented + emoji unit names survive a round-trip."""
        ws = _make_workspace(session_unit, slug="uni-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="uni-ws",
            clock=frozen_clock,
        )
        view = create_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="uni-ws"),
            property_id=prop.id,
            body=UnitCreate.model_validate(
                {"name": "Chambre — Étage 2 ✨", "ordinal": 2}
            ),
            clock=frozen_clock,
        )
        assert view.name == "Chambre — Étage 2 ✨"


class TestUpdateUnit:
    """``update_unit`` happy path + name-collision + cross-workspace gate."""

    def test_round_trip_update(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_unit, slug="upd-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="upd-ws",
            name="Villa Sud",
            clock=frozen_clock,
        )
        units = list_units(
            session_unit, _ctx(workspace_id=ws, slug="upd-ws"), property_id=prop.id
        )
        target = units[0]

        later = FrozenClock(_PINNED.replace(hour=14))
        updated = update_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="upd-ws"),
            unit_id=target.id,
            body=UnitUpdate.model_validate(
                {
                    "name": "Main suite",
                    "ordinal": 0,
                    "max_guests": 4,
                    "notes_md": "Renovated.",
                }
            ),
            clock=later,
        )
        assert updated.name == "Main suite"
        assert updated.max_guests == 4
        assert updated.notes_md == "Renovated."

    def test_rename_onto_sibling_rejected(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_unit, slug="rn-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="rn-ws",
            name="Villa Sud",
            clock=frozen_clock,
        )
        room1 = create_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="rn-ws"),
            property_id=prop.id,
            body=UnitCreate.model_validate({"name": "Room 1", "ordinal": 1}),
            clock=frozen_clock,
        )
        with pytest.raises(UnitNameTaken):
            update_unit(
                session_unit,
                _ctx(workspace_id=ws, slug="rn-ws"),
                unit_id=room1.id,
                body=UnitUpdate.model_validate({"name": "Villa Sud", "ordinal": 0}),
                clock=frozen_clock,
            )

    def test_rename_to_self_is_no_collision(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        """Updating a unit without changing its name is not a collision."""
        ws = _make_workspace(session_unit, slug="self-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="self-ws",
            name="Villa Sud",
            clock=frozen_clock,
        )
        units = list_units(
            session_unit,
            _ctx(workspace_id=ws, slug="self-ws"),
            property_id=prop.id,
        )
        target = units[0]
        # Same name, different notes — must succeed.
        updated = update_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="self-ws"),
            unit_id=target.id,
            body=UnitUpdate.model_validate(
                {"name": "Villa Sud", "ordinal": 0, "notes_md": "Edited."}
            ),
            clock=frozen_clock,
        )
        assert updated.notes_md == "Edited."

    def test_update_cross_workspace_denied(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws_a = _make_workspace(session_unit, slug="upd-cw-a")
        ws_b = _make_workspace(session_unit, slug="upd-cw-b")
        prop = _create_property(
            session_unit,
            workspace_id=ws_a,
            slug="upd-cw-a",
            clock=frozen_clock,
        )
        units = list_units(
            session_unit,
            _ctx(workspace_id=ws_a, slug="upd-cw-a"),
            property_id=prop.id,
        )
        target = units[0]
        with pytest.raises(UnitNotFound):
            update_unit(
                session_unit,
                _ctx(workspace_id=ws_b, slug="upd-cw-b"),
                unit_id=target.id,
                body=UnitUpdate.model_validate({"name": "Hijacked"}),
                clock=frozen_clock,
            )


class TestSoftDeleteUnit:
    """``soft_delete_unit`` enforces the §04 last-unit invariant."""

    def test_cannot_delete_last_unit(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_unit, slug="last-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="last-ws",
            clock=frozen_clock,
        )
        units = list_units(
            session_unit,
            _ctx(workspace_id=ws, slug="last-ws"),
            property_id=prop.id,
        )
        only = units[0]
        with pytest.raises(LastUnitProtected):
            soft_delete_unit(
                session_unit,
                _ctx(workspace_id=ws, slug="last-ws"),
                unit_id=only.id,
                clock=frozen_clock,
            )

    def test_can_delete_when_sibling_exists(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_unit, slug="sib-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="sib-ws",
            clock=frozen_clock,
        )
        sibling = create_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="sib-ws"),
            property_id=prop.id,
            body=UnitCreate.model_validate({"name": "Room 1", "ordinal": 1}),
            clock=frozen_clock,
        )

        deleted = soft_delete_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="sib-ws"),
            unit_id=sibling.id,
            clock=frozen_clock,
        )
        assert deleted.deleted_at is not None

        # Live list now has only the bootstrap unit; the deleted
        # surface returns the tombstone.
        live = list_units(
            session_unit, _ctx(workspace_id=ws, slug="sib-ws"), property_id=prop.id
        )
        assert [u.id for u in live] != [sibling.id]
        retired = list_units(
            session_unit,
            _ctx(workspace_id=ws, slug="sib-ws"),
            property_id=prop.id,
            deleted=True,
        )
        assert [u.id for u in retired] == [sibling.id]

    def test_delete_then_recreate_with_same_name(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        """The partial UNIQUE excludes tombstones — re-create must succeed."""
        ws = _make_workspace(session_unit, slug="rec-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="rec-ws",
            clock=frozen_clock,
        )
        first = create_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="rec-ws"),
            property_id=prop.id,
            body=UnitCreate.model_validate({"name": "Room 1", "ordinal": 1}),
            clock=frozen_clock,
        )
        soft_delete_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="rec-ws"),
            unit_id=first.id,
            clock=frozen_clock,
        )
        second = create_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="rec-ws"),
            property_id=prop.id,
            body=UnitCreate.model_validate({"name": "Room 1", "ordinal": 1}),
            clock=frozen_clock,
        )
        assert second.id != first.id

    def test_delete_writes_audit(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_unit, slug="dau-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="dau-ws",
            clock=frozen_clock,
        )
        sib = create_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="dau-ws"),
            property_id=prop.id,
            body=UnitCreate.model_validate({"name": "Room 1", "ordinal": 1}),
            clock=frozen_clock,
        )
        soft_delete_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="dau-ws"),
            unit_id=sib.id,
            clock=frozen_clock,
        )
        audits = session_unit.scalars(
            select(AuditLog).where(
                AuditLog.entity_kind == "unit",
                AuditLog.entity_id == sib.id,
                AuditLog.action == "delete",
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].diff["before"]["deleted_at"] is None
        assert audits[0].diff["after"]["deleted_at"] is not None

    def test_delete_cross_workspace_denied(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws_a = _make_workspace(session_unit, slug="dx-a")
        ws_b = _make_workspace(session_unit, slug="dx-b")
        prop = _create_property(
            session_unit,
            workspace_id=ws_a,
            slug="dx-a",
            clock=frozen_clock,
        )
        sib = create_unit(
            session_unit,
            _ctx(workspace_id=ws_a, slug="dx-a"),
            property_id=prop.id,
            body=UnitCreate.model_validate({"name": "Room 1", "ordinal": 1}),
            clock=frozen_clock,
        )
        with pytest.raises(UnitNotFound):
            soft_delete_unit(
                session_unit,
                _ctx(workspace_id=ws_b, slug="dx-b"),
                unit_id=sib.id,
                clock=frozen_clock,
            )


class TestListAndGetUnit:
    """``list_units`` + ``get_unit`` ordering, filters, cross-workspace gate."""

    def test_list_orders_by_ordinal_then_id(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_unit, slug="ord-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="ord-ws",
            name="Villa",
            clock=frozen_clock,
        )
        # Bootstrap unit lands at ordinal=0; add two siblings out of order.
        create_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="ord-ws"),
            property_id=prop.id,
            body=UnitCreate.model_validate({"name": "Z", "ordinal": 5}),
            clock=frozen_clock,
        )
        create_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="ord-ws"),
            property_id=prop.id,
            body=UnitCreate.model_validate({"name": "A", "ordinal": 1}),
            clock=frozen_clock,
        )
        units = list_units(
            session_unit,
            _ctx(workspace_id=ws, slug="ord-ws"),
            property_id=prop.id,
        )
        assert [u.ordinal for u in units] == [0, 1, 5]

    def test_get_unknown_unit_404(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_unit, slug="ghost-ws")
        # Need a property to set up a workspace context that's valid;
        # the unknown unit id is what we test.
        _create_property(
            session_unit, workspace_id=ws, slug="ghost-ws", clock=frozen_clock
        )
        with pytest.raises(UnitNotFound):
            get_unit(
                session_unit,
                _ctx(workspace_id=ws, slug="ghost-ws"),
                unit_id="01HWAGHOST00000000000000UN",
            )

    def test_list_unknown_property_404(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_unit, slug="up-ws")
        with pytest.raises(UnitNotFound):
            list_units(
                session_unit,
                _ctx(workspace_id=ws, slug="up-ws"),
                property_id="01HWAGHOST00000000000000PR",
            )

    def test_list_cross_workspace_denied(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws_a = _make_workspace(session_unit, slug="lx-a")
        ws_b = _make_workspace(session_unit, slug="lx-b")
        prop = _create_property(
            session_unit, workspace_id=ws_a, slug="lx-a", clock=frozen_clock
        )
        with pytest.raises(UnitNotFound):
            list_units(
                session_unit,
                _ctx(workspace_id=ws_b, slug="lx-b"),
                property_id=prop.id,
            )

    def test_get_include_deleted_surfaces_tombstone(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_unit, slug="inc-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="inc-ws",
            clock=frozen_clock,
        )
        sib = create_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="inc-ws"),
            property_id=prop.id,
            body=UnitCreate.model_validate({"name": "Room 1", "ordinal": 1}),
            clock=frozen_clock,
        )
        soft_delete_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="inc-ws"),
            unit_id=sib.id,
            clock=frozen_clock,
        )
        # Default get → 404.
        with pytest.raises(UnitNotFound):
            get_unit(
                session_unit,
                _ctx(workspace_id=ws, slug="inc-ws"),
                unit_id=sib.id,
            )
        # include_deleted=True returns the tombstone.
        view = get_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="inc-ws"),
            unit_id=sib.id,
            include_deleted=True,
        )
        assert view.deleted_at is not None


class TestAuditTrail:
    """Every mutation writes one audit row."""

    def test_create_update_writes_audit(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        ws = _make_workspace(session_unit, slug="aut-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="aut-ws",
            clock=frozen_clock,
        )
        view = create_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="aut-ws"),
            property_id=prop.id,
            body=UnitCreate.model_validate({"name": "Room 1", "ordinal": 1}),
            clock=frozen_clock,
        )
        update_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="aut-ws"),
            unit_id=view.id,
            body=UnitUpdate.model_validate({"name": "Room 1 — renamed", "ordinal": 1}),
            clock=frozen_clock,
        )
        audits = session_unit.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_kind == "unit",
                AuditLog.entity_id == view.id,
            )
            .order_by(AuditLog.created_at, AuditLog.id)
        ).all()
        # One create + one update (+ no soft-delete).
        actions = [a.action for a in audits]
        assert actions == ["create", "update"]

    def test_row_carries_label_mirror(
        self, session_unit: Session, frozen_clock: FrozenClock
    ) -> None:
        """The legacy ``label`` column mirrors ``name`` for back-compat."""
        ws = _make_workspace(session_unit, slug="lab-ws")
        prop = _create_property(
            session_unit,
            workspace_id=ws,
            slug="lab-ws",
            clock=frozen_clock,
        )
        view = create_unit(
            session_unit,
            _ctx(workspace_id=ws, slug="lab-ws"),
            property_id=prop.id,
            body=UnitCreate.model_validate({"name": "Loft", "ordinal": 1}),
            clock=frozen_clock,
        )
        row = session_unit.get(Unit, view.id)
        assert row is not None
        assert row.label == "Loft"
        assert row.name == "Loft"
