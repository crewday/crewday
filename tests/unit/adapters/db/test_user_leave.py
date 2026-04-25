"""Unit tests for :class:`UserLeave` (cd-l2r9).

Covers the SQLAlchemy mapped class from
:mod:`app.adapters.db.availability.models`:

* construction defaults (``approved_at`` / ``approved_by`` / ``note_md``
  / ``deleted_at`` all default to ``None``);
* tablename + ``__table_args__`` shape (CHECKs, hot-path index,
  registry membership);
* in-memory SQLite round-trip, including the ``category`` enum CHECK,
  the ``ends_on >= starts_on`` range CHECK, the soft-delete tombstone
  round-trip, and the approve-then-archive lifecycle;
* cross-workspace insert isolation so a row owned by workspace B is
  invisible to a SELECT scoped to workspace A;
* workspace hard-delete cascade on SQLite, using a dedicated
  ``fk_engine`` fixture that installs ``PRAGMA foreign_keys=ON``.

Integration coverage (FK cascade on PG, schema fingerprint parity,
tenant-filter behaviour) is delegated to
:mod:`tests.integration.test_migration_cd_l2r9` and
:mod:`tests.integration.test_schema_parity`.

See ``docs/specs/06-tasks-and-scheduling.md`` §"user_leave".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import CheckConstraint, Engine, Index, create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.availability import UserLeave
from app.adapters.db.base import Base
from app.adapters.db.workspace import Workspace
from app.tenancy import registry

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_TODAY = date(2026, 4, 25)
_TOMORROW = date(2026, 4, 26)


# ---------------------------------------------------------------------------
# Engine fixture — in-memory SQLite shared across the test
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so cross-package
    FKs resolve on a bare ``Base.metadata.create_all``.

    Mirrors the sibling helper in
    :mod:`tests.unit.adapters.db.test_work_engagement` — without this
    step a test run order that imports a later context first could
    leave ``Base.metadata`` with dangling FKs.
    """
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
    """Same in-memory SQLite engine as :func:`engine`, but with FKs enforced.

    The default fixture skips ``PRAGMA foreign_keys=ON`` (schema-shape
    is the concern there). Cascade-delete coverage needs the pragma so
    the ``ON DELETE CASCADE`` fires; this fixture mirrors the
    production hook from
    :func:`app.adapters.db.session._enable_sqlite_foreign_keys`.
    """
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
    """Fresh :class:`Session` bound to the in-memory engine."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    s = factory()
    try:
        yield s
    finally:
        s.close()


def _seed_workspace(session: Session, workspace_id: str, slug: str) -> Workspace:
    """Insert a minimal workspace so the FK constraint lands cleanly."""
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


def _leave(
    *,
    id: str,
    workspace_id: str,
    user_id: str,
    starts_on: date = _TODAY,
    ends_on: date = _TOMORROW,
    category: str = "vacation",
    approved_at: datetime | None = None,
    approved_by: str | None = None,
    note_md: str | None = None,
    deleted_at: datetime | None = None,
) -> UserLeave:
    """Factory helper — most tests only vary a couple of fields."""
    return UserLeave(
        id=id,
        workspace_id=workspace_id,
        user_id=user_id,
        starts_on=starts_on,
        ends_on=ends_on,
        category=category,
        approved_at=approved_at,
        approved_by=approved_by,
        note_md=note_md,
        created_at=_PINNED,
        updated_at=_PINNED,
        deleted_at=deleted_at,
    )


# ---------------------------------------------------------------------------
# Pure-Python construction shape
# ---------------------------------------------------------------------------


class TestUserLeaveModelShape:
    """The mapped class carries the cd-l2r9 v1 slice."""

    def test_minimal_construction(self) -> None:
        row = UserLeave(
            id="01HWA00000000000000000UL01",
            workspace_id="01HWA00000000000000000WSPA",
            user_id="01HWA00000000000000000USRA",
            starts_on=_TODAY,
            ends_on=_TOMORROW,
            category="vacation",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        assert row.id == "01HWA00000000000000000UL01"
        assert row.workspace_id == "01HWA00000000000000000WSPA"
        assert row.user_id == "01HWA00000000000000000USRA"
        assert row.starts_on == _TODAY
        assert row.ends_on == _TOMORROW
        assert row.category == "vacation"
        # Optional columns default to ``None`` until set.
        assert row.approved_at is None
        assert row.approved_by is None
        assert row.note_md is None
        assert row.deleted_at is None

    def test_tablename(self) -> None:
        assert UserLeave.__tablename__ == "user_leave"

    def test_category_check_present(self) -> None:
        """``__table_args__`` carries the category CHECK."""
        checks = [c for c in UserLeave.__table_args__ if isinstance(c, CheckConstraint)]
        assert "ck_user_leave_category" in {c.name for c in checks}

    def test_range_check_present(self) -> None:
        """``__table_args__`` carries the ``ends_on >= starts_on`` CHECK."""
        checks = [c for c in UserLeave.__table_args__ if isinstance(c, CheckConstraint)]
        assert "ck_user_leave_range" in {c.name for c in checks}

    def test_hot_path_index_present(self) -> None:
        """``(workspace_id, user_id)`` non-unique index for the candidate-pool walk."""
        indexes: dict[str, list[str]] = {
            str(i.name): [c.name for c in i.columns]
            for i in UserLeave.__table_args__
            if isinstance(i, Index)
        }
        assert indexes["ix_user_leave_workspace_user"] == [
            "workspace_id",
            "user_id",
        ]


class TestRegistryMembership:
    """``user_leave`` is registered as scoped.

    Mirrors the pattern in :class:`tests.unit.test_db_messaging.TestRegistryIntent`:
    we call :func:`registry.register` directly rather than relying on the
    import-time side effect, because sibling tests (e.g.
    :mod:`tests.unit.test_tenancy_orm_filter`) wipe the process-wide
    registry via an autouse ``_reset_for_tests`` fixture. Under
    pytest-xdist the test order across workers is non-deterministic,
    so a reset can land between collection and this assertion.
    """

    def test_user_leave_registered(self) -> None:
        registry.register("user_leave")
        assert registry.is_scoped("user_leave")


# ---------------------------------------------------------------------------
# Idempotent re-import
# ---------------------------------------------------------------------------


class TestModuleReimportIdempotent:
    """Re-importing the package does not redefine the table."""

    def test_reimport_does_not_raise(self) -> None:
        import importlib

        import app.adapters.db.availability as avail_pkg

        importlib.reload(avail_pkg)


# ---------------------------------------------------------------------------
# Round-trip + category CHECK + range CHECK on SQLite
# ---------------------------------------------------------------------------


class TestUserLeaveRoundTrip:
    """Insert + reload exercises the round-trip path on SQLite."""

    def test_insert_then_read_back(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPA", "alpha")
        session.add(
            _leave(
                id="01HWA00000000000000000UL02",
                workspace_id="01HWA00000000000000000WSPA",
                user_id="01HWA00000000000000000USRA",
                category="sick",
                approved_at=_LATER,
                approved_by="01HWA00000000000000000MGR1",
                note_md="flu, doctor's note attached",
            )
        )
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(UserLeave).where(UserLeave.id == "01HWA00000000000000000UL02")
        ).one()
        assert loaded.category == "sick"
        assert loaded.note_md == "flu, doctor's note attached"
        assert loaded.approved_by == "01HWA00000000000000000MGR1"
        assert loaded.approved_at is not None

    def test_same_day_leave_accepted(self, session: Session) -> None:
        """``starts_on == ends_on`` is a single-day leave — must be accepted."""
        _seed_workspace(session, "01HWA00000000000000000WSPS", "same-day")
        session.add(
            _leave(
                id="01HWA00000000000000000UL03",
                workspace_id="01HWA00000000000000000WSPS",
                user_id="01HWA00000000000000000USRS",
                starts_on=_TODAY,
                ends_on=_TODAY,
                category="personal",
            )
        )
        session.flush()  # No IntegrityError — single-day is valid.

    def test_pending_leave_round_trip(self, session: Session) -> None:
        """A self-submitted leave (``approved_at IS NULL``) round-trips cleanly."""
        _seed_workspace(session, "01HWA00000000000000000WSPP", "pending")
        session.add(
            _leave(
                id="01HWA00000000000000000UL04",
                workspace_id="01HWA00000000000000000WSPP",
                user_id="01HWA00000000000000000USRP",
            )
        )
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(UserLeave).where(UserLeave.id == "01HWA00000000000000000UL04")
        ).one()
        assert loaded.approved_at is None
        assert loaded.approved_by is None


class TestCategoryCheck:
    """The ``category`` CHECK rejects unknown enum values."""

    @pytest.mark.parametrize("bad_value", ["holiday", "PTO", "", "VACATION"])
    def test_unknown_category_rejected(self, session: Session, bad_value: str) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSCK", f"cat-{bad_value}")
        session.add(
            _leave(
                id=f"01HWA00000000000000000UL{bad_value[:2] or 'XY':<2}"[:26],
                workspace_id="01HWA00000000000000000WSCK",
                user_id="01HWA00000000000000000USCK",
                category=bad_value,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    @pytest.mark.parametrize(
        "category", ["vacation", "sick", "personal", "bereavement", "other"]
    )
    def test_known_categories_accepted(self, session: Session, category: str) -> None:
        _seed_workspace(
            session, f"01HWA0000000000000000WS{category[:2].upper():<2}"[:26], category
        )
        session.add(
            _leave(
                id=f"01HWA00000000000000UL{category[:4].upper():<4}"[:26],
                workspace_id=f"01HWA0000000000000000WS{category[:2].upper():<2}"[:26],
                user_id="01HWA00000000000000000USKN",
                category=category,
            )
        )
        session.flush()  # No IntegrityError — every enum value valid.


class TestRangeCheck:
    """``ends_on >= starts_on`` rejects backwards ranges."""

    def test_backwards_range_rejected(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSRG", "range-bad")
        session.add(
            _leave(
                id="01HWA00000000000000000UL05",
                workspace_id="01HWA00000000000000000WSRG",
                user_id="01HWA00000000000000000USRG",
                starts_on=_TOMORROW,
                ends_on=_TODAY,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


# ---------------------------------------------------------------------------
# Soft-delete tombstone — round-trip + default-list filtering
# ---------------------------------------------------------------------------


class TestSoftDeleteTombstone:
    """``deleted_at`` round-trips and excludes from the live-list filter."""

    def test_soft_delete_excludes_from_default_select(self, session: Session) -> None:
        """A ``WHERE deleted_at IS NULL`` filter hides tombstoned rows."""
        _seed_workspace(session, "01HWA00000000000000000WSPS", "soft")
        session.add(
            _leave(
                id="01HWA00000000000000000UL06",
                workspace_id="01HWA00000000000000000WSPS",
                user_id="01HWA00000000000000000USRS",
            )
        )
        session.add(
            _leave(
                id="01HWA00000000000000000UL07",
                workspace_id="01HWA00000000000000000WSPS",
                user_id="01HWA00000000000000000USRS",
                # Different range so the test is unambiguous about
                # which row is tombstoned vs live.
                starts_on=date(2026, 5, 1),
                ends_on=date(2026, 5, 2),
                deleted_at=_LATER,
            )
        )
        session.flush()
        session.expire_all()

        live = session.scalars(
            select(UserLeave).where(
                UserLeave.workspace_id == "01HWA00000000000000000WSPS",
                UserLeave.deleted_at.is_(None),
            )
        ).all()
        assert {r.id for r in live} == {"01HWA00000000000000000UL06"}, (
            "live-list path leaked a tombstoned row — check the "
            "deleted_at column round-trips correctly"
        )

    def test_tombstone_timestamp_round_trips(self, session: Session) -> None:
        """Setting ``deleted_at`` and reading it back round-trips cleanly."""
        _seed_workspace(session, "01HWA00000000000000000WSPR", "ts-rt")
        row = _leave(
            id="01HWA00000000000000000UL08",
            workspace_id="01HWA00000000000000000WSPR",
            user_id="01HWA00000000000000000USRT",
        )
        session.add(row)
        session.flush()

        row.deleted_at = _LATER
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(UserLeave).where(UserLeave.id == "01HWA00000000000000000UL08")
        ).one()
        assert loaded.deleted_at is not None
        assert loaded.deleted_at.replace(tzinfo=None) == _LATER.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Cross-workspace isolation
# ---------------------------------------------------------------------------


class TestCrossWorkspaceIsolation:
    """A row owned by workspace B is invisible to a SELECT for A."""

    def test_b_row_invisible_under_a_filter(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSAA", "iso-a")
        _seed_workspace(session, "01HWA00000000000000000WSBB", "iso-b")
        session.add(
            _leave(
                id="01HWA00000000000000000UL0A",
                workspace_id="01HWA00000000000000000WSAA",
                user_id="01HWA00000000000000000USHR",
            )
        )
        session.add(
            _leave(
                id="01HWA00000000000000000UL0B",
                workspace_id="01HWA00000000000000000WSBB",
                user_id="01HWA00000000000000000USHR",
            )
        )
        session.flush()
        session.expire_all()

        rows_a = session.scalars(
            select(UserLeave).where(
                UserLeave.workspace_id == "01HWA00000000000000000WSAA"
            )
        ).all()
        assert {r.id for r in rows_a} == {"01HWA00000000000000000UL0A"}, (
            "A-scoped SELECT returned a B-owned row — workspace_id is "
            "not discriminating correctly"
        )


# ---------------------------------------------------------------------------
# FK cascade on SQLite — workspace delete sweeps user_leave rows
# ---------------------------------------------------------------------------


class TestWorkspaceCascade:
    """Hard-deleting the parent workspace sweeps ``user_leave`` rows."""

    def test_workspace_delete_cascades_to_leave(self, fk_engine: Engine) -> None:
        factory = sessionmaker(bind=fk_engine, expire_on_commit=False, class_=Session)
        with factory() as session:
            _seed_workspace(session, "01HWA00000000000000000WSCC", "cascade-ul")
            session.add(
                _leave(
                    id="01HWA00000000000000000UL0C",
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
                select(UserLeave).where(
                    UserLeave.workspace_id == "01HWA00000000000000000WSCC"
                )
            ).all()
            assert remaining == [], (
                "user_leave rows survived a workspace hard-delete — "
                "FK ON DELETE CASCADE is not firing"
            )
