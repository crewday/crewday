"""Unit tests for :class:`UserAvailabilityOverride` (cd-l2r9).

Covers the SQLAlchemy mapped class from
:mod:`app.adapters.db.availability.models`:

* construction defaults (``starts_local`` / ``ends_local`` / ``reason``
  / ``approved_at`` / ``approved_by`` / ``deleted_at`` default to
  ``None``);
* tablename + ``__table_args__`` shape (CHECK, UNIQUE, hot-path
  index, registry membership);
* in-memory SQLite round-trip including the BOTH-OR-NEITHER hours
  pairing CHECK, the soft-delete tombstone round-trip, and the
  ``approval_required`` round-trip;
* ``UNIQUE(workspace_id, user_id, date)`` rejection on a duplicate
  triple plus acceptance of overrides on different dates / users;
* cross-workspace insert isolation;
* workspace hard-delete cascade on SQLite.

Integration coverage (FK cascade on PG, schema fingerprint parity,
tenant-filter behaviour) is delegated to
:mod:`tests.integration.test_migration_cd_l2r9` and
:mod:`tests.integration.test_schema_parity`.

See ``docs/specs/06-tasks-and-scheduling.md`` §"user_availability_overrides".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, time

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

from app.adapters.db.availability import UserAvailabilityOverride
from app.adapters.db.base import Base
from app.adapters.db.workspace import Workspace
from app.tenancy import registry

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_TODAY = date(2026, 4, 25)
_TOMORROW = date(2026, 4, 26)
_T_0900 = time(9, 0)
_T_1700 = time(17, 0)


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


def _override(
    *,
    id: str,
    workspace_id: str,
    user_id: str,
    date: date = _TODAY,
    available: bool = True,
    starts_local: time | None = None,
    ends_local: time | None = None,
    reason: str | None = None,
    approval_required: bool = False,
    approved_at: datetime | None = None,
    approved_by: str | None = None,
    deleted_at: datetime | None = None,
) -> UserAvailabilityOverride:
    return UserAvailabilityOverride(
        id=id,
        workspace_id=workspace_id,
        user_id=user_id,
        date=date,
        available=available,
        starts_local=starts_local,
        ends_local=ends_local,
        reason=reason,
        approval_required=approval_required,
        approved_at=approved_at,
        approved_by=approved_by,
        created_at=_PINNED,
        updated_at=_PINNED,
        deleted_at=deleted_at,
    )


# ---------------------------------------------------------------------------
# Pure-Python construction shape
# ---------------------------------------------------------------------------


class TestModelShape:
    def test_minimal_construction(self) -> None:
        row = UserAvailabilityOverride(
            id="01HWA00000000000000000AO01",
            workspace_id="01HWA00000000000000000WSPA",
            user_id="01HWA00000000000000000USRA",
            date=_TODAY,
            available=True,
            approval_required=False,
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        assert row.id == "01HWA00000000000000000AO01"
        assert row.workspace_id == "01HWA00000000000000000WSPA"
        assert row.user_id == "01HWA00000000000000000USRA"
        assert row.date == _TODAY
        assert row.available is True
        assert row.approval_required is False
        assert row.starts_local is None
        assert row.ends_local is None
        assert row.reason is None
        assert row.approved_at is None
        assert row.approved_by is None
        assert row.deleted_at is None

    def test_tablename(self) -> None:
        assert UserAvailabilityOverride.__tablename__ == "user_availability_override"

    def test_hours_pairing_check_present(self) -> None:
        checks = [
            c
            for c in UserAvailabilityOverride.__table_args__
            if isinstance(c, CheckConstraint)
        ]
        assert "ck_user_availability_override_hours_pairing" in {c.name for c in checks}

    def test_unique_constraint_present(self) -> None:
        uniques = [
            c
            for c in UserAvailabilityOverride.__table_args__
            if isinstance(c, UniqueConstraint)
        ]
        target = next(
            c for c in uniques if c.name == "uq_user_availability_override_user_date"
        )
        assert [col.name for col in target.columns] == [
            "workspace_id",
            "user_id",
            "date",
        ]

    def test_hot_path_index_present(self) -> None:
        indexes: dict[str, list[str]] = {
            str(i.name): [c.name for c in i.columns]
            for i in UserAvailabilityOverride.__table_args__
            if isinstance(i, Index)
        }
        assert indexes["ix_user_availability_override_workspace_user"] == [
            "workspace_id",
            "user_id",
        ]


class TestRegistryMembership:
    def test_user_availability_override_registered(self) -> None:
        assert registry.is_scoped("user_availability_override")


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_insert_then_read_back(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPA", "alpha")
        session.add(
            _override(
                id="01HWA00000000000000000AO02",
                workspace_id="01HWA00000000000000000WSPA",
                user_id="01HWA00000000000000000USRA",
                date=_TODAY,
                available=True,
                starts_local=_T_0900,
                ends_local=_T_1700,
                reason="covering for Maria",
                approval_required=True,
                approved_at=_LATER,
                approved_by="01HWA00000000000000000MGR1",
            )
        )
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(UserAvailabilityOverride).where(
                UserAvailabilityOverride.id == "01HWA00000000000000000AO02"
            )
        ).one()
        assert loaded.available is True
        assert loaded.starts_local == _T_0900
        assert loaded.ends_local == _T_1700
        assert loaded.reason == "covering for Maria"
        assert loaded.approval_required is True
        assert loaded.approved_by == "01HWA00000000000000000MGR1"

    def test_unavailable_override_round_trip(self, session: Session) -> None:
        """An ``available = false`` override (declining a working day)."""
        _seed_workspace(session, "01HWA00000000000000000WSPB", "decline")
        session.add(
            _override(
                id="01HWA00000000000000000AO03",
                workspace_id="01HWA00000000000000000WSPB",
                user_id="01HWA00000000000000000USRB",
                date=_TOMORROW,
                available=False,
                approval_required=True,
                reason="doctor appointment",
            )
        )
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(UserAvailabilityOverride).where(
                UserAvailabilityOverride.id == "01HWA00000000000000000AO03"
            )
        ).one()
        assert loaded.available is False
        assert loaded.starts_local is None
        assert loaded.ends_local is None


# ---------------------------------------------------------------------------
# BOTH-OR-NEITHER hours pairing CHECK
# ---------------------------------------------------------------------------


class TestHoursPairingCheck:
    def test_starts_only_rejected(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSP1", "starts-only")
        session.add(
            _override(
                id="01HWA00000000000000000AO04",
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
            _override(
                id="01HWA00000000000000000AO05",
                workspace_id="01HWA00000000000000000WSP2",
                user_id="01HWA00000000000000000US02",
                starts_local=None,
                ends_local=_T_1700,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


# ---------------------------------------------------------------------------
# UNIQUE(workspace_id, user_id, date)
# ---------------------------------------------------------------------------


class TestUniqueWorkspaceUserDate:
    """One override per (workspace, user, date)."""

    def test_two_rows_same_triple_rejected(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPU", "uniq-trip")
        session.add(
            _override(
                id="01HWA00000000000000000AO06",
                workspace_id="01HWA00000000000000000WSPU",
                user_id="01HWA00000000000000000USUQ",
                date=_TODAY,
            )
        )
        session.flush()

        session.add(
            _override(
                id="01HWA00000000000000000AO07",
                workspace_id="01HWA00000000000000000WSPU",
                user_id="01HWA00000000000000000USUQ",
                date=_TODAY,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_same_user_different_dates_accepted(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPV", "two-dates")
        session.add(
            _override(
                id="01HWA00000000000000000AO08",
                workspace_id="01HWA00000000000000000WSPV",
                user_id="01HWA00000000000000000USVU",
                date=_TODAY,
            )
        )
        session.add(
            _override(
                id="01HWA00000000000000000AO09",
                workspace_id="01HWA00000000000000000WSPV",
                user_id="01HWA00000000000000000USVU",
                date=_TOMORROW,
            )
        )
        session.flush()  # No IntegrityError — two distinct dates.

    def test_same_date_different_users_accepted(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPX", "two-users")
        session.add(
            _override(
                id="01HWA00000000000000000AO0A",
                workspace_id="01HWA00000000000000000WSPX",
                user_id="01HWA00000000000000000USXA",
                date=_TODAY,
            )
        )
        session.add(
            _override(
                id="01HWA00000000000000000AO0B",
                workspace_id="01HWA00000000000000000WSPX",
                user_id="01HWA00000000000000000USXB",
                date=_TODAY,
            )
        )
        session.flush()  # No IntegrityError — two users, same date.


# ---------------------------------------------------------------------------
# Soft-delete tombstone
# ---------------------------------------------------------------------------


class TestSoftDeleteTombstone:
    def test_soft_delete_excludes_from_default_select(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPS", "soft")
        # Live row.
        session.add(
            _override(
                id="01HWA00000000000000000AO0C",
                workspace_id="01HWA00000000000000000WSPS",
                user_id="01HWA00000000000000000USRS",
                date=_TODAY,
            )
        )
        # Tombstoned row at a different date so the UNIQUE doesn't
        # fight us. (The unique applies regardless of soft-delete
        # state — there is no partial UNIQUE here. The service layer
        # is responsible for "withdraw + re-submit on the same date"
        # by flipping ``deleted_at`` back to NULL on the existing
        # row, not by inserting a fresh row.)
        session.add(
            _override(
                id="01HWA00000000000000000AO0D",
                workspace_id="01HWA00000000000000000WSPS",
                user_id="01HWA00000000000000000USRS",
                date=_TOMORROW,
                deleted_at=_LATER,
            )
        )
        session.flush()
        session.expire_all()

        live = session.scalars(
            select(UserAvailabilityOverride).where(
                UserAvailabilityOverride.workspace_id == "01HWA00000000000000000WSPS",
                UserAvailabilityOverride.deleted_at.is_(None),
            )
        ).all()
        assert {r.id for r in live} == {"01HWA00000000000000000AO0C"}, (
            "live-list path leaked a tombstoned row — check the "
            "deleted_at column round-trips correctly"
        )

    def test_tombstone_timestamp_round_trips(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPR", "ts-rt")
        row = _override(
            id="01HWA00000000000000000AO0E",
            workspace_id="01HWA00000000000000000WSPR",
            user_id="01HWA00000000000000000USRT",
        )
        session.add(row)
        session.flush()

        row.deleted_at = _LATER
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(UserAvailabilityOverride).where(
                UserAvailabilityOverride.id == "01HWA00000000000000000AO0E"
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
            _override(
                id="01HWA00000000000000000AOAA",
                workspace_id="01HWA00000000000000000WSAA",
                user_id="01HWA00000000000000000USHR",
            )
        )
        session.add(
            _override(
                id="01HWA00000000000000000AOBB",
                workspace_id="01HWA00000000000000000WSBB",
                user_id="01HWA00000000000000000USHR",
            )
        )
        session.flush()
        session.expire_all()

        rows_a = session.scalars(
            select(UserAvailabilityOverride).where(
                UserAvailabilityOverride.workspace_id == "01HWA00000000000000000WSAA"
            )
        ).all()
        assert {r.id for r in rows_a} == {"01HWA00000000000000000AOAA"}, (
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
            _seed_workspace(session, "01HWA00000000000000000WSCC", "cascade-ao")
            session.add(
                _override(
                    id="01HWA00000000000000000AOCC",
                    workspace_id="01HWA00000000000000000WSCC",
                    user_id="01HWA00000000000000000USCC",
                )
            )
            session.flush()

            ws = session.get(Workspace, "01HWA00000000000000000WSCC")
            assert ws is not None
            session.delete(ws)
            session.flush()
            session.expire_all()

            remaining = session.scalars(
                select(UserAvailabilityOverride).where(
                    UserAvailabilityOverride.workspace_id
                    == "01HWA00000000000000000WSCC"
                )
            ).all()
            assert remaining == [], (
                "user_availability_override rows survived a workspace "
                "hard-delete — FK ON DELETE CASCADE is not firing"
            )
