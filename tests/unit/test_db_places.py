"""Unit tests for :mod:`app.adapters.db.places.models`.

Pure-Python sanity on the SQLAlchemy mapped classes: construction,
default handling, and the shape of ``__table_args__``. Integration
coverage (migrations, FK cascade, uniqueness + CHECK violations
against a real DB, tenant filter behaviour) lives in
``tests/integration/test_db_places.py``.

See ``docs/specs/02-domain-model.md`` §"property_workspace" and
``docs/specs/04-properties-and-stays.md`` §"Property" / §"Unit" /
§"Area".
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, Index

from app.adapters.db.places import (
    Area,
    Property,
    PropertyClosure,
    PropertyWorkRoleAssignment,
    PropertyWorkspace,
    Unit,
)
from app.adapters.db.places import models as places_models

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


class TestPropertyModel:
    """The ``Property`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        prop = Property(
            id="01HWA00000000000000000PRPA",
            address="12 Chemin des Oliviers, Antibes",
            timezone="Europe/Paris",
            tags_json=[],
            created_at=_PINNED,
        )
        assert prop.id == "01HWA00000000000000000PRPA"
        assert prop.address == "12 Chemin des Oliviers, Antibes"
        assert prop.timezone == "Europe/Paris"
        # ``lat`` / ``lon`` are nullable and default to ``None``.
        assert prop.lat is None
        assert prop.lon is None
        assert prop.tags_json == []
        assert prop.created_at == _PINNED

    def test_with_geo_and_tags(self) -> None:
        prop = Property(
            id="01HWA00000000000000000PRPB",
            address="Marina Sud, Antibes",
            timezone="Europe/Paris",
            lat=43.5808,
            lon=7.1251,
            tags_json=["riviera", "summer-only"],
            created_at=_PINNED,
        )
        assert prop.lat == 43.5808
        assert prop.lon == 7.1251
        assert prop.tags_json == ["riviera", "summer-only"]

    def test_tablename(self) -> None:
        assert Property.__tablename__ == "property"


class TestPropertyWorkspaceModel:
    """The ``PropertyWorkspace`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        pw = PropertyWorkspace(
            property_id="01HWA00000000000000000PRPA",
            workspace_id="01HWA00000000000000000WSPA",
            label="Villa Sud",
            membership_role="owner_workspace",
            created_at=_PINNED,
        )
        assert pw.property_id == "01HWA00000000000000000PRPA"
        assert pw.workspace_id == "01HWA00000000000000000WSPA"
        assert pw.label == "Villa Sud"
        assert pw.membership_role == "owner_workspace"
        assert pw.created_at == _PINNED

    def test_tablename(self) -> None:
        assert PropertyWorkspace.__tablename__ == "property_workspace"

    def test_membership_role_check_constraint_present(self) -> None:
        """``__table_args__`` carries the ``membership_role`` CHECK.

        cd-hsk added a sibling ``status`` CHECK on the same table; both
        constraints live in ``__table_args__`` and are asserted
        independently. The SA naming convention prefixes constraint
        names with ``ck_<table>_``.
        """
        checks = [
            c
            for c in PropertyWorkspace.__table_args__
            if isinstance(c, CheckConstraint)
        ]
        membership_check = next(
            (c for c in checks if c.name == "ck_property_workspace_membership_role"),
            None,
        )
        assert membership_check is not None
        sql = str(membership_check.sqltext)
        for role in ("owner_workspace", "managed_workspace", "observer_workspace"):
            assert role in sql, f"{role} missing from CHECK constraint"

    def test_status_check_constraint_present(self) -> None:
        """``__table_args__`` carries the cd-hsk ``status`` CHECK."""
        checks = [
            c
            for c in PropertyWorkspace.__table_args__
            if isinstance(c, CheckConstraint)
        ]
        status_check = next(
            (c for c in checks if c.name == "ck_property_workspace_status"),
            None,
        )
        assert status_check is not None
        sql = str(status_check.sqltext)
        for value in ("invited", "active"):
            assert value in sql, f"{value} missing from status CHECK"

    def test_workspace_index_present(self) -> None:
        indexes = [i for i in PropertyWorkspace.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_property_workspace_workspace" in names
        assert "ix_property_workspace_property" in names
        ws_idx = next(i for i in indexes if i.name == "ix_property_workspace_workspace")
        assert [c.name for c in ws_idx.columns] == ["workspace_id"]
        prop_idx = next(
            i for i in indexes if i.name == "ix_property_workspace_property"
        )
        assert [c.name for c in prop_idx.columns] == ["property_id"]


class TestUnitModel:
    """The ``Unit`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        unit = Unit(
            id="01HWA00000000000000000UNTA",
            property_id="01HWA00000000000000000PRPA",
            label="Main house",
            type="villa",
            capacity=6,
            created_at=_PINNED,
        )
        assert unit.id == "01HWA00000000000000000UNTA"
        assert unit.property_id == "01HWA00000000000000000PRPA"
        assert unit.label == "Main house"
        assert unit.type == "villa"
        assert unit.capacity == 6
        assert unit.created_at == _PINNED

    def test_tablename(self) -> None:
        assert Unit.__tablename__ == "unit"

    def test_type_check_constraint_present(self) -> None:
        checks = [c for c in Unit.__table_args__ if isinstance(c, CheckConstraint)]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for kind in ("apartment", "studio", "room", "bungalow", "villa", "other"):
            assert kind in sql, f"{kind} missing from CHECK constraint"

    def test_property_index_present(self) -> None:
        indexes = [i for i in Unit.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_unit_property" in names
        target = next(i for i in indexes if i.name == "ix_unit_property")
        assert [c.name for c in target.columns] == ["property_id"]


class TestAreaModel:
    """The ``Area`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        area = Area(
            id="01HWA00000000000000000ARAA",
            property_id="01HWA00000000000000000PRPA",
            label="Kitchen",
            created_at=_PINNED,
        )
        assert area.id == "01HWA00000000000000000ARAA"
        assert area.property_id == "01HWA00000000000000000PRPA"
        assert area.label == "Kitchen"
        # ``icon`` is nullable; defaults to ``None`` until a UI writes one.
        assert area.icon is None
        assert area.created_at == _PINNED

    def test_with_icon_and_ordering(self) -> None:
        area = Area(
            id="01HWA00000000000000000ARAB",
            property_id="01HWA00000000000000000PRPA",
            label="Pool",
            icon="waves",
            ordering=10,
            created_at=_PINNED,
        )
        assert area.icon == "waves"
        assert area.ordering == 10

    def test_tablename(self) -> None:
        assert Area.__tablename__ == "area"

    def test_property_index_present(self) -> None:
        indexes = [i for i in Area.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_area_property" in names
        target = next(i for i in indexes if i.name == "ix_area_property")
        assert [c.name for c in target.columns] == ["property_id"]


class TestPropertyClosureModel:
    """The ``PropertyClosure`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        closure = PropertyClosure(
            id="01HWA00000000000000000PCLA",
            property_id="01HWA00000000000000000PRPA",
            starts_at=_PINNED,
            ends_at=datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC),
            reason="renovation",
            created_at=_PINNED,
        )
        assert closure.id == "01HWA00000000000000000PCLA"
        assert closure.property_id == "01HWA00000000000000000PRPA"
        assert closure.unit_id is None
        assert closure.starts_at == _PINNED
        assert closure.ends_at == datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
        assert closure.reason == "renovation"
        # ``created_by_user_id`` is nullable; defaults to ``None``.
        assert closure.created_by_user_id is None
        assert closure.created_at == _PINNED

    def test_with_created_by(self) -> None:
        closure = PropertyClosure(
            id="01HWA00000000000000000PCLB",
            property_id="01HWA00000000000000000PRPA",
            starts_at=_PINNED,
            ends_at=datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC),
            reason="owner-stay",
            created_by_user_id="01HWA00000000000000000USRA",
            created_at=_PINNED,
        )
        assert closure.created_by_user_id == "01HWA00000000000000000USRA"

    def test_tablename(self) -> None:
        assert PropertyClosure.__tablename__ == "property_closure"

    def test_ends_after_starts_check_present(self) -> None:
        checks = [
            c for c in PropertyClosure.__table_args__ if isinstance(c, CheckConstraint)
        ]
        assert len(checks) == 1
        # Constraint name is ``ends_after_starts`` on the model; the
        # shared naming convention (``ck_%(table_name)s_%(constraint_name)s``)
        # rewrites it to ``ck_property_closure_ends_after_starts`` on the
        # bound column, so assert the suffix rather than the raw name.
        assert checks[0].name is not None
        assert str(checks[0].name).endswith("ends_after_starts")
        assert "ends_at" in str(checks[0].sqltext)
        assert "starts_at" in str(checks[0].sqltext)

    def test_property_starts_index_present(self) -> None:
        indexes = [i for i in PropertyClosure.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_property_closure_property_starts" in names
        assert "ix_property_closure_unit" in names
        target = next(
            i for i in indexes if i.name == "ix_property_closure_property_starts"
        )
        assert [c.name for c in target.columns] == ["property_id", "starts_at"]


class TestPackageReExports:
    """``app.adapters.db.places`` re-exports every v1-slice model."""

    def test_models_re_exported(self) -> None:
        assert Property is places_models.Property
        assert PropertyWorkspace is places_models.PropertyWorkspace
        assert Unit is places_models.Unit
        assert Area is places_models.Area
        assert PropertyClosure is places_models.PropertyClosure
        # cd-e4m3 added the per-property pinning of a ``user_work_role``.
        assert PropertyWorkRoleAssignment is places_models.PropertyWorkRoleAssignment


class TestRegistryIntent:
    """``property_workspace`` and ``property_work_role_assignment``
    are the only places tables registered as workspace-scoped.

    ``property`` is shared across workspaces via the junction, and
    ``unit`` / ``area`` / ``property_closure`` reach the boundary
    through their parent property (see the package docstring).
    ``property_work_role_assignment`` (cd-e4m3) carries its own
    denormalised ``workspace_id`` column, so it joins
    ``property_workspace`` as the second registered places table —
    same pattern as ``work_engagement`` / ``user_work_role``.

    The assertions call :func:`app.tenancy.registry.register` directly
    rather than relying on the import-time side effect of
    ``app.adapters.db.places``: a sibling ``test_tenancy_orm_filter``
    autouse fixture calls ``registry._reset_for_tests()`` which wipes
    the process-wide set, so asserting presence after that reset would
    be flaky. The tests below encode the invariant — "the only places
    tables we scope are the junction and the assignment" — without
    over-coupling to import ordering.
    """

    def test_registered_places_tables(self) -> None:
        """Registering both scoped places tables flips :func:`is_scoped`."""
        from app.tenancy import registry

        registry.register("property_workspace")
        registry.register("property_work_role_assignment")
        assert registry.is_scoped("property_workspace") is True
        assert registry.is_scoped("property_work_role_assignment") is True

    def test_other_places_tables_not_registered_by_default(self) -> None:
        """The remaining places tables stay absent from a fresh registry.

        We reset the registry, re-apply the package's import-time
        policy, and confirm only the two scoped tables remain. The
        snapshot / restore around the reset keeps the per-suite
        registrations from sibling packages (``work_engagement``,
        ``user_work_role``, ``user_workspace``, …) intact for the
        rest of the suite — without it, a sibling test running later
        in the file order would observe a wiped registry and fail
        ``is_scoped`` checks that pass in isolation.
        """
        from app.tenancy import registry

        snapshot = registry.scoped_tables()
        try:
            registry._reset_for_tests()
            registry.register("property_workspace")
            registry.register("property_work_role_assignment")
            scoped = registry.scoped_tables()
            assert "property_workspace" in scoped
            assert "property_work_role_assignment" in scoped
            for table in ("property", "unit", "area", "property_closure"):
                assert table not in scoped, f"{table} must not be scoped in v1"
        finally:
            # Restore the original registration set so sibling tests
            # (e.g. ``test_work_engagement_registered``) keep observing
            # their tables as scoped.
            registry._reset_for_tests()
            for table in snapshot:
                registry.register(table)
