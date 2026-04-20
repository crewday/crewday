"""Integration tests for :mod:`app.adapters.db.stays` against a real DB.

Covers the post-migration schema shape (tables, unique composites,
FKs, CHECK constraints), the referential-integrity contract on all
three tables (CASCADE on workspace / property / reservation; SET
NULL on ``reservation.ical_feed_id``), happy-path round-trip of the
full feed → reservation → bundle chain, CHECK violations, the
idempotent-re-poll unique key, and tenant-filter behaviour (all
three tables scoped; SELECT without a :class:`WorkspaceContext`
raises :class:`TenantFilterMissing`).

The sibling ``tests/unit/test_db_stays.py`` covers pure-Python
model construction without the migration harness.

See ``docs/specs/02-domain-model.md`` §"reservation", §"ical_feed",
§"stay_bundle", and ``docs/specs/04-properties-and-stays.md``
§"Stay (reservation)" / §"iCal feed".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.places.models import Property
from app.adapters.db.stays.models import IcalFeed, Reservation, StayBundle
from app.adapters.db.workspace.models import Workspace
from app.tenancy import registry, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import TenantFilterMissing, install_tenant_filter
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_CHECK_IN = _PINNED + timedelta(days=1)
_CHECK_OUT = _CHECK_IN + timedelta(days=3)


_STAYS_TABLES: tuple[str, ...] = ("ical_feed", "reservation", "stay_bundle")


@pytest.fixture(scope="module")
def filtered_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory with the tenant filter installed.

    Module-scoped so SQLAlchemy's per-sessionmaker event dispatch
    doesn't churn across tests. The top-level ``db_session`` fixture
    binds directly to a raw connection for SAVEPOINT isolation and
    therefore bypasses the filter; tests that need to observe
    :class:`TenantFilterMissing` use this factory explicitly.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_stays_registered() -> None:
    """Re-register the three stays tables as workspace-scoped before each test.

    ``app.adapters.db.stays.__init__`` registers them at import time,
    but a sibling unit test
    (``tests/unit/test_tenancy_orm_filter.py``) calls
    :func:`registry._reset_for_tests` in an autouse fixture, which
    wipes the process-wide registry. Without this fixture the
    import-time registration loses the race and our tenant-filter
    assertions pass in isolation yet silently drop the filter under
    the full suite.
    """
    for table in _STAYS_TABLES:
        registry.register(table)


def _ctx_for(workspace: Workspace, actor_id: str) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to ``workspace``."""
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLS",
    )


def _seed_property(session: Session, *, property_id: str) -> Property:
    """Insert a :class:`Property` row (tenant-agnostic table)."""
    prop = Property(
        id=property_id,
        address="12 Chemin des Oliviers, Antibes",
        timezone="Europe/Paris",
        tags_json=[],
        created_at=_PINNED,
    )
    session.add(prop)
    session.flush()
    return prop


def _seed_feed(
    session: Session,
    *,
    feed_id: str,
    workspace_id: str,
    property_id: str,
    provider: str = "airbnb",
) -> IcalFeed:
    feed = IcalFeed(
        id=feed_id,
        workspace_id=workspace_id,
        property_id=property_id,
        url=f"https://example.com/{feed_id}.ics",
        provider=provider,
        enabled=True,
        created_at=_PINNED,
    )
    session.add(feed)
    session.flush()
    return feed


def _seed_reservation(
    session: Session,
    *,
    reservation_id: str,
    workspace_id: str,
    property_id: str,
    ical_feed_id: str | None = None,
    external_uid: str = "uid-1",
    status: str = "scheduled",
    source: str = "ical",
) -> Reservation:
    res = Reservation(
        id=reservation_id,
        workspace_id=workspace_id,
        property_id=property_id,
        ical_feed_id=ical_feed_id,
        external_uid=external_uid,
        check_in=_CHECK_IN,
        check_out=_CHECK_OUT,
        status=status,
        source=source,
        created_at=_PINNED,
    )
    session.add(res)
    session.flush()
    return res


