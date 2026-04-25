"""Unit tests for :class:`UserWeeklyAvailability` (cd-l2r9).

Covers the SQLAlchemy mapped class from
:mod:`app.adapters.db.availability.models`:

* construction defaults (``starts_local`` / ``ends_local`` default to
  ``None`` for the "off that day" pair);
* tablename + ``__table_args__`` shape (CHECKs, UNIQUE, hot-path
  index, registry membership);
* in-memory SQLite round-trip including the BOTH-OR-NEITHER hours
  pairing CHECK (both half-set shapes rejected), the weekday range
  CHECK (``-1`` and ``7`` rejected), and the
  ``UNIQUE(workspace_id, user_id, weekday)`` constraint (two rows
  for the same triple rejected);
* cross-workspace insert isolation;
* workspace hard-delete cascade on SQLite.

Integration coverage (FK cascade on PG, schema fingerprint parity,
tenant-filter behaviour) is delegated to
:mod:`tests.integration.test_migration_cd_l2r9` and
:mod:`tests.integration.test_schema_parity`.

See ``docs/specs/06-tasks-and-scheduling.md`` §"Weekly availability".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, time

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

from app.adapters.db.availability import UserWeeklyAvailability
from app.adapters.db.base import Base
from app.adapters.db.workspace import Workspace
from app.tenancy import registry

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_T_0900 = time(9, 0)
_T_1700 = time(17, 0)


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models``."""
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
    """In-memory SQLite engine with every ORM table created."""
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
    """In-memory SQLite engine with FKs enforced (cascade-test only)."""
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


def _pattern(
    *,
    id: str,
    workspace_id: str,
    user_id: str,
    weekday: int = 0,
    starts_local: time | None = _T_0900,
    ends_local: time | None = _T_1700,
) -> UserWeeklyAvailability:
    return UserWeeklyAvailability(
        id=id,
        workspace_id=workspace_id,
        user_id=user_id,
        weekday=weekday,
        starts_local=starts_local,
        ends_local=ends_local,
        updated_at=_PINNED,
    )


# ---------------------------------------------------------------------------
# Pure-Python construction shape
# ---------------------------------------------------------------------------


class TestUserWeeklyAvailabilityModelShape:
    """The mapped class carries the cd-l2r9 v1 slice."""

    def test_minimal_construction(self) -> None:
        row = UserWeeklyAvailability(
            id="01HWA00000000000000000WK01",
            workspace_id="01HWA00000000000000000WSPA",
            user_id="01HWA00000000000000000USRA",
            weekday=2,
            updated_at=_PINNED,
        )
        assert row.id == "01HWA00000000000000000WK01"
        assert row.workspace_id == "01HWA00000000000000000WSPA"
        assert row.user_id == "01HWA00000000000000000USRA"
        assert row.weekday == 2
        # The "off that day" default — both null.
        assert row.starts_local is None
        assert row.ends_local is None

    def test_tablename(self) -> None:
        assert UserWeeklyAvailability.__tablename__ == "user_weekly_availability"

    def test_weekday_range_check_present(self) -> None:
        checks = [
            c
            for c in UserWeeklyAvailability.__table_args__
            if isinstance(c, CheckConstraint)
        ]
        assert "ck_user_weekly_availability_weekday_range" in {c.name for c in checks}

    def test_hours_pairing_check_present(self) -> None:
        checks = [
            c
            for c in UserWeeklyAvailability.__table_args__
            if isinstance(c, CheckConstraint)
        ]
        assert "ck_user_weekly_availability_hours_pairing" in {c.name for c in checks}

    def test_unique_constraint_present(self) -> None:
        """``UNIQUE(workspace_id, user_id, weekday)`` on the live triple."""
        uniques = [
            c
            for c in UserWeeklyAvailability.__table_args__
            if isinstance(c, UniqueConstraint)
        ]
        target = next(
            c for c in uniques if c.name == "uq_user_weekly_availability_user_weekday"
        )
        assert [col.name for col in target.columns] == [
            "workspace_id",
            "user_id",
            "weekday",
        ]

    def test_hot_path_index_present(self) -> None:
        indexes: dict[str, list[str]] = {
            str(i.name): [c.name for c in i.columns]
            for i in UserWeeklyAvailability.__table_args__
            if isinstance(i, Index)
        }
        assert indexes["ix_user_weekly_availability_workspace_user"] == [
            "workspace_id",
            "user_id",
        ]


