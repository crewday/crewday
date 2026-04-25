"""Unit tests for :class:`PropertyWorkRoleAssignment` (cd-e4m3).

Covers the SQLAlchemy mapped class from
:mod:`app.adapters.db.places.models`:

* construction defaults (``schedule_ruleset_id`` /
  ``property_pay_rule_id`` / ``deleted_at`` all default to ``None``);
* tablename + ``__table_args__`` shape (partial UNIQUE, hot-path
  indexes, registry membership);
* in-memory SQLite round-trip, including the partial UNIQUE on
  ``(user_work_role_id, property_id) WHERE deleted_at IS NULL``,
  the soft-delete tombstone round-trip, and the
  archive-then-re-pin lifecycle;
* cross-workspace isolation so a row owned by workspace B is
  invisible to a manual ``WHERE workspace_id = A`` SELECT;
* property hard-delete cascade — sweeping the assignment row when
  the parent property is removed, using a dedicated ``fk_engine``
  fixture that installs ``PRAGMA foreign_keys=ON``.

Integration coverage (FK cascade on PG, schema fingerprint parity,
tenant-filter behaviour) is delegated to
:mod:`tests.integration.test_db_places` and
:mod:`tests.integration.test_schema_parity`.

See ``docs/specs/05-employees-and-roles.md`` §"Property work role
assignment", ``docs/specs/02-domain-model.md`` §"People, work roles,
engagements", and ``docs/specs/06-tasks-and-scheduling.md``
§"Schedule ruleset (per-property rota)".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, Index, create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.base import Base

# Importing the package (not just ``.models``) is critical so the
# tenancy-registry side effect fires; the registry-membership test
# then observes the table is registered.
from app.adapters.db.places import (
    Property,
    PropertyWorkRoleAssignment,
    PropertyWorkspace,
)
from app.adapters.db.workspace import (
    UserWorkRole,
    WorkRole,
    Workspace,
)
from app.tenancy import registry

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
_TODAY = date(2026, 4, 25)


# ---------------------------------------------------------------------------
# Engine fixture — in-memory SQLite shared across the test
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so cross-package
    FKs (e.g. ``user_work_role.workspace_id`` → ``workspace.id``,
    ``property_work_role_assignment.property_pay_rule_id`` →
    ``pay_rule.id``) resolve on a bare ``Base.metadata.create_all``.

    Mirrors the sibling helper in
    :mod:`tests.unit.adapters.db.test_work_role` — without this step
    a test run order that imports a later context first could leave
    ``Base.metadata`` with dangling FKs.
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
            # Only swallow "this context has no models module yet" —
            # any other import-time failure must surface.
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with every ORM table created.

    StaticPool keeps the same underlying SQLite DB across checkouts
    so the fixture's ``create_all`` is visible to the session the
    test opens.
    """
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

    The default unit fixture skips ``PRAGMA foreign_keys=ON`` so the
    schema-shape suite stays cheap. Cascade-delete coverage on the
    new table needs the pragma so the ``ON DELETE CASCADE`` actually
    fires; this fixture mirrors the production hook from
    :func:`app.adapters.db.session._enable_sqlite_foreign_keys` for
    the cascade test alone.
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
        # The pool hands connections back without re-running connect
        # events, so we still re-issue the PRAGMA every time we see
        # a raw sqlite3 connection — same defensive guard as the
        # production helper.
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
    """Fresh :class:`Session` bound to the in-memory engine.

    Skips the tenant filter — the unit slice exercises the schema
    shape, not the filter wiring (which is covered in
    :mod:`tests.integration.test_db_places`).
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    s = factory()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Seeding helpers — chain a workspace → work_role → user_work_role + property
# ---------------------------------------------------------------------------


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


def _seed_property(session: Session, property_id: str) -> Property:
    """Insert a minimal property — tenant-agnostic at the row level."""
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


def _seed_work_role(
    session: Session, *, role_id: str, workspace_id: str, key: str = "maid"
) -> WorkRole:
    """Insert a minimal :class:`WorkRole` for the given workspace."""
    role = WorkRole(
        id=role_id,
        workspace_id=workspace_id,
        key=key,
        name=key.title(),
        created_at=_PINNED,
    )
    session.add(role)
    session.flush()
    return role


def _seed_user_work_role(
    session: Session,
    *,
    user_work_role_id: str,
    user_id: str,
    workspace_id: str,
    work_role_id: str,
) -> UserWorkRole:
    """Insert a minimal :class:`UserWorkRole` link row."""
    link = UserWorkRole(
        id=user_work_role_id,
        user_id=user_id,
        workspace_id=workspace_id,
        work_role_id=work_role_id,
        started_on=_TODAY,
        created_at=_PINNED,
    )
    session.add(link)
    session.flush()
    return link


def _assignment(
    *,
    id: str,
    workspace_id: str,
    user_work_role_id: str,
    property_id: str,
    schedule_ruleset_id: str | None = None,
    property_pay_rule_id: str | None = None,
    deleted_at: datetime | None = None,
    created_at: datetime = _PINNED,
    updated_at: datetime = _PINNED,
) -> PropertyWorkRoleAssignment:
    """Factory helper — most tests only vary a couple of fields."""
    return PropertyWorkRoleAssignment(
        id=id,
        workspace_id=workspace_id,
        user_work_role_id=user_work_role_id,
        property_id=property_id,
        schedule_ruleset_id=schedule_ruleset_id,
        property_pay_rule_id=property_pay_rule_id,
        created_at=created_at,
        updated_at=updated_at,
        deleted_at=deleted_at,
    )


def _bootstrap_chain(
    session: Session,
    *,
    workspace_id: str,
    workspace_slug: str,
    property_id: str,
    work_role_id: str,
    user_work_role_id: str,
    user_id: str,
) -> None:
    """Seed workspace + property + work_role + user_work_role.

    The four parent rows the assignment FKs at — keeps the per-test
    setup terse without smearing the fixture state across cases.
    """
    _seed_workspace(session, workspace_id, workspace_slug)
    _seed_property(session, property_id)
    _seed_work_role(session, role_id=work_role_id, workspace_id=workspace_id)
    _seed_user_work_role(
        session,
        user_work_role_id=user_work_role_id,
        user_id=user_id,
        workspace_id=workspace_id,
        work_role_id=work_role_id,
    )


# ---------------------------------------------------------------------------
# Pure-Python construction shape
# ---------------------------------------------------------------------------


class TestPropertyWorkRoleAssignmentModelShape:
    """The mapped class carries the cd-e4m3 v1 slice."""

    def test_minimal_construction(self) -> None:
        row = PropertyWorkRoleAssignment(
            id="01HWA00000000000000000PWRA",
            workspace_id="01HWA00000000000000000WSPA",
            user_work_role_id="01HWA00000000000000000UWR1",
            property_id="01HWA00000000000000000PRPA",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        assert row.id == "01HWA00000000000000000PWRA"
        assert row.workspace_id == "01HWA00000000000000000WSPA"
        assert row.user_work_role_id == "01HWA00000000000000000UWR1"
        assert row.property_id == "01HWA00000000000000000PRPA"
        assert row.created_at == _PINNED
        assert row.updated_at == _PINNED
        # Optional columns default to ``None`` until set.
        assert row.schedule_ruleset_id is None
        assert row.property_pay_rule_id is None
        assert row.deleted_at is None

    def test_tablename(self) -> None:
        assert (
            PropertyWorkRoleAssignment.__tablename__ == "property_work_role_assignment"
        )

    def test_partial_unique_index_present(self) -> None:
        """``uq_property_work_role_assignment_role_property_active``
        carries the partial predicate.

        Both the SQLite and PG dialect kwargs must be set so Alembic
        and ``Base.metadata.create_all`` agree on the same ``WHERE``
        predicate (``deleted_at IS NULL``).
        """
        indexes = [
            i for i in PropertyWorkRoleAssignment.__table_args__ if isinstance(i, Index)
        ]
        target = next(
            i
            for i in indexes
            if i.name == "uq_property_work_role_assignment_role_property_active"
        )
        assert target.unique is True
        assert [c.name for c in target.columns] == [
            "user_work_role_id",
            "property_id",
        ]
        # The partial predicate is dialect-scoped; both halves must
        # be present so Alembic + ``create_all`` agree on shape.
        assert target.dialect_kwargs.get("sqlite_where") is not None
        assert target.dialect_kwargs.get("postgresql_where") is not None

    def test_hot_path_indexes_present(self) -> None:
        """Three non-unique indexes leading on ``workspace_id``."""
        indexes: dict[str, list[str]] = {
            str(i.name): [c.name for c in i.columns]
            for i in PropertyWorkRoleAssignment.__table_args__
            if isinstance(i, Index) and not i.unique
        }
        assert indexes["ix_property_work_role_assignment_workspace_deleted"] == [
            "workspace_id",
            "deleted_at",
        ]
        assert indexes["ix_property_work_role_assignment_workspace_user_work_role"] == [
            "workspace_id",
            "user_work_role_id",
        ]
        assert indexes["ix_property_work_role_assignment_workspace_property"] == [
            "workspace_id",
            "property_id",
        ]


class TestRegistryMembership:
    """``property_work_role_assignment`` is registered as scoped.

    See :class:`tests.unit.adapters.db.test_user_leave.TestRegistryMembership`
    for the rationale behind calling :func:`registry.register` directly
    rather than asserting the import-time side effect.
    """

    def test_assignment_registered(self) -> None:
        registry.register("property_work_role_assignment")
        assert registry.is_scoped("property_work_role_assignment")

    def test_property_table_still_not_registered(self) -> None:
        """Sanity: the existing tenancy-agnostic shape on ``property``
        survives the cd-e4m3 registration. No test or production code
        calls ``register("property")`` — verified at audit time — so the
        absence assertion is robust to test ordering under xdist."""
        assert not registry.is_scoped("property")


# ---------------------------------------------------------------------------
# Idempotent re-import — landing the module twice must not redefine the table
# ---------------------------------------------------------------------------


class TestModuleReimportIdempotent:
    """A second ``import`` of the package does not redefine the table.

    Re-importing a SQLAlchemy module that already populated
    :attr:`Base.metadata` raises
    ``InvalidRequestError: Table 'property_work_role_assignment' is
    already defined`` if the module body re-declares the class on
    top of an existing one. This test guards against a refactor that
    would inadvertently re-define the table.
    """

    def test_reimport_does_not_raise(self) -> None:
        import importlib

        import app.adapters.db.places as places_pkg

        # Force-reimport via importlib so the cached module is
        # re-executed; a redefinition error on the mapped class
        # would raise inside ``importlib.reload``.
        importlib.reload(places_pkg)


# ---------------------------------------------------------------------------
# Round-trip on SQLite — insert + reload exercises the basic shape
# ---------------------------------------------------------------------------


class TestPropertyWorkRoleAssignmentRoundTrip:
    """Insert + reload exercises the round-trip path on SQLite."""

    def test_create_assignment_basic(self, session: Session) -> None:
        _bootstrap_chain(
            session,
            workspace_id="01HWA00000000000000000WSPA",
            workspace_slug="alpha",
            property_id="01HWA00000000000000000PRPA",
            work_role_id="01HWA00000000000000000WRA1",
            user_work_role_id="01HWA00000000000000000UWR1",
            user_id="01HWA00000000000000000USRA",
        )
        session.add(
            _assignment(
                id="01HWA00000000000000000PWRA",
                workspace_id="01HWA00000000000000000WSPA",
                user_work_role_id="01HWA00000000000000000UWR1",
                property_id="01HWA00000000000000000PRPA",
                schedule_ruleset_id="01HWA00000000000000000RUL1",
            )
        )
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(PropertyWorkRoleAssignment).where(
                PropertyWorkRoleAssignment.id == "01HWA00000000000000000PWRA"
            )
        ).one()
        assert loaded.workspace_id == "01HWA00000000000000000WSPA"
        assert loaded.user_work_role_id == "01HWA00000000000000000UWR1"
        assert loaded.property_id == "01HWA00000000000000000PRPA"
        assert loaded.schedule_ruleset_id == "01HWA00000000000000000RUL1"
        assert loaded.property_pay_rule_id is None
        assert loaded.deleted_at is None

    def test_minimal_required_columns(self, session: Session) -> None:
        """Only the four mandatory FKs + timestamps are required."""
        _bootstrap_chain(
            session,
            workspace_id="01HWA00000000000000000WSPM",
            workspace_slug="minimal",
            property_id="01HWA00000000000000000PRPM",
            work_role_id="01HWA00000000000000000WRM1",
            user_work_role_id="01HWA00000000000000000UWM1",
            user_id="01HWA00000000000000000USRM",
        )
        session.add(
            _assignment(
                id="01HWA00000000000000000PWMM",
                workspace_id="01HWA00000000000000000WSPM",
                user_work_role_id="01HWA00000000000000000UWM1",
                property_id="01HWA00000000000000000PRPM",
            )
        )
        session.flush()
        session.expire_all()
        loaded = session.scalars(
            select(PropertyWorkRoleAssignment).where(
                PropertyWorkRoleAssignment.id == "01HWA00000000000000000PWMM"
            )
        ).one()
        assert loaded.schedule_ruleset_id is None
        assert loaded.property_pay_rule_id is None


# ---------------------------------------------------------------------------
# Partial UNIQUE — one live row per (user_work_role, property)
# ---------------------------------------------------------------------------


class TestUniquePerUserWorkRoleProperty:
    """``(user_work_role_id, property_id) WHERE deleted_at IS NULL`` UNIQUE.

    SQLite 3.8+ respects the partial predicate; PG always does. This
    class is the unit guard that the predicate lands on the SQLite
    side; integration-level coverage (PG parity) is handled by
    :mod:`tests.integration.test_schema_parity`.
    """

    def test_unique_per_workspace_property_role_user(self, session: Session) -> None:
        """Two live rows for the same (user_work_role, property) is rejected."""
        _bootstrap_chain(
            session,
            workspace_id="01HWA00000000000000000WSPU",
            workspace_slug="uniq-two",
            property_id="01HWA00000000000000000PRPU",
            work_role_id="01HWA00000000000000000WRU1",
            user_work_role_id="01HWA00000000000000000UWU1",
            user_id="01HWA00000000000000000USRU",
        )
        session.add(
            _assignment(
                id="01HWA00000000000000000PWU1",
                workspace_id="01HWA00000000000000000WSPU",
                user_work_role_id="01HWA00000000000000000UWU1",
                property_id="01HWA00000000000000000PRPU",
            )
        )
        session.flush()

        # Second live row with the same identity tuple — must fail.
        session.add(
            _assignment(
                id="01HWA00000000000000000PWU2",
                workspace_id="01HWA00000000000000000WSPU",
                user_work_role_id="01HWA00000000000000000UWU1",
                property_id="01HWA00000000000000000PRPU",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_live_plus_archived_coexist(self, session: Session) -> None:
        """An archived row + a live row for the same (uwr, property) is OK.

        The partial predicate excludes ``deleted_at IS NOT NULL``
        rows from the UNIQUE, so the archive history can stack up
        without fighting the live-row invariant.
        """
        _bootstrap_chain(
            session,
            workspace_id="01HWA00000000000000000WSPV",
            workspace_slug="uniq-mixed",
            property_id="01HWA00000000000000000PRPV",
            work_role_id="01HWA00000000000000000WRV1",
            user_work_role_id="01HWA00000000000000000UWV1",
            user_id="01HWA00000000000000000USRV",
        )
        # Archived row first so the next INSERT lands against a
        # non-empty state.
        session.add(
            _assignment(
                id="01HWA00000000000000000PWV1",
                workspace_id="01HWA00000000000000000WSPV",
                user_work_role_id="01HWA00000000000000000UWV1",
                property_id="01HWA00000000000000000PRPV",
                deleted_at=_PINNED,
            )
        )
        session.flush()
        # Live row for the same (uwr, property) — must be accepted.
        session.add(
            _assignment(
                id="01HWA00000000000000000PWV2",
                workspace_id="01HWA00000000000000000WSPV",
                user_work_role_id="01HWA00000000000000000UWV1",
                property_id="01HWA00000000000000000PRPV",
            )
        )
        session.flush()  # No IntegrityError — partial predicate held.

    def test_archive_then_re_pin(self, session: Session) -> None:
        """Soft-delete the live row, then mint a fresh re-pin.

        This is the archive + re-pin path — the partial UNIQUE must
        accept a new live row once the previous one is tombstoned.
        Walks the full lifecycle in one session so we observe the
        predicate kicking in mid-transaction.
        """
        _bootstrap_chain(
            session,
            workspace_id="01HWA00000000000000000WSPW",
            workspace_slug="uniq-rehire",
            property_id="01HWA00000000000000000PRPW",
            work_role_id="01HWA00000000000000000WRW1",
            user_work_role_id="01HWA00000000000000000UWW1",
            user_id="01HWA00000000000000000USRW",
        )
        first = _assignment(
            id="01HWA00000000000000000PWW1",
            workspace_id="01HWA00000000000000000WSPW",
            user_work_role_id="01HWA00000000000000000UWW1",
            property_id="01HWA00000000000000000PRPW",
        )
        session.add(first)
        session.flush()

        # Soft-delete the first row — the partial predicate now
        # excludes it from the UNIQUE, so the re-pin below lands.
        first.deleted_at = _LATER
        session.flush()

        session.add(
            _assignment(
                id="01HWA00000000000000000PWW2",
                workspace_id="01HWA00000000000000000WSPW",
                user_work_role_id="01HWA00000000000000000UWW1",
                property_id="01HWA00000000000000000PRPW",
            )
        )
        session.flush()  # No IntegrityError — re-pin accepted.

    def test_same_uwr_different_property_accepted(self, session: Session) -> None:
        """A user_work_role may pin to several properties at once.

        §05 records two assignments for the same maid (Villa Sud +
        Apt 3B) — the canonical example. Each pin is a separate row.
        """
        _seed_workspace(session, "01HWA00000000000000000WSPX", "multi-property")
        _seed_property(session, "01HWA00000000000000000PRPX")
        _seed_property(session, "01HWA00000000000000000PRPY")
        _seed_work_role(
            session,
            role_id="01HWA00000000000000000WRX1",
            workspace_id="01HWA00000000000000000WSPX",
        )
        _seed_user_work_role(
            session,
            user_work_role_id="01HWA00000000000000000UWX1",
            user_id="01HWA00000000000000000USRX",
            workspace_id="01HWA00000000000000000WSPX",
            work_role_id="01HWA00000000000000000WRX1",
        )
        session.add(
            _assignment(
                id="01HWA00000000000000000PWX1",
                workspace_id="01HWA00000000000000000WSPX",
                user_work_role_id="01HWA00000000000000000UWX1",
                property_id="01HWA00000000000000000PRPX",
            )
        )
        session.add(
            _assignment(
                id="01HWA00000000000000000PWX2",
                workspace_id="01HWA00000000000000000WSPX",
                user_work_role_id="01HWA00000000000000000UWX1",
                property_id="01HWA00000000000000000PRPY",
            )
        )
        session.flush()  # No IntegrityError — same uwr, two properties.


# ---------------------------------------------------------------------------
# Soft-delete tombstone — round-trip + default-list filtering
# ---------------------------------------------------------------------------


class TestSoftDeleteTombstone:
    """``deleted_at`` round-trips and excludes from the live-list filter."""

    def test_soft_delete_excludes_from_default_select(self, session: Session) -> None:
        """A ``WHERE deleted_at IS NULL`` filter hides tombstoned rows.

        The schema doesn't auto-filter — the service layer is
        responsible for the predicate. This test pins the
        contract on what the live-list path emits: tombstoned
        rows are excluded, live rows are returned.
        """
        _bootstrap_chain(
            session,
            workspace_id="01HWA00000000000000000WSPS",
            workspace_slug="soft",
            property_id="01HWA00000000000000000PRPS",
            work_role_id="01HWA00000000000000000WRS1",
            user_work_role_id="01HWA00000000000000000UWS1",
            user_id="01HWA00000000000000000USRS",
        )
        # Live row.
        session.add(
            _assignment(
                id="01HWA00000000000000000PWS1",
                workspace_id="01HWA00000000000000000WSPS",
                user_work_role_id="01HWA00000000000000000UWS1",
                property_id="01HWA00000000000000000PRPS",
            )
        )
        # Pre-tombstoned row at a different property so the partial
        # UNIQUE doesn't fight us.
        _seed_property(session, "01HWA00000000000000000PRPT")
        session.add(
            _assignment(
                id="01HWA00000000000000000PWS2",
                workspace_id="01HWA00000000000000000WSPS",
                user_work_role_id="01HWA00000000000000000UWS1",
                property_id="01HWA00000000000000000PRPT",
                deleted_at=_LATER,
            )
        )
        session.flush()
        session.expire_all()

        live = session.scalars(
            select(PropertyWorkRoleAssignment).where(
                PropertyWorkRoleAssignment.workspace_id == "01HWA00000000000000000WSPS",
                PropertyWorkRoleAssignment.deleted_at.is_(None),
            )
        ).all()
        assert {r.id for r in live} == {"01HWA00000000000000000PWS1"}, (
            "live-list path leaked a tombstoned row — check the "
            "deleted_at column persists / round-trips correctly"
        )

    def test_tombstone_timestamp_roundtrips(self, session: Session) -> None:
        """Setting ``deleted_at`` and reading it back round-trips cleanly."""
        _bootstrap_chain(
            session,
            workspace_id="01HWA00000000000000000WSPR",
            workspace_slug="ts-rt",
            property_id="01HWA00000000000000000PRPR",
            work_role_id="01HWA00000000000000000WRR1",
            user_work_role_id="01HWA00000000000000000UWR2",
            user_id="01HWA00000000000000000USRR",
        )
        row = _assignment(
            id="01HWA00000000000000000PWRT",
            workspace_id="01HWA00000000000000000WSPR",
            user_work_role_id="01HWA00000000000000000UWR2",
            property_id="01HWA00000000000000000PRPR",
        )
        session.add(row)
        session.flush()

        row.deleted_at = _LATER
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(PropertyWorkRoleAssignment).where(
                PropertyWorkRoleAssignment.id == "01HWA00000000000000000PWRT"
            )
        ).one()
        # SQLite ``DateTime(timezone=True)`` strips tzinfo on read
        # with the default driver; PG keeps it. Compare wall-clock
        # components.
        assert loaded.deleted_at is not None
        assert loaded.deleted_at.replace(tzinfo=None) == _LATER.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Workspace scoping + cross-workspace isolation
# ---------------------------------------------------------------------------


class TestWorkspaceScoping:
    """A row owned by workspace A is visible under an A-scoped SELECT.

    The unit slice exercises the schema-level ``workspace_id``
    discriminator rather than the ORM tenant filter (integration-
    tested in :mod:`tests.integration.test_db_places`). The point
    here is that the column is populated correctly and a manual
    ``WHERE workspace_id = A`` returns A-owned rows.
    """

    def test_workspace_scoping(self, session: Session) -> None:
        _bootstrap_chain(
            session,
            workspace_id="01HWA00000000000000000WSPN",
            workspace_slug="scoping",
            property_id="01HWA00000000000000000PRPN",
            work_role_id="01HWA00000000000000000WRN1",
            user_work_role_id="01HWA00000000000000000UWN1",
            user_id="01HWA00000000000000000USRN",
        )
        session.add(
            _assignment(
                id="01HWA00000000000000000PWN1",
                workspace_id="01HWA00000000000000000WSPN",
                user_work_role_id="01HWA00000000000000000UWN1",
                property_id="01HWA00000000000000000PRPN",
            )
        )
        session.flush()
        session.expire_all()

        rows = session.scalars(
            select(PropertyWorkRoleAssignment).where(
                PropertyWorkRoleAssignment.workspace_id == "01HWA00000000000000000WSPN"
            )
        ).all()
        assert {r.id for r in rows} == {"01HWA00000000000000000PWN1"}


class TestCrossWorkspaceIsolation:
    """A row owned by workspace B is invisible to a SELECT for A.

    Two workspaces, two assignment rows on different (uwr, property)
    chains; manual filtering must split them cleanly.
    """

    def test_cross_workspace_isolation(self, session: Session) -> None:
        _bootstrap_chain(
            session,
            workspace_id="01HWA00000000000000000WSAA",
            workspace_slug="iso-a",
            property_id="01HWA00000000000000000PRAA",
            work_role_id="01HWA00000000000000000WRA2",
            user_work_role_id="01HWA00000000000000000UWAA",
            user_id="01HWA00000000000000000USAA",
        )
        _bootstrap_chain(
            session,
            workspace_id="01HWA00000000000000000WSBB",
            workspace_slug="iso-b",
            property_id="01HWA00000000000000000PRBB",
            work_role_id="01HWA00000000000000000WRB2",
            user_work_role_id="01HWA00000000000000000UWBB",
            user_id="01HWA00000000000000000USBB",
        )
        session.add(
            _assignment(
                id="01HWA00000000000000000PWAA",
                workspace_id="01HWA00000000000000000WSAA",
                user_work_role_id="01HWA00000000000000000UWAA",
                property_id="01HWA00000000000000000PRAA",
            )
        )
        session.add(
            _assignment(
                id="01HWA00000000000000000PWBB",
                workspace_id="01HWA00000000000000000WSBB",
                user_work_role_id="01HWA00000000000000000UWBB",
                property_id="01HWA00000000000000000PRBB",
            )
        )
        session.flush()
        session.expire_all()

        rows_a = session.scalars(
            select(PropertyWorkRoleAssignment).where(
                PropertyWorkRoleAssignment.workspace_id == "01HWA00000000000000000WSAA"
            )
        ).all()
        ids_a = {r.id for r in rows_a}
        assert ids_a == {"01HWA00000000000000000PWAA"}, (
            "A-scoped SELECT returned a B-owned row — workspace_id "
            "is not discriminating correctly"
        )

        rows_b = session.scalars(
            select(PropertyWorkRoleAssignment).where(
                PropertyWorkRoleAssignment.workspace_id == "01HWA00000000000000000WSBB"
            )
        ).all()
        assert {r.id for r in rows_b} == {"01HWA00000000000000000PWBB"}


# ---------------------------------------------------------------------------
# FK cascade on SQLite — deleting the parent property sweeps assignments
# ---------------------------------------------------------------------------


class TestPropertyCascade:
    """Hard-deleting the parent property sweeps assignment rows.

    SQLite only honours ``ON DELETE CASCADE`` when the connection
    has ``PRAGMA foreign_keys=ON`` — the default unit fixture skips
    that pragma (schema shape is the concern there), so this class
    uses the dedicated :func:`fk_engine` fixture that mirrors the
    production hook from :mod:`app.adapters.db.session`.
    """

    def test_property_delete_cascades_to_assignment(self, fk_engine: Engine) -> None:
        factory = sessionmaker(bind=fk_engine, expire_on_commit=False, class_=Session)
        with factory() as session:
            _bootstrap_chain(
                session,
                workspace_id="01HWA00000000000000000WSCP",
                workspace_slug="cascade-prop",
                property_id="01HWA00000000000000000PRCP",
                work_role_id="01HWA00000000000000000WRCP",
                user_work_role_id="01HWA00000000000000000UWCP",
                user_id="01HWA00000000000000000USCP",
            )
            session.add(
                _assignment(
                    id="01HWA00000000000000000PWCP",
                    workspace_id="01HWA00000000000000000WSCP",
                    user_work_role_id="01HWA00000000000000000UWCP",
                    property_id="01HWA00000000000000000PRCP",
                )
            )
            session.flush()

            prop = session.get(Property, "01HWA00000000000000000PRCP")
            assert prop is not None
            session.delete(prop)
            session.flush()
            session.expire_all()

            remaining = session.scalars(
                select(PropertyWorkRoleAssignment).where(
                    PropertyWorkRoleAssignment.property_id
                    == "01HWA00000000000000000PRCP"
                )
            ).all()
            assert remaining == [], (
                "assignment rows survived a property hard-delete — "
                "FK ON DELETE CASCADE is not firing"
            )

    def test_user_work_role_delete_cascades_to_assignment(
        self, fk_engine: Engine
    ) -> None:
        """Hard-deleting the parent ``user_work_role`` sweeps assignments."""
        factory = sessionmaker(bind=fk_engine, expire_on_commit=False, class_=Session)
        with factory() as session:
            _bootstrap_chain(
                session,
                workspace_id="01HWA00000000000000000WSCU",
                workspace_slug="cascade-uwr",
                property_id="01HWA00000000000000000PRCU",
                work_role_id="01HWA00000000000000000WRCU",
                user_work_role_id="01HWA00000000000000000UWCU",
                user_id="01HWA00000000000000000USCU",
            )
            session.add(
                _assignment(
                    id="01HWA00000000000000000PWCU",
                    workspace_id="01HWA00000000000000000WSCU",
                    user_work_role_id="01HWA00000000000000000UWCU",
                    property_id="01HWA00000000000000000PRCU",
                )
            )
            session.flush()

            uwr = session.get(UserWorkRole, "01HWA00000000000000000UWCU")
            assert uwr is not None
            session.delete(uwr)
            session.flush()
            session.expire_all()

            remaining = session.scalars(
                select(PropertyWorkRoleAssignment).where(
                    PropertyWorkRoleAssignment.user_work_role_id
                    == "01HWA00000000000000000UWCU"
                )
            ).all()
            assert remaining == [], (
                "assignment rows survived a user_work_role hard-delete — "
                "FK ON DELETE CASCADE is not firing"
            )


# ---------------------------------------------------------------------------
# Soft import-time guarantee on the existing places exports
# ---------------------------------------------------------------------------


def test_existing_places_exports_still_importable() -> None:
    """The package still re-exports the v1 places classes post cd-e4m3.

    cd-e4m3 expanded :data:`__all__`; this sanity check guards
    against a future refactor accidentally dropping the older
    exports.
    """
    assert Property.__tablename__ == "property"
    assert PropertyWorkspace.__tablename__ == "property_workspace"