class TestMigrationShape:
    """The migration lands all three tables with correct keys + indexes."""

    def test_all_tables_exist(self, engine: Engine) -> None:
        tables = set(inspect(engine).get_table_names())
        for table in _STAYS_TABLES:
            assert table in tables, f"{table} missing from schema"

    def test_ical_feed_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("ical_feed")}
        expected = {
            "id",
            "workspace_id",
            "property_id",
            "url",
            "provider",
            "last_polled_at",
            "last_etag",
            "enabled",
            "created_at",
        }
        assert set(cols) == expected
        for nullable in ("last_polled_at", "last_etag"):
            assert cols[nullable]["nullable"] is True, f"{nullable} must be nullable"
        for notnull in expected - {"last_polled_at", "last_etag"}:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_ical_feed_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("ical_feed")
        }
        assert fks[("property_id",)]["referred_table"] == "property"
        assert fks[("property_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"

    def test_ical_feed_index(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("ical_feed")}
        assert "ix_ical_feed_workspace_property" in indexes
        assert indexes["ix_ical_feed_workspace_property"]["column_names"] == [
            "workspace_id",
            "property_id",
        ]

    def test_reservation_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("reservation")}
        expected = {
            "id",
            "workspace_id",
            "property_id",
            "ical_feed_id",
            "external_uid",
            "check_in",
            "check_out",
            "guest_name",
            "guest_count",
            "status",
            "source",
            "raw_summary",
            "raw_description",
            "created_at",
        }
        assert set(cols) == expected
        for nullable in (
            "ical_feed_id",
            "guest_name",
            "guest_count",
            "raw_summary",
            "raw_description",
        ):
            assert cols[nullable]["nullable"] is True, f"{nullable} must be nullable"

    def test_reservation_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("reservation")
        }
        # Property / workspace cascade on parent delete.
        assert fks[("property_id",)]["referred_table"] == "property"
        assert fks[("property_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        # ical_feed SET NULL so a reservation outlives the feed's deletion.
        assert fks[("ical_feed_id",)]["referred_table"] == "ical_feed"
        assert fks[("ical_feed_id",)]["options"].get("ondelete") == "SET NULL"

    def test_reservation_property_check_in_index(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("reservation")}
        assert "ix_reservation_property_check_in" in indexes
        assert indexes["ix_reservation_property_check_in"]["column_names"] == [
            "property_id",
            "check_in",
        ]

    def test_reservation_unique_feed_external_uid(self, engine: Engine) -> None:
        uniques = {
            u["name"]: u for u in inspect(engine).get_unique_constraints("reservation")
        }
        assert "uq_reservation_feed_external_uid" in uniques
        assert uniques["uq_reservation_feed_external_uid"]["column_names"] == [
            "ical_feed_id",
            "external_uid",
        ]

    def test_stay_bundle_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("stay_bundle")}
        expected = {
            "id",
            "workspace_id",
            "reservation_id",
            "kind",
            "tasks_json",
            "created_at",
        }
        assert set(cols) == expected
        for name in expected:
            assert cols[name]["nullable"] is False, f"{name} must be NOT NULL"

    def test_stay_bundle_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("stay_bundle")
        }
        assert fks[("reservation_id",)]["referred_table"] == "reservation"
        assert fks[("reservation_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"


class TestFullChainRoundTrip:
    """Insert the full feed → reservation → bundle chain."""

    def test_round_trip(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="stays-chain@example.com",
            display_name="StaysChain",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="stays-chain-ws",
            name="StaysChainWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPR")

        token = set_current(_ctx_for(ws, user.id))
        try:
            feed = _seed_feed(
                db_session,
                feed_id="01HWA00000000000000000FEDR",
                workspace_id=ws.id,
                property_id=prop.id,
            )
            res = _seed_reservation(
                db_session,
                reservation_id="01HWA00000000000000000RESR",
                workspace_id=ws.id,
                property_id=prop.id,
                ical_feed_id=feed.id,
                external_uid="hm-round-trip",
            )
            db_session.add(
                StayBundle(
                    id="01HWA00000000000000000STBR",
                    workspace_id=ws.id,
                    reservation_id=res.id,
                    kind="turnover",
                    tasks_json=[
                        {"template_id": "01HWA00000000000000000TPLR", "offset_hours": 0}
                    ],
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            loaded_feed = db_session.get(IcalFeed, feed.id)
            assert loaded_feed is not None
            assert loaded_feed.provider == "airbnb"
            assert loaded_feed.enabled is True

            loaded_res = db_session.get(Reservation, res.id)
            assert loaded_res is not None
            assert loaded_res.ical_feed_id == feed.id
            assert loaded_res.external_uid == "hm-round-trip"
            assert loaded_res.status == "scheduled"
            assert loaded_res.source == "ical"

            bundles = db_session.scalars(
                select(StayBundle).where(StayBundle.reservation_id == res.id)
            ).all()
            assert len(bundles) == 1
            assert bundles[0].kind == "turnover"
            assert bundles[0].tasks_json == [
                {"template_id": "01HWA00000000000000000TPLR", "offset_hours": 0}
            ]
        finally:
            reset_current(token)


class TestCheckConstraints:
    """CHECK constraints reject values outside the v1 enums."""

    def test_bogus_provider_rejected(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="bogus-provider@example.com",
            display_name="BogusProvider",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="bogus-provider-ws",
            name="BogusProviderWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPP")
        token = set_current(_ctx_for(ws, user.id))
        try:
            db_session.add(
                IcalFeed(
                    id="01HWA00000000000000000FEDP",
                    workspace_id=ws.id,
                    property_id=prop.id,
                    url="https://example.com/feed.ics",
                    provider="myspace",
                    enabled=True,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_bogus_status_rejected(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="bogus-status@example.com",
            display_name="BogusStatus",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="bogus-status-ws",
            name="BogusStatusWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPS")
        token = set_current(_ctx_for(ws, user.id))
        try:
            db_session.add(
                Reservation(
                    id="01HWA00000000000000000RESS",
                    workspace_id=ws.id,
                    property_id=prop.id,
                    external_uid="uid-bogus-status",
                    check_in=_CHECK_IN,
                    check_out=_CHECK_OUT,
                    status="checked_out",
                    source="ical",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_bogus_source_rejected(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="bogus-source@example.com",
            display_name="BogusSource",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="bogus-source-ws",
            name="BogusSourceWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPO")
        token = set_current(_ctx_for(ws, user.id))
        try:
            db_session.add(
                Reservation(
                    id="01HWA00000000000000000RESO",
                    workspace_id=ws.id,
                    property_id=prop.id,
                    external_uid="uid-bogus-source",
                    check_in=_CHECK_IN,
                    check_out=_CHECK_OUT,
                    status="scheduled",
                    source="whatsapp",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_bogus_kind_rejected(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="bogus-kind@example.com",
            display_name="BogusKind",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="bogus-kind-ws",
            name="BogusKindWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPK")
        token = set_current(_ctx_for(ws, user.id))
        try:
            res = _seed_reservation(
                db_session,
                reservation_id="01HWA00000000000000000RESK",
                workspace_id=ws.id,
                property_id=prop.id,
                external_uid="uid-bogus-kind",
            )
            db_session.add(
                StayBundle(
                    id="01HWA00000000000000000STBK",
                    workspace_id=ws.id,
                    reservation_id=res.id,
                    kind="spaceship",
                    tasks_json=[],
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_check_out_before_check_in_rejected(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="rev-stay@example.com",
            display_name="RevStay",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="rev-stay-ws",
            name="RevStayWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPZ")
        token = set_current(_ctx_for(ws, user.id))
        try:
            db_session.add(
                Reservation(
                    id="01HWA00000000000000000RESZ",
                    workspace_id=ws.id,
                    property_id=prop.id,
                    external_uid="uid-rev",
                    # Flip the window — should trip the CHECK.
                    check_in=_CHECK_OUT,
                    check_out=_CHECK_IN,
                    status="scheduled",
                    source="ical",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_check_out_equal_check_in_rejected(self, db_session: Session) -> None:
        """``check_out > check_in`` — zero-length stay is a data bug."""
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="eq-stay@example.com",
            display_name="EqStay",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="eq-stay-ws",
            name="EqStayWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPQ")
        token = set_current(_ctx_for(ws, user.id))
        try:
            db_session.add(
                Reservation(
                    id="01HWA00000000000000000RESQ",
                    workspace_id=ws.id,
                    property_id=prop.id,
                    external_uid="uid-eq",
                    check_in=_CHECK_IN,
                    check_out=_CHECK_IN,
                    status="scheduled",
                    source="ical",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)


class TestUniqueConstraints:
    """``(ical_feed_id, external_uid)`` is idempotent for re-polls."""

    def test_duplicate_feed_external_uid_rejected(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="dup-uid@example.com",
            display_name="DupUid",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="dup-uid-ws",
            name="DupUidWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPD")
        token = set_current(_ctx_for(ws, user.id))
        try:
            feed = _seed_feed(
                db_session,
                feed_id="01HWA00000000000000000FEDD",
                workspace_id=ws.id,
                property_id=prop.id,
            )
            _seed_reservation(
                db_session,
                reservation_id="01HWA00000000000000000RES1",
                workspace_id=ws.id,
                property_id=prop.id,
                ical_feed_id=feed.id,
                external_uid="hm-dup",
            )
            db_session.add(
                Reservation(
                    id="01HWA00000000000000000RES2",
                    workspace_id=ws.id,
                    property_id=prop.id,
                    ical_feed_id=feed.id,
                    external_uid="hm-dup",
                    check_in=_CHECK_IN,
                    check_out=_CHECK_OUT,
                    status="scheduled",
                    source="ical",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_same_uid_different_feeds_allowed(self, db_session: Session) -> None:
        """Two feeds may independently advertise the same UID.

        Airbnb and VRBO use different UID spaces; the uniqueness key
        is ``(ical_feed_id, external_uid)``, not ``external_uid``
        alone. Both inserts must succeed.
        """
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="uid-diff@example.com",
            display_name="UidDiff",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="uid-diff-ws",
            name="UidDiffWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPX")
        token = set_current(_ctx_for(ws, user.id))
        try:
            feed_a = _seed_feed(
                db_session,
                feed_id="01HWA00000000000000000FEDA",
                workspace_id=ws.id,
                property_id=prop.id,
                provider="airbnb",
            )
            feed_b = _seed_feed(
                db_session,
                feed_id="01HWA00000000000000000FEDB",
                workspace_id=ws.id,
                property_id=prop.id,
                provider="vrbo",
            )
            _seed_reservation(
                db_session,
                reservation_id="01HWA00000000000000000RESXA",
                workspace_id=ws.id,
                property_id=prop.id,
                ical_feed_id=feed_a.id,
                external_uid="shared-uid",
            )
            _seed_reservation(
                db_session,
                reservation_id="01HWA00000000000000000RESXB",
                workspace_id=ws.id,
                property_id=prop.id,
                ical_feed_id=feed_b.id,
                external_uid="shared-uid",
            )
            db_session.flush()

            rows = db_session.scalars(
                select(Reservation).where(Reservation.external_uid == "shared-uid")
            ).all()
            assert {r.ical_feed_id for r in rows} == {feed_a.id, feed_b.id}
        finally:
            reset_current(token)

    def test_null_feed_with_same_uid_does_not_collide(
        self, db_session: Session
    ) -> None:
        """Manual / API reservations (``ical_feed_id IS NULL``) never collide.

        Both Postgres and SQLite treat NULLs as distinct in unique
        indexes by default, so two manual reservations that happen to
        carry the same ``external_uid`` coexist. The models docstring
        and migration commentary document this explicitly as v1-slice
        behaviour; this test locks it in so a future switch to
        ``NULLS NOT DISTINCT`` (Postgres 15+) doesn't silently break
        the manual-entry workflow without us noticing.
        """
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="null-feed@example.com",
            display_name="NullFeed",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="null-feed-ws",
            name="NullFeedWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPW")
        token = set_current(_ctx_for(ws, user.id))
        try:
            _seed_reservation(
                db_session,
                reservation_id="01HWA00000000000000000RESW1",
                workspace_id=ws.id,
                property_id=prop.id,
                ical_feed_id=None,
                external_uid="manual-uid",
                source="manual",
            )
            _seed_reservation(
                db_session,
                reservation_id="01HWA00000000000000000RESW2",
                workspace_id=ws.id,
                property_id=prop.id,
                ical_feed_id=None,
                external_uid="manual-uid",
                source="manual",
            )
            db_session.flush()

            rows = db_session.scalars(
                select(Reservation).where(Reservation.external_uid == "manual-uid")
            ).all()
            assert len(rows) == 2
            assert all(r.ical_feed_id is None for r in rows)
        finally:
            reset_current(token)


class TestCascadeAndSetNull:
    """FK cascade / SET NULL behaviour on parent deletion."""

    def test_delete_property_cascades_feed_and_reservation(
        self, db_session: Session
    ) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="cascade-prop-stays@example.com",
            display_name="CascadePropStays",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="cascade-prop-stays-ws",
            name="CascadePropStaysWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPC")
        token = set_current(_ctx_for(ws, user.id))
        try:
            feed = _seed_feed(
                db_session,
                feed_id="01HWA00000000000000000FEDC",
                workspace_id=ws.id,
                property_id=prop.id,
            )
            _seed_reservation(
                db_session,
                reservation_id="01HWA00000000000000000RESC",
                workspace_id=ws.id,
                property_id=prop.id,
                ical_feed_id=feed.id,
                external_uid="uid-cascade",
            )
            db_session.flush()
        finally:
            reset_current(token)

        # justification: property is tenant-agnostic; deleting it is a
        # platform-level op, not scoped to any workspace.
        loaded = db_session.get(Property, prop.id)
        assert loaded is not None
        with tenant_agnostic():
            db_session.delete(loaded)
            db_session.flush()

        token = set_current(_ctx_for(ws, user.id))
        try:
            assert (
                db_session.scalars(
                    select(IcalFeed).where(IcalFeed.property_id == prop.id)
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(Reservation).where(Reservation.property_id == prop.id)
                ).all()
                == []
            )
        finally:
            reset_current(token)

    def test_delete_reservation_cascades_stay_bundle(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="cascade-res@example.com",
            display_name="CascadeRes",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="cascade-res-ws",
            name="CascadeResWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPY")
        token = set_current(_ctx_for(ws, user.id))
        try:
            res = _seed_reservation(
                db_session,
                reservation_id="01HWA00000000000000000RESY",
                workspace_id=ws.id,
                property_id=prop.id,
                external_uid="uid-cascade-res",
                source="manual",
            )
            db_session.add(
                StayBundle(
                    id="01HWA00000000000000000STBY",
                    workspace_id=ws.id,
                    reservation_id=res.id,
                    kind="turnover",
                    tasks_json=[],
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            db_session.delete(res)
            db_session.flush()

            assert (
                db_session.scalars(
                    select(StayBundle).where(StayBundle.reservation_id == res.id)
                ).all()
                == []
            )
        finally:
            reset_current(token)

    def test_delete_ical_feed_sets_reservation_feed_null(
        self, db_session: Session
    ) -> None:
        """Deleting a feed nulls ``reservation.ical_feed_id`` but keeps the row."""
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="setnull-feed@example.com",
            display_name="SetNullFeed",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="setnull-feed-ws",
            name="SetNullFeedWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPN")
        token = set_current(_ctx_for(ws, user.id))
        try:
            feed = _seed_feed(
                db_session,
                feed_id="01HWA00000000000000000FEDN",
                workspace_id=ws.id,
                property_id=prop.id,
            )
            _seed_reservation(
                db_session,
                reservation_id="01HWA00000000000000000RESN",
                workspace_id=ws.id,
                property_id=prop.id,
                ical_feed_id=feed.id,
                external_uid="uid-setnull",
            )
            db_session.flush()

            db_session.delete(feed)
            db_session.flush()
            db_session.expire_all()

            survivor = db_session.get(Reservation, "01HWA00000000000000000RESN")
            assert survivor is not None
            assert survivor.ical_feed_id is None
        finally:
            reset_current(token)


class TestTenantFilter:
    """All three stays tables are workspace-scoped under the filter."""

    @pytest.mark.parametrize("model", [IcalFeed, Reservation, StayBundle])
    def test_read_without_ctx_raises(
        self,
        filtered_factory: sessionmaker[Session],
        model: type[IcalFeed] | type[Reservation] | type[StayBundle],
    ) -> None:
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(model))
        assert exc.value.table == model.__tablename__
