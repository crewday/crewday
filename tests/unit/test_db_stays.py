"""Unit tests for :mod:`app.adapters.db.stays.models`.

Pure-Python sanity on the SQLAlchemy mapped classes: construction,
default handling, and the shape of ``__table_args__`` (CHECK
constraints, unique composites, index shape). Integration coverage
(migrations, FK cascade, uniqueness + CHECK violations against a
real DB, tenant filter behaviour) lives in
``tests/integration/test_db_stays.py``.

See ``docs/specs/02-domain-model.md`` §"reservation", §"ical_feed",
§"stay_bundle", and ``docs/specs/04-properties-and-stays.md``
§"Stay (reservation)" / §"iCal feed".
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from app.adapters.db.stays import IcalFeed, Reservation, StayBundle
from app.adapters.db.stays import models as stays_models

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


class TestIcalFeedModel:
    """The ``IcalFeed`` mapped class constructs from the full §04 shape."""

    def test_minimal_construction(self) -> None:
        feed = IcalFeed(
            id="01HWA00000000000000000FEDA",
            workspace_id="01HWA00000000000000000WSPA",
            property_id="01HWA00000000000000000PRPA",
            url="https://example.com/feed.ics",
            provider="airbnb",
            enabled=True,
            created_at=_PINNED,
        )
        assert feed.id == "01HWA00000000000000000FEDA"
        assert feed.workspace_id == "01HWA00000000000000000WSPA"
        assert feed.property_id == "01HWA00000000000000000PRPA"
        assert feed.url == "https://example.com/feed.ics"
        assert feed.provider == "airbnb"
        assert feed.enabled is True
        # Nullable fields default to None.
        assert feed.last_polled_at is None
        assert feed.last_etag is None
        assert feed.last_error is None
        assert feed.unit_id is None
        assert feed.created_at == _PINNED

    def test_with_poll_state(self) -> None:
        feed = IcalFeed(
            id="01HWA00000000000000000FEDB",
            workspace_id="01HWA00000000000000000WSPA",
            property_id="01HWA00000000000000000PRPA",
            url="https://example.com/feed.ics",
            provider="vrbo",
            last_polled_at=_PINNED,
            last_etag='W/"deadbeef"',
            last_error="ical_url_timeout",
            enabled=False,
            created_at=_PINNED,
        )
        assert feed.last_polled_at == _PINNED
        assert feed.last_etag == 'W/"deadbeef"'
        assert feed.last_error == "ical_url_timeout"
        assert feed.enabled is False

    def test_with_unit_and_cadence(self) -> None:
        """cd-ewd7 columns: ``unit_id``, ``poll_cadence`` round-trip."""
        feed = IcalFeed(
            id="01HWA00000000000000000FEDC",
            workspace_id="01HWA00000000000000000WSPA",
            property_id="01HWA00000000000000000PRPA",
            unit_id="01HWA00000000000000000UNIT",
            url="https://calendar.google.com/x/ical.ics",
            provider="gcal",
            poll_cadence="*/30 * * * *",
            enabled=True,
            created_at=_PINNED,
        )
        assert feed.unit_id == "01HWA00000000000000000UNIT"
        assert feed.poll_cadence == "*/30 * * * *"
        assert feed.provider == "gcal"

    def test_tablename(self) -> None:
        assert IcalFeed.__tablename__ == "ical_feed"

    def test_provider_check_present(self) -> None:
        # Constraint name ``provider`` on the model; the shared naming
        # convention rewrites it to ``ck_ical_feed_provider`` on the
        # bound column, so match by suffix (mirrors the sibling
        # ``tasks`` / ``places`` test pattern). cd-ewd7 widened the
        # set from v1's ``airbnb | vrbo | booking | custom``.
        checks = [
            c
            for c in IcalFeed.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("provider")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for provider in ("airbnb", "vrbo", "booking", "gcal", "generic", "custom"):
            assert provider in sql, f"{provider} missing from CHECK constraint"

    def test_workspace_property_index_present(self) -> None:
        indexes = [i for i in IcalFeed.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_ical_feed_workspace_property" in names
        target = next(i for i in indexes if i.name == "ix_ical_feed_workspace_property")
        assert [c.name for c in target.columns] == ["workspace_id", "property_id"]

    def test_unit_index_present(self) -> None:
        """cd-ewd7: the poller's ``unit_id`` lookup needs an index."""
        indexes = [i for i in IcalFeed.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_ical_feed_unit" in names
        target = next(i for i in indexes if i.name == "ix_ical_feed_unit")
        assert [c.name for c in target.columns] == ["unit_id"]


class TestReservationModel:
    """The ``Reservation`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        res = Reservation(
            id="01HWA00000000000000000RESA",
            workspace_id="01HWA00000000000000000WSPA",
            property_id="01HWA00000000000000000PRPA",
            external_uid="HMABC123",
            check_in=_PINNED,
            check_out=_LATER,
            status="scheduled",
            source="ical",
            created_at=_PINNED,
        )
        assert res.ical_feed_id is None
        assert res.guest_name is None
        assert res.guest_count is None
        assert res.raw_summary is None
        assert res.raw_description is None
        assert res.status == "scheduled"
        assert res.source == "ical"

    def test_with_full_payload(self) -> None:
        res = Reservation(
            id="01HWA00000000000000000RESB",
            workspace_id="01HWA00000000000000000WSPA",
            property_id="01HWA00000000000000000PRPA",
            ical_feed_id="01HWA00000000000000000FEDA",
            external_uid="HMABC124",
            check_in=_PINNED,
            check_out=_LATER,
            guest_name="Ada Lovelace",
            guest_count=2,
            status="checked_in",
            source="manual",
            raw_summary="Reserved by Ada",
            raw_description="Arrives 4pm",
            created_at=_PINNED,
        )
        assert res.ical_feed_id == "01HWA00000000000000000FEDA"
        assert res.guest_name == "Ada Lovelace"
        assert res.guest_count == 2
        assert res.raw_summary == "Reserved by Ada"

    def test_tablename(self) -> None:
        assert Reservation.__tablename__ == "reservation"

    def test_status_check_present(self) -> None:
        checks = [
            c
            for c in Reservation.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("status")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for status in ("scheduled", "checked_in", "completed", "cancelled"):
            assert status in sql, f"{status} missing from CHECK constraint"

    def test_source_check_present(self) -> None:
        checks = [
            c
            for c in Reservation.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("source")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for source in ("ical", "manual", "api"):
            assert source in sql, f"{source} missing from CHECK constraint"

    def test_check_out_after_check_in_present(self) -> None:
        checks = [
            c
            for c in Reservation.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("check_out_after_check_in")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "check_out" in sql
        assert "check_in" in sql

    def test_unique_feed_external_uid_present(self) -> None:
        uniques = [
            u for u in Reservation.__table_args__ if isinstance(u, UniqueConstraint)
        ]
        assert len(uniques) == 1
        assert [c.name for c in uniques[0].columns] == [
            "ical_feed_id",
            "external_uid",
        ]

    def test_property_check_in_index_present(self) -> None:
        indexes = [i for i in Reservation.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_reservation_property_check_in" in names
        target = next(
            i for i in indexes if i.name == "ix_reservation_property_check_in"
        )
        assert [c.name for c in target.columns] == ["property_id", "check_in"]


class TestStayBundleModel:
    """The ``StayBundle`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        bundle = StayBundle(
            id="01HWA00000000000000000STBA",
            workspace_id="01HWA00000000000000000WSPA",
            reservation_id="01HWA00000000000000000RESA",
            kind="turnover",
            tasks_json=[],
            created_at=_PINNED,
        )
        assert bundle.id == "01HWA00000000000000000STBA"
        assert bundle.reservation_id == "01HWA00000000000000000RESA"
        assert bundle.kind == "turnover"
        assert bundle.tasks_json == []
        assert bundle.created_at == _PINNED

    def test_with_tasks_payload(self) -> None:
        payload: list[dict[str, object]] = [
            {"template_id": "01HWA00000000000000000TPLA", "offset_hours": 0},
            {"template_id": "01HWA00000000000000000TPLB", "offset_hours": 2},
        ]
        bundle = StayBundle(
            id="01HWA00000000000000000STBB",
            workspace_id="01HWA00000000000000000WSPA",
            reservation_id="01HWA00000000000000000RESA",
            kind="welcome",
            tasks_json=payload,
            created_at=_PINNED,
        )
        assert bundle.kind == "welcome"
        assert bundle.tasks_json == payload

    def test_tablename(self) -> None:
        assert StayBundle.__tablename__ == "stay_bundle"

    def test_kind_check_present(self) -> None:
        checks = [
            c
            for c in StayBundle.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("kind")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for kind in ("turnover", "welcome", "deep_clean"):
            assert kind in sql, f"{kind} missing from CHECK constraint"

    def test_reservation_index_present(self) -> None:
        indexes = [i for i in StayBundle.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_stay_bundle_reservation" in names
        target = next(i for i in indexes if i.name == "ix_stay_bundle_reservation")
        assert [c.name for c in target.columns] == ["reservation_id"]


class TestPackageReExports:
    """``app.adapters.db.stays`` re-exports every v1-slice model."""

    def test_models_re_exported(self) -> None:
        assert IcalFeed is stays_models.IcalFeed
        assert Reservation is stays_models.Reservation
        assert StayBundle is stays_models.StayBundle


class TestRegistryIntent:
    """Every stays table is registered as workspace-scoped.

    The assertions call :func:`app.tenancy.registry.register` directly
    rather than relying on the import-time side effect of
    ``app.adapters.db.stays``: a sibling ``test_tenancy_orm_filter``
    autouse fixture calls ``registry._reset_for_tests()`` which wipes
    the process-wide set, so asserting presence after that reset
    would be flaky. The tests below encode the invariant — "every
    stays table is scoped" — without over-coupling to import
    ordering.
    """

    def test_every_stays_table_is_registered(self) -> None:
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in ("ical_feed", "reservation", "stay_bundle"):
            registry.register(table)
        scoped = registry.scoped_tables()
        for table in ("ical_feed", "reservation", "stay_bundle"):
            assert table in scoped, f"{table} must be scoped"