class TestRegistryMembership:
    def test_user_weekly_availability_registered(self) -> None:
        assert registry.is_scoped("user_weekly_availability")


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestUserWeeklyAvailabilityRoundTrip:
    def test_insert_then_read_back(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPA", "alpha")
        session.add(
            _pattern(
                id="01HWA00000000000000000WK02",
                workspace_id="01HWA00000000000000000WSPA",
                user_id="01HWA00000000000000000USRA",
                weekday=3,
            )
        )
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(UserWeeklyAvailability).where(
                UserWeeklyAvailability.id == "01HWA00000000000000000WK02"
            )
        ).one()
        assert loaded.weekday == 3
        assert loaded.starts_local == _T_0900
        assert loaded.ends_local == _T_1700

    def test_off_that_day_round_trip(self, session: Session) -> None:
        """A ``starts_local IS NULL AND ends_local IS NULL`` row is the
        "off that day" pattern — must be accepted and round-trip cleanly."""
        _seed_workspace(session, "01HWA00000000000000000WSPO", "off-day")
        session.add(
            _pattern(
                id="01HWA00000000000000000WK03",
                workspace_id="01HWA00000000000000000WSPO",
                user_id="01HWA00000000000000000USRO",
                weekday=6,
                starts_local=None,
                ends_local=None,
            )
        )
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(UserWeeklyAvailability).where(
                UserWeeklyAvailability.id == "01HWA00000000000000000WK03"
            )
        ).one()
        assert loaded.starts_local is None
        assert loaded.ends_local is None


# ---------------------------------------------------------------------------
# Weekday range CHECK
# ---------------------------------------------------------------------------


