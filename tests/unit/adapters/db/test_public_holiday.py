"""Unit tests for :class:`PublicHoliday` (cd-l2r9).

Covers the SQLAlchemy mapped class from
:mod:`app.adapters.db.holidays.models`:

* construction defaults (``country`` / ``reduced_starts_local`` /
  ``reduced_ends_local`` / ``payroll_multiplier`` / ``recurrence`` /
  ``notes_md`` / ``deleted_at`` default to ``None``);
* tablename + ``__table_args__`` shape (CHECKs, UNIQUE, hot-path
  indexes, registry membership);
* in-memory SQLite round-trip including the ``scheduling_effect``
  enum CHECK, the ``recurrence`` enum CHECK (NULL or ``annual``),
  the reduced-hours pairing CHECK, the ``payroll_multiplier``
  Decimal round-trip, and the soft-delete tombstone round-trip;
* ``UNIQUE(workspace_id, date, country)`` rejection on a duplicate
  triple plus acceptance of overrides on different
  workspace/date/country combinations;
* cross-workspace insert isolation;
* workspace hard-delete cascade on SQLite.

Integration coverage (FK cascade on PG, schema fingerprint parity,
tenant-filter behaviour) is delegated to
:mod:`tests.integration.test_migration_cd_l2r9` and
:mod:`tests.integration.test_schema_parity`.

See ``docs/specs/06-tasks-and-scheduling.md`` §"public_holidays".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy import (
    CheckConstraint,
    Engine,
    Index,
    UniqueConstraint,
    create_engine,
    event,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.base import Base
from app.adapters.db.holidays import PublicHoliday
from app.adapters.db.workspace import Workspace
from app.tenancy import registry

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_XMAS = date(2026, 12, 25)
_NYE = date(2026, 12, 31)
_T_0900 = time(9, 0)
_T_1300 = time(13, 0)


def _load_all_models() -> None:
    import importlib
    import pkgutil

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


@pytest.fixture
def engine() -> Iterator[Engine]:
    _load_all_models()
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def fk_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(
        dbapi_connection: object, _connection_record: object
    ) -> None:
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    s = factory()
    try:
        yield s
    finally:
        s.close()


def _seed_workspace(session: Session, workspace_id: str, slug: str) -> Workspace:
    ws = Workspace(
        id=workspace_id,
        slug=slug,
        name=slug.title(),
        plan="free",
        quota_json={},
        created_at=_PINNED,
    )
    session.add(ws)
    session.flush()
    return ws


def _holiday(
    *,
    id: str,
    workspace_id: str,
    name: str = "Christmas Day",
    date: date = _XMAS,
    country: str | None = None,
    scheduling_effect: str = "block",
    reduced_starts_local: time | None = None,
    reduced_ends_local: time | None = None,
    payroll_multiplier: Decimal | None = None,
    recurrence: str | None = None,
    notes_md: str | None = None,
    deleted_at: datetime | None = None,
) -> PublicHoliday:
    return PublicHoliday(
        id=id,
        workspace_id=workspace_id,
        name=name,
        date=date,
        country=country,
        scheduling_effect=scheduling_effect,
        reduced_starts_local=reduced_starts_local,
        reduced_ends_local=reduced_ends_local,
        payroll_multiplier=payroll_multiplier,
        recurrence=recurrence,
        notes_md=notes_md,
        created_at=_PINNED,
        updated_at=_PINNED,
        deleted_at=deleted_at,
    )


# ---------------------------------------------------------------------------
# Pure-Python construction shape
# ---------------------------------------------------------------------------


class TestModelShape:
    def test_minimal_construction(self) -> None:
        row = PublicHoliday(
            id="01HWA00000000000000000PH01",
            workspace_id="01HWA00000000000000000WSPA",
            name="Christmas Day",
            date=_XMAS,
            scheduling_effect="block",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        assert row.id == "01HWA00000000000000000PH01"
        assert row.workspace_id == "01HWA00000000000000000WSPA"
        assert row.name == "Christmas Day"
        assert row.date == _XMAS
        assert row.scheduling_effect == "block"
        assert row.country is None
        assert row.reduced_starts_local is None
        assert row.reduced_ends_local is None
        assert row.payroll_multiplier is None
        assert row.recurrence is None
        assert row.notes_md is None
        assert row.deleted_at is None

    def test_tablename(self) -> None:
        assert PublicHoliday.__tablename__ == "public_holiday"

    def test_scheduling_effect_check_present(self) -> None:
        checks = [
            c for c in PublicHoliday.__table_args__ if isinstance(c, CheckConstraint)
        ]
        assert "ck_public_holiday_scheduling_effect" in {c.name for c in checks}

    def test_recurrence_check_present(self) -> None:
        checks = [
            c for c in PublicHoliday.__table_args__ if isinstance(c, CheckConstraint)
        ]
        assert "ck_public_holiday_recurrence" in {c.name for c in checks}

    def test_reduced_hours_pairing_check_present(self) -> None:
        checks = [
            c for c in PublicHoliday.__table_args__ if isinstance(c, CheckConstraint)
        ]
        assert "ck_public_holiday_reduced_hours_pairing" in {c.name for c in checks}

    def test_unique_constraint_present(self) -> None:
        uniques = [
            c for c in PublicHoliday.__table_args__ if isinstance(c, UniqueConstraint)
        ]
        target = next(
            c for c in uniques if c.name == "uq_public_holiday_workspace_date_country"
        )
        assert [col.name for col in target.columns] == [
            "workspace_id",
            "date",
            "country",
        ]

    def test_hot_path_indexes_present(self) -> None:
        indexes: dict[str, list[str]] = {
            str(i.name): [c.name for c in i.columns]
            for i in PublicHoliday.__table_args__
            if isinstance(i, Index)
        }
        assert indexes["ix_public_holiday_workspace_date"] == [
            "workspace_id",
            "date",
        ]
        assert indexes["ix_public_holiday_workspace_deleted"] == [
            "workspace_id",
            "deleted_at",
        ]


class TestRegistryMembership:
    """``public_holiday`` is registered as scoped.

    See :class:`tests.unit.adapters.db.test_user_leave.TestRegistryMembership`
    for the rationale behind calling :func:`registry.register` directly
    rather than asserting the import-time side effect.
    """

    def test_public_holiday_registered(self) -> None:
        registry.register("public_holiday")
        assert registry.is_scoped("public_holiday")


# ---------------------------------------------------------------------------
# Idempotent re-import
# ---------------------------------------------------------------------------


class TestModuleReimportIdempotent:
    def test_reimport_does_not_raise(self) -> None:
        import importlib

        import app.adapters.db.holidays as hol_pkg

        importlib.reload(hol_pkg)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_block_holiday_round_trip(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPA", "alpha")
        session.add(
            _holiday(
                id="01HWA00000000000000000PH02",
                workspace_id="01HWA00000000000000000WSPA",
                country="FR",
                scheduling_effect="block",
                payroll_multiplier=Decimal("2.00"),
                recurrence="annual",
                notes_md="Public holiday — France",
            )
        )
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(PublicHoliday).where(
                PublicHoliday.id == "01HWA00000000000000000PH02"
            )
        ).one()
        assert loaded.scheduling_effect == "block"
        assert loaded.country == "FR"
        # SQLite renders Numeric as TEXT but round-trips via Decimal.
        assert loaded.payroll_multiplier == Decimal("2.00")
        assert loaded.recurrence == "annual"

    def test_reduced_holiday_round_trip(self, session: Session) -> None:
        """A ``scheduling_effect = reduced`` row carries reduced hours."""
        _seed_workspace(session, "01HWA00000000000000000WSPB", "reduced")
        session.add(
            _holiday(
                id="01HWA00000000000000000PH03",
                workspace_id="01HWA00000000000000000WSPB",
                name="Christmas Eve",
                date=date(2026, 12, 24),
                scheduling_effect="reduced",
                reduced_starts_local=_T_0900,
                reduced_ends_local=_T_1300,
            )
        )
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(PublicHoliday).where(
                PublicHoliday.id == "01HWA00000000000000000PH03"
            )
        ).one()
        assert loaded.scheduling_effect == "reduced"
        assert loaded.reduced_starts_local == _T_0900
        assert loaded.reduced_ends_local == _T_1300

    def test_allow_holiday_round_trip(self, session: Session) -> None:
        """``allow`` is a payroll-only marker — no scheduling impact."""
        _seed_workspace(session, "01HWA00000000000000000WSPC", "allow")
        session.add(
            _holiday(
                id="01HWA00000000000000000PH04",
                workspace_id="01HWA00000000000000000WSPC",
                name="Labour Day",
                date=date(2026, 5, 1),
                scheduling_effect="allow",
                payroll_multiplier=Decimal("1.50"),
            )
        )
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(PublicHoliday).where(
                PublicHoliday.id == "01HWA00000000000000000PH04"
            )
        ).one()
        assert loaded.scheduling_effect == "allow"
        assert loaded.payroll_multiplier == Decimal("1.50")
        assert loaded.reduced_starts_local is None
        assert loaded.reduced_ends_local is None


# ---------------------------------------------------------------------------
# scheduling_effect CHECK
# ---------------------------------------------------------------------------


class TestSchedulingEffectCheck:
    @pytest.mark.parametrize("bad_value", ["BLOCK", "deny", "soft", ""])
    def test_unknown_effect_rejected(self, session: Session, bad_value: str) -> None:
        slug = f"se-{bad_value or 'empty'}"
        suffix = f"{abs(hash(bad_value)) % 1000:03d}"
        ws_id = f"01HWA0000000000000000WS{suffix}"
        _seed_workspace(session, ws_id, slug)
        session.add(
            _holiday(
                id=f"01HWA000000000000000PHX{suffix}",
                workspace_id=ws_id,
                scheduling_effect=bad_value,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


# ---------------------------------------------------------------------------
# recurrence CHECK
# ---------------------------------------------------------------------------


class TestRecurrenceCheck:
    @pytest.mark.parametrize("bad_value", ["weekly", "monthly", "ANNUAL"])
    def test_unknown_recurrence_rejected(
        self, session: Session, bad_value: str
    ) -> None:
        _seed_workspace(
            session,
            f"01HWA0000000000000000WSR{abs(hash(bad_value)) % 1000:03d}",
            f"rec-{bad_value}",
        )
        session.add(
            _holiday(
                id=f"01HWA000000000000PHRC{abs(hash(bad_value)) % 1000:03d}",
                workspace_id=(
                    f"01HWA0000000000000000WSR{abs(hash(bad_value)) % 1000:03d}"
                ),
                recurrence=bad_value,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_null_recurrence_accepted(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSP5", "one-off")
        session.add(
            _holiday(
                id="01HWA00000000000000000PH05",
                workspace_id="01HWA00000000000000000WSP5",
                recurrence=None,
            )
        )
        session.flush()  # No IntegrityError — NULL is the one-off marker.

    def test_annual_recurrence_accepted(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSP6", "annual")
        session.add(
            _holiday(
                id="01HWA00000000000000000PH06",
                workspace_id="01HWA00000000000000000WSP6",
                recurrence="annual",
            )
        )
        session.flush()  # No IntegrityError — ``annual`` is the v1 enum.


# ---------------------------------------------------------------------------
# Reduced-hours pairing CHECK
# ---------------------------------------------------------------------------


class TestReducedHoursPairingCheck:
    """The biconditional CHECK on ``scheduling_effect = 'reduced'``."""

    def test_reduced_without_hours_rejected(self, session: Session) -> None:
        """A ``reduced`` row without reduced hours is half-wired."""
        _seed_workspace(session, "01HWA00000000000000000WSP7", "red-no-hrs")
        session.add(
            _holiday(
                id="01HWA00000000000000000PH07",
                workspace_id="01HWA00000000000000000WSP7",
                scheduling_effect="reduced",
                reduced_starts_local=None,
                reduced_ends_local=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_block_with_reduced_hours_rejected(self, session: Session) -> None:
        """A non-``reduced`` row carrying reduced hours is a UX bug."""
        _seed_workspace(session, "01HWA00000000000000000WSP8", "blk-w-hrs")
        session.add(
            _holiday(
                id="01HWA00000000000000000PH08",
                workspace_id="01HWA00000000000000000WSP8",
                scheduling_effect="block",
                reduced_starts_local=_T_0900,
                reduced_ends_local=_T_1300,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_reduced_with_hours_accepted(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSP9", "red-ok")
        session.add(
            _holiday(
                id="01HWA00000000000000000PH09",
                workspace_id="01HWA00000000000000000WSP9",
                scheduling_effect="reduced",
                reduced_starts_local=_T_0900,
                reduced_ends_local=_T_1300,
            )
        )
        session.flush()  # No IntegrityError — happy path.

    def test_reduced_starts_only_rejected(self, session: Session) -> None:
        """Half-set reduced hours fail the biconditional."""
        _seed_workspace(session, "01HWA00000000000000000WSPK", "red-half")
        session.add(
            _holiday(
                id="01HWA00000000000000000PH0K",
                workspace_id="01HWA00000000000000000WSPK",
                scheduling_effect="reduced",
                reduced_starts_local=_T_0900,
                reduced_ends_local=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


# ---------------------------------------------------------------------------
# UNIQUE(workspace_id, date, country)
# ---------------------------------------------------------------------------


class TestUniqueWorkspaceDateCountry:
    """One holiday per (workspace, date, country)."""

    def test_two_rows_same_triple_rejected(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPU", "uniq-trip")
        session.add(
            _holiday(
                id="01HWA00000000000000000PH0U",
                workspace_id="01HWA00000000000000000WSPU",
                date=_XMAS,
                country="FR",
            )
        )
        session.flush()

        session.add(
            _holiday(
                id="01HWA00000000000000000PH0V",
                workspace_id="01HWA00000000000000000WSPU",
                date=_XMAS,
                country="FR",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_same_date_different_country_accepted(self, session: Session) -> None:
        """France's Xmas + Italy's Xmas are distinct rows."""
        _seed_workspace(session, "01HWA00000000000000000WSPW", "two-countries")
        session.add(
            _holiday(
                id="01HWA00000000000000000PH0W",
                workspace_id="01HWA00000000000000000WSPW",
                date=_XMAS,
                country="FR",
            )
        )
        session.add(
            _holiday(
                id="01HWA00000000000000000PH0X",
                workspace_id="01HWA00000000000000000WSPW",
                date=_XMAS,
                country="IT",
            )
        )
        session.flush()  # No IntegrityError — country distinguishes.

    def test_same_country_different_date_accepted(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPY", "two-dates")
        session.add(
            _holiday(
                id="01HWA00000000000000000PH0Y",
                workspace_id="01HWA00000000000000000WSPY",
                date=_XMAS,
                country="FR",
            )
        )
        session.add(
            _holiday(
                id="01HWA00000000000000000PH0Z",
                workspace_id="01HWA00000000000000000WSPY",
                date=_NYE,
                country="FR",
            )
        )
        session.flush()  # No IntegrityError — distinct dates.


# ---------------------------------------------------------------------------
# Soft-delete tombstone
# ---------------------------------------------------------------------------


class TestSoftDeleteTombstone:
    def test_tombstone_round_trip(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPS", "soft")
        row = _holiday(
            id="01HWA00000000000000000PH0S",
            workspace_id="01HWA00000000000000000WSPS",
        )
        session.add(row)
        session.flush()

        row.deleted_at = _LATER
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(PublicHoliday).where(
                PublicHoliday.id == "01HWA00000000000000000PH0S"
            )
        ).one()
        assert loaded.deleted_at is not None
        assert loaded.deleted_at.replace(tzinfo=None) == _LATER.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Cross-workspace isolation
# ---------------------------------------------------------------------------


class TestCrossWorkspaceIsolation:
    def test_b_row_invisible_under_a_filter(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSAA", "iso-a")
        _seed_workspace(session, "01HWA00000000000000000WSBB", "iso-b")
        session.add(
            _holiday(
                id="01HWA00000000000000000PHAA",
                workspace_id="01HWA00000000000000000WSAA",
                country="FR",
            )
        )
        session.add(
            _holiday(
                id="01HWA00000000000000000PHBB",
                workspace_id="01HWA00000000000000000WSBB",
                country="FR",
            )
        )
        session.flush()
        session.expire_all()

        rows_a = session.scalars(
            select(PublicHoliday).where(
                PublicHoliday.workspace_id == "01HWA00000000000000000WSAA"
            )
        ).all()
        assert {r.id for r in rows_a} == {"01HWA00000000000000000PHAA"}, (
            "A-scoped SELECT returned a B-owned row — workspace_id is "
            "not discriminating correctly"
        )


# ---------------------------------------------------------------------------
# FK cascade on SQLite
# ---------------------------------------------------------------------------


class TestWorkspaceCascade:
    def test_workspace_delete_cascades(self, fk_engine: Engine) -> None:
        factory = sessionmaker(bind=fk_engine, expire_on_commit=False, class_=Session)
        with factory() as session:
            _seed_workspace(session, "01HWA00000000000000000WSCC", "cascade-ph")
            session.add(
                _holiday(
                    id="01HWA00000000000000000PHCC",
                    workspace_id="01HWA00000000000000000WSCC",
                )
            )
            session.flush()

            ws = session.get(Workspace, "01HWA00000000000000000WSCC")
            assert ws is not None
            session.delete(ws)
            session.flush()
            session.expire_all()

            remaining = session.scalars(
                select(PublicHoliday).where(
                    PublicHoliday.workspace_id == "01HWA00000000000000000WSCC"
                )
            ).all()
            assert remaining == [], (
                "public_holiday rows survived a workspace hard-delete "
                "— FK ON DELETE CASCADE is not firing"
            )