class TestWeekdayRangeCheck:
    """``weekday >= 0 AND weekday <= 6`` rejects out-of-range values."""

    @pytest.mark.parametrize("bad_value", [-1, 7, 100])
    def test_out_of_range_rejected(self, session: Session, bad_value: int) -> None:
        _seed_workspace(
            session,
            f"01HWA0000000000000000WS{bad_value:>3d}".replace(" ", "0")[:26],
            f"wd-{bad_value}",
        )
        session.add(
            _pattern(
                id=f"01HWA00000000000000000WK{bad_value:>2d}".replace(" ", "0")[:26],
                workspace_id=f"01HWA0000000000000000WS{bad_value:>3d}".replace(
                    " ", "0"
                )[:26],
                user_id="01HWA00000000000000000USWD",
                weekday=bad_value,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    @pytest.mark.parametrize("weekday", [0, 1, 2, 3, 4, 5, 6])
    def test_in_range_accepted(self, session: Session, weekday: int) -> None:
        ws_id = f"01HWA0000000000000000WS{weekday:02d}"
        _seed_workspace(session, ws_id, f"wd-{weekday}")
        session.add(
            _pattern(
                id=f"01HWA0000000000000000WK1{weekday}",
                workspace_id=ws_id,
                user_id="01HWA00000000000000000USOK",
                weekday=weekday,
            )
        )
        session.flush()  # No IntegrityError — every ISO weekday valid.


# ---------------------------------------------------------------------------
# BOTH-OR-NEITHER hours pairing CHECK
# ---------------------------------------------------------------------------


class TestHoursPairingCheck:
    """The biconditional CHECK on ``(starts_local, ends_local)``."""

    def test_starts_only_rejected(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSP1", "starts-only")
        session.add(
            _pattern(
                id="01HWA00000000000000000WK04",
                workspace_id="01HWA00000000000000000WSP1",
                user_id="01HWA00000000000000000US01",
                starts_local=_T_0900,
                ends_local=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_ends_only_rejected(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSP2", "ends-only")
        session.add(
            _pattern(
                id="01HWA00000000000000000WK05",
                workspace_id="01HWA00000000000000000WSP2",
                user_id="01HWA00000000000000000US02",
                starts_local=None,
                ends_local=_T_1700,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_both_set_accepted(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSP3", "both-set")
        session.add(
            _pattern(
                id="01HWA00000000000000000WK06",
                workspace_id="01HWA00000000000000000WSP3",
                user_id="01HWA00000000000000000US03",
                starts_local=_T_0900,
                ends_local=_T_1700,
            )
        )
        session.flush()  # No IntegrityError — happy path.

    def test_both_null_accepted(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSP4", "both-null")
        session.add(
            _pattern(
                id="01HWA00000000000000000WK07",
                workspace_id="01HWA00000000000000000WSP4",
                user_id="01HWA00000000000000000US04",
                starts_local=None,
                ends_local=None,
            )
        )
        session.flush()  # No IntegrityError — "off that day".


# ---------------------------------------------------------------------------
# UNIQUE(workspace_id, user_id, weekday)
# ---------------------------------------------------------------------------


class TestUniqueWorkspaceUserWeekday:
    """One live row per (workspace, user, weekday)."""

    def test_two_rows_same_triple_rejected(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPU", "uniq-trip")
        session.add(
            _pattern(
                id="01HWA00000000000000000WK08",
                workspace_id="01HWA00000000000000000WSPU",
                user_id="01HWA00000000000000000USUQ",
                weekday=2,
            )
        )
        session.flush()

        session.add(
            _pattern(
                id="01HWA00000000000000000WK09",
                workspace_id="01HWA00000000000000000WSPU",
                user_id="01HWA00000000000000000USUQ",
                weekday=2,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_same_user_different_weekday_accepted(self, session: Session) -> None:
        """A user has up to seven rows, one per weekday."""
        _seed_workspace(session, "01HWA00000000000000000WSPV", "full-week")
        for wd in range(7):
            session.add(
                _pattern(
                    id=f"01HWA0000000000000000WKDV{wd}",
                    workspace_id="01HWA00000000000000000WSPV",
                    user_id="01HWA00000000000000000USVU",
                    weekday=wd,
                )
            )
        session.flush()  # No IntegrityError — the canonical seven rows.

    def test_same_weekday_different_user_accepted(self, session: Session) -> None:
        """Two users may share a weekday — uniqueness is per-user."""
        _seed_workspace(session, "01HWA00000000000000000WSPX", "two-users")
        session.add(
            _pattern(
                id="01HWA00000000000000000WK0X",
                workspace_id="01HWA00000000000000000WSPX",
                user_id="01HWA00000000000000000USXA",
                weekday=1,
            )
        )
        session.add(
            _pattern(
                id="01HWA00000000000000000WK0Y",
                workspace_id="01HWA00000000000000000WSPX",
                user_id="01HWA00000000000000000USXB",
                weekday=1,
            )
        )
        session.flush()  # No IntegrityError — two users share Tuesday.


# ---------------------------------------------------------------------------
# Cross-workspace isolation
# ---------------------------------------------------------------------------


class TestCrossWorkspaceIsolation:
    def test_b_row_invisible_under_a_filter(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSAA", "iso-a")
        _seed_workspace(session, "01HWA00000000000000000WSBB", "iso-b")
        session.add(
            _pattern(
                id="01HWA00000000000000000WKAA",
                workspace_id="01HWA00000000000000000WSAA",
                user_id="01HWA00000000000000000USHR",
                weekday=0,
            )
        )
        session.add(
            _pattern(
                id="01HWA00000000000000000WKBB",
                workspace_id="01HWA00000000000000000WSBB",
                user_id="01HWA00000000000000000USHR",
                weekday=0,
            )
        )
        session.flush()
        session.expire_all()

        rows_a = session.scalars(
            select(UserWeeklyAvailability).where(
                UserWeeklyAvailability.workspace_id == "01HWA00000000000000000WSAA"
            )
        ).all()
        assert {r.id for r in rows_a} == {"01HWA00000000000000000WKAA"}, (
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
            _seed_workspace(session, "01HWA00000000000000000WSCC", "cascade-wa")
            session.add(
                _pattern(
                    id="01HWA00000000000000000WKCC",
                    workspace_id="01HWA00000000000000000WSCC",
                    user_id="01HWA00000000000000000USCC",
                    weekday=4,
                )
            )
            session.flush()

            ws = session.get(Workspace, "01HWA00000000000000000WSCC")
            assert ws is not None
            session.delete(ws)
            session.flush()
            session.expire_all()

            remaining = session.scalars(
                select(UserWeeklyAvailability).where(
                    UserWeeklyAvailability.workspace_id == "01HWA00000000000000000WSCC"
                )
            ).all()
            assert remaining == [], (
                "user_weekly_availability rows survived a workspace "
                "hard-delete — FK ON DELETE CASCADE is not firing"
            )
