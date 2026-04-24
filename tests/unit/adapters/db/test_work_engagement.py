"""Unit tests for :class:`WorkEngagement` (cd-4saj).

Covers the SQLAlchemy mapped class from
:mod:`app.adapters.db.workspace.models`:

* re-import idempotence (landing the module twice must not redefine
  the table);
* tablename + ``__table_args__`` shape (CHECKs, partial UNIQUE,
  hot-path indexes, registry membership);
* in-memory SQLite round-trip, including the ``engagement_kind``
  enum CHECK, the ``supplier_org_id`` pairing CHECK, the partial
  UNIQUE on ``(user_id, workspace_id) WHERE archived_on IS NULL``,
  and the soft-archive round-trip;
* cross-workspace insert isolation so a row owned by workspace B is
  invisible to a SELECT scoped to workspace A;
* workspace hard-delete cascade on SQLite, using a dedicated
  ``fk_engine`` fixture that installs ``PRAGMA foreign_keys=ON``
  (the cd-4saj self-review asked for explicit cascade coverage
  because the sibling ``user_work_role`` also cascades on the same
  parent).

Remaining integration-only coverage (schema fingerprint parity,
CHECK behaviour on Postgres, tenant-filter behaviour) stays in
``tests/integration/test_db_workspace.py`` and
``tests/integration/test_schema_parity.py``.

See ``docs/specs/02-domain-model.md`` §"work_engagement" and
``docs/specs/22-clients-and-vendors.md`` §"Engagement kinds".
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

from app.adapters.db.base import Base

# Importing the package (not just ``.models``) is critical so the
# tenancy-registry side effect fires; the cross-workspace test then
# observes the table is registered.
from app.adapters.db.workspace import (
    WorkEngagement,
    Workspace,
)
from app.tenancy import registry

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
_TODAY = date(2026, 4, 24)
_TOMORROW = date(2026, 4, 25)


# ---------------------------------------------------------------------------
# Engine fixture — in-memory SQLite shared across the test
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so cross-package
    FKs resolve on a bare ``Base.metadata.create_all``.

    Mirrors the sibling helper in :mod:`tests.unit.adapters.db.test_work_role`
    — without this step a test run order that imports a later context
    first could leave ``Base.metadata`` with dangling FKs.
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

    The default unit fixture matches the package-wide convention of
    exercising schema shape without the production engine factory's
    ``PRAGMA foreign_keys=ON`` hook (FK cascade coverage is normally
    delegated to the integration tier — see the module docstring).
    Cascade-delete coverage for cd-4saj is called out explicitly in
    the self-review checklist, so this fixture mirrors the production
    hook from :func:`app.adapters.db.session._enable_sqlite_foreign_keys`
    just for the cascade test. Scoped to a single test class so the
    rest of the suite still runs on the FK-off default.
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
        # events, so we still re-issue the PRAGMA every time we see a
        # raw sqlite3 connection — same defensive guard as the
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
    shape, not the filter wiring (integration coverage lives in
    :mod:`tests.tenant.test_repository_parity`).
    """
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


def _engagement(
    *,
    id: str,
    user_id: str,
    workspace_id: str,
    engagement_kind: str = "payroll",
    supplier_org_id: str | None = None,
    archived_on: date | None = None,
    started_on: date = _TODAY,
) -> WorkEngagement:
    """Factory helper — most tests only vary a couple of fields."""
    return WorkEngagement(
        id=id,
        user_id=user_id,
        workspace_id=workspace_id,
        engagement_kind=engagement_kind,
        supplier_org_id=supplier_org_id,
        started_on=started_on,
        archived_on=archived_on,
        created_at=_PINNED,
        updated_at=_PINNED,
    )


# ---------------------------------------------------------------------------
# Pure-Python construction shape
# ---------------------------------------------------------------------------


class TestWorkEngagementModelShape:
    """The ``WorkEngagement`` mapped class carries the cd-4saj v1 slice."""

    def test_minimal_construction(self) -> None:
        row = WorkEngagement(
            id="01HWA00000000000000000WEN1",
            user_id="01HWA00000000000000000USRA",
            workspace_id="01HWA00000000000000000WSPA",
            engagement_kind="payroll",
            started_on=_TODAY,
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        assert row.id == "01HWA00000000000000000WEN1"
        assert row.user_id == "01HWA00000000000000000USRA"
        assert row.workspace_id == "01HWA00000000000000000WSPA"
        assert row.engagement_kind == "payroll"
        assert row.started_on == _TODAY
        assert row.created_at == _PINNED
        assert row.updated_at == _PINNED
        # Optional columns default to ``None`` until set.
        assert row.supplier_org_id is None
        assert row.pay_destination_id is None
        assert row.reimbursement_destination_id is None
        assert row.archived_on is None

    def test_tablename(self) -> None:
        assert WorkEngagement.__tablename__ == "work_engagement"

    def test_engagement_kind_check_present(self) -> None:
        """``__table_args__`` carries the engagement-kind CHECK.

        The naming convention in :mod:`app.adapters.db.base` expands
        ``ck`` to ``ck_%(table_name)s_%(constraint_name)s``, so the
        short ``engagement_kind`` local name resolves to the fully
        qualified ``ck_work_engagement_engagement_kind`` at DDL time.
        """
        checks = [
            c for c in WorkEngagement.__table_args__ if isinstance(c, CheckConstraint)
        ]
        names = [c.name for c in checks]
        assert "ck_work_engagement_engagement_kind" in names

    def test_supplier_pairing_check_present(self) -> None:
        """``__table_args__`` carries the supplier pairing CHECK."""
        checks = [
            c for c in WorkEngagement.__table_args__ if isinstance(c, CheckConstraint)
        ]
        names = [c.name for c in checks]
        assert "ck_work_engagement_supplier_org_pairing" in names

    def test_partial_unique_index_present(self) -> None:
        """``uq_work_engagement_user_workspace_active`` carries the partial predicate.

        Both the SQLite and PG dialect kwargs must be set so Alembic
        and ``Base.metadata.create_all`` agree on the same WHERE
        predicate (``archived_on IS NULL``).
        """
        indexes = [i for i in WorkEngagement.__table_args__ if isinstance(i, Index)]
        target = next(
            i for i in indexes if i.name == "uq_work_engagement_user_workspace_active"
        )
        assert target.unique is True
        assert [c.name for c in target.columns] == ["user_id", "workspace_id"]
        # The partial predicate is dialect-scoped; both halves must
        # be present so Alembic + ``create_all`` agree on shape.
        assert target.dialect_kwargs.get("sqlite_where") is not None
        assert target.dialect_kwargs.get("postgresql_where") is not None

    def test_hot_path_indexes_present(self) -> None:
        """Both ``(workspace_id, user_id)`` and ``(workspace_id, archived_on)``."""
        indexes: dict[str, list[str]] = {
            str(i.name): [c.name for c in i.columns]
            for i in WorkEngagement.__table_args__
            if isinstance(i, Index) and not i.unique
        }
        assert indexes["ix_work_engagement_workspace_user"] == [
            "workspace_id",
            "user_id",
        ]
        assert indexes["ix_work_engagement_workspace_archived"] == [
            "workspace_id",
            "archived_on",
        ]


class TestRegistryMembership:
    """``work_engagement`` is registered as scoped."""

    def test_work_engagement_registered(self) -> None:
        assert registry.is_scoped("work_engagement")


# ---------------------------------------------------------------------------
# Idempotent re-import — landing the module twice must not redefine the table
# ---------------------------------------------------------------------------


class TestModuleReimportIdempotent:
    """A second ``import`` of the package does not redefine the tables.

    Re-importing a SQLAlchemy module that already populated
    :attr:`Base.metadata` raises
    ``InvalidRequestError: Table 'work_engagement' is already defined``
    if the module body re-declares the class on top of an existing one.
    This test guards against a refactor that would inadvertently
    re-define the tables (e.g. by collapsing to a ``del`` + reload).
    """

    def test_reimport_does_not_raise(self) -> None:
        import importlib

        import app.adapters.db.workspace as ws_pkg

        # Force-reimport via importlib so the cached module is
        # re-executed; a redefinition error on the mapped class
        # would raise inside ``importlib.reload``.
        importlib.reload(ws_pkg)


# ---------------------------------------------------------------------------
# Round-trip + engagement_kind CHECK + supplier-pairing CHECK on SQLite
# ---------------------------------------------------------------------------


class TestWorkEngagementRoundTrip:
    """Insert + reload exercises the round-trip path on SQLite."""

    def test_insert_then_read_back(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPA", "alpha")
        row = WorkEngagement(
            id="01HWA00000000000000000WERT",
            user_id="01HWA00000000000000000USRA",
            workspace_id="01HWA00000000000000000WSPA",
            engagement_kind="contractor",
            pay_destination_id="01HWA00000000000000000PAYX",
            reimbursement_destination_id="01HWA00000000000000000REIY",
            started_on=_TODAY,
            notes_md="initial contractor engagement",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        session.add(row)
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(WorkEngagement).where(
                WorkEngagement.id == "01HWA00000000000000000WERT"
            )
        ).one()
        assert loaded.engagement_kind == "contractor"
        assert loaded.pay_destination_id == "01HWA00000000000000000PAYX"
        assert loaded.reimbursement_destination_id == "01HWA00000000000000000REIY"
        assert loaded.supplier_org_id is None
        assert loaded.archived_on is None
        assert loaded.notes_md == "initial contractor engagement"

    def test_soft_archive_roundtrips(self, session: Session) -> None:
        """Setting ``archived_on`` and reading it back round-trips cleanly."""
        _seed_workspace(session, "01HWA00000000000000000WSPB", "bravo")
        session.add(
            _engagement(
                id="01HWA00000000000000000WESA",
                user_id="01HWA00000000000000000USRB",
                workspace_id="01HWA00000000000000000WSPB",
            )
        )
        session.flush()

        row = session.scalars(
            select(WorkEngagement).where(
                WorkEngagement.id == "01HWA00000000000000000WESA"
            )
        ).one()
        row.archived_on = _TOMORROW
        session.flush()
        session.expire_all()

        loaded = session.scalars(
            select(WorkEngagement).where(
                WorkEngagement.id == "01HWA00000000000000000WESA"
            )
        ).one()
        assert loaded.archived_on == _TOMORROW


class TestEngagementKindCheck:
    """The ``engagement_kind`` CHECK rejects unknown values."""

    def test_unknown_kind_rejected(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSCK", "check-kind")
        session.add(
            WorkEngagement(
                id="01HWA00000000000000000WECK",
                user_id="01HWA00000000000000000USCK",
                workspace_id="01HWA00000000000000000WSCK",
                engagement_kind="intern",  # not in the enum
                started_on=_TODAY,
                created_at=_PINNED,
                updated_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


class TestSupplierOrgPairingCheck:
    """The biconditional CHECK on ``supplier_org_id`` + ``engagement_kind``.

    §02 records "supplier_org_id required iff engagement_kind =
    'agency_supplied'". Both halves of the biconditional must fail
    at the DB — an ``agency_supplied`` row without a supplier is a
    half-wired pipeline, a ``payroll`` / ``contractor`` row with a
    supplier is a UX bug waiting to happen.
    """

    def test_agency_without_supplier_rejected(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSP1", "agency-no-sup")
        session.add(
            _engagement(
                id="01HWA00000000000000000WE01",
                user_id="01HWA00000000000000000US01",
                workspace_id="01HWA00000000000000000WSP1",
                engagement_kind="agency_supplied",
                supplier_org_id=None,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_payroll_with_supplier_rejected(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSP2", "pay-with-sup")
        session.add(
            _engagement(
                id="01HWA00000000000000000WE02",
                user_id="01HWA00000000000000000US02",
                workspace_id="01HWA00000000000000000WSP2",
                engagement_kind="payroll",
                supplier_org_id="01HWA00000000000000000ORGX",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_contractor_with_supplier_rejected(self, session: Session) -> None:
        """Symmetry — contractor must not carry a supplier either."""
        _seed_workspace(session, "01HWA00000000000000000WSP3", "con-with-sup")
        session.add(
            _engagement(
                id="01HWA00000000000000000WE03",
                user_id="01HWA00000000000000000US03",
                workspace_id="01HWA00000000000000000WSP3",
                engagement_kind="contractor",
                supplier_org_id="01HWA00000000000000000ORGY",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_agency_with_supplier_accepted(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSP4", "agency-ok")
        session.add(
            _engagement(
                id="01HWA00000000000000000WE04",
                user_id="01HWA00000000000000000US04",
                workspace_id="01HWA00000000000000000WSP4",
                engagement_kind="agency_supplied",
                supplier_org_id="01HWA00000000000000000ORGZ",
            )
        )
        session.flush()  # No IntegrityError — the happy path.

    def test_payroll_without_supplier_accepted(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSP5", "pay-ok")
        session.add(
            _engagement(
                id="01HWA00000000000000000WE05",
                user_id="01HWA00000000000000000US05",
                workspace_id="01HWA00000000000000000WSP5",
                engagement_kind="payroll",
                supplier_org_id=None,
            )
        )
        session.flush()  # No IntegrityError — the happy path.


# ---------------------------------------------------------------------------
# Partial UNIQUE — one active engagement per (user, workspace)
# ---------------------------------------------------------------------------


class TestActiveEngagementPartialUnique:
    """``(user_id, workspace_id) WHERE archived_on IS NULL`` UNIQUE.

    SQLite 3.8+ respects the partial predicate; PG always does. This
    class is the unit guard that the predicate lands on the SQLite
    side; integration-level coverage (PG parity) is handled by
    :mod:`tests.integration.test_schema_parity`.
    """

    def test_two_active_rows_same_user_workspace_rejected(
        self, session: Session
    ) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSPU", "uniq-two")
        session.add(
            _engagement(
                id="01HWA00000000000000000WEU1",
                user_id="01HWA00000000000000000USUQ",
                workspace_id="01HWA00000000000000000WSPU",
            )
        )
        session.flush()

        session.add(
            _engagement(
                id="01HWA00000000000000000WEU2",
                user_id="01HWA00000000000000000USUQ",
                workspace_id="01HWA00000000000000000WSPU",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_active_plus_archived_coexist(self, session: Session) -> None:
        """An archived row + an active row for the same (user, workspace) is OK.

        The partial predicate excludes ``archived_on IS NOT NULL``
        rows from the UNIQUE, so the archive history can stack up
        without fighting the live-row invariant.
        """
        _seed_workspace(session, "01HWA00000000000000000WSPV", "uniq-mixed")
        # Archived row first so the next INSERT lands against a
        # non-empty state.
        session.add(
            _engagement(
                id="01HWA00000000000000000WEV1",
                user_id="01HWA00000000000000000USVQ",
                workspace_id="01HWA00000000000000000WSPV",
                archived_on=_TODAY,
            )
        )
        session.flush()

        # Active row for the same (user, workspace) — must be accepted.
        session.add(
            _engagement(
                id="01HWA00000000000000000WEV2",
                user_id="01HWA00000000000000000USVQ",
                workspace_id="01HWA00000000000000000WSPV",
            )
        )
        session.flush()  # No IntegrityError — partial predicate held.

    def test_archive_then_reengage(self, session: Session) -> None:
        """Archive the current active row, then open a fresh active row.

        This is the rehire path — the partial UNIQUE must accept
        a new active row once the previous one is archived. Walks
        the full lifecycle in one session so we observe the
        predicate kicking in mid-transaction.
        """
        _seed_workspace(session, "01HWA00000000000000000WSPW", "uniq-rehire")
        first = _engagement(
            id="01HWA00000000000000000WEW1",
            user_id="01HWA00000000000000000USWQ",
            workspace_id="01HWA00000000000000000WSPW",
        )
        session.add(first)
        session.flush()

        # Archive the first row — the partial predicate now excludes
        # it from the UNIQUE, so the rehire below must land.
        first.archived_on = _TODAY
        session.flush()

        session.add(
            _engagement(
                id="01HWA00000000000000000WEW2",
                user_id="01HWA00000000000000000USWQ",
                workspace_id="01HWA00000000000000000WSPW",
                started_on=_TOMORROW,
            )
        )
        session.flush()  # No IntegrityError — rehire accepted.

    def test_same_user_different_workspace_accepted(self, session: Session) -> None:
        """A user may hold one active engagement in each of two workspaces."""
        _seed_workspace(session, "01HWA00000000000000000WSXA", "multi-a")
        _seed_workspace(session, "01HWA00000000000000000WSXB", "multi-b")
        session.add(
            _engagement(
                id="01HWA00000000000000000WEXA",
                user_id="01HWA00000000000000000USXQ",
                workspace_id="01HWA00000000000000000WSXA",
            )
        )
        session.add(
            _engagement(
                id="01HWA00000000000000000WEXB",
                user_id="01HWA00000000000000000USXQ",
                workspace_id="01HWA00000000000000000WSXB",
            )
        )
        session.flush()  # No IntegrityError — two workspaces, two rows.


# ---------------------------------------------------------------------------
# Cross-workspace isolation — manual SELECT shows row scoped by workspace
# ---------------------------------------------------------------------------


class TestCrossWorkspaceIsolation:
    """A row owned by workspace B is invisible to a SELECT for A.

    This unit test exercises the schema-level ``workspace_id``
    discriminator rather than the ORM tenant filter (integration-
    tested in :mod:`tests.tenant.test_repository_parity`). The point
    here is that the column is populated correctly and a manual
    ``WHERE workspace_id = A`` returns only A-owned rows.
    """

    def test_b_row_invisible_under_a_filter(self, session: Session) -> None:
        _seed_workspace(session, "01HWA00000000000000000WSAA", "iso-a")
        _seed_workspace(session, "01HWA00000000000000000WSBB", "iso-b")
        session.add(
            _engagement(
                id="01HWA00000000000000000WEAA",
                user_id="01HWA00000000000000000USQQ",
                workspace_id="01HWA00000000000000000WSAA",
            )
        )
        session.add(
            _engagement(
                id="01HWA00000000000000000WEBB",
                user_id="01HWA00000000000000000USQQ",
                workspace_id="01HWA00000000000000000WSBB",
            )
        )
        session.flush()
        session.expire_all()

        rows_a = session.scalars(
            select(WorkEngagement).where(
                WorkEngagement.workspace_id == "01HWA00000000000000000WSAA"
            )
        ).all()
        ids_a = {r.id for r in rows_a}
        assert ids_a == {"01HWA00000000000000000WEAA"}, (
            "A-scoped SELECT returned a B-owned row — workspace_id is "
            "not discriminating correctly"
        )

        rows_b = session.scalars(
            select(WorkEngagement).where(
                WorkEngagement.workspace_id == "01HWA00000000000000000WSBB"
            )
        ).all()
        ids_b = {r.id for r in rows_b}
        assert ids_b == {"01HWA00000000000000000WEBB"}


# ---------------------------------------------------------------------------
# FK cascade on SQLite — deleting the workspace sweeps work_engagement rows
# ---------------------------------------------------------------------------


class TestWorkspaceCascade:
    """Hard-deleting the parent workspace sweeps ``work_engagement`` rows.

    SQLite only honours ``ON DELETE CASCADE`` when the connection
    has ``PRAGMA foreign_keys=ON`` — the default unit fixture skips
    that pragma (schema shape is the concern there), so this class
    uses the dedicated :func:`fk_engine` fixture that mirrors the
    production hook from :mod:`app.adapters.db.session`. The cd-4saj
    self-review directive calls out cascade depth explicitly because
    the ``user_work_role`` sibling (cd-5kv4) cascades against the
    same ``workspace`` parent; landing two CASCADE children on the
    same parent is the shape we want proven on SQLite before it
    goes to PG.
    """

    def test_workspace_delete_cascades_to_engagement(self, fk_engine: Engine) -> None:
        factory = sessionmaker(bind=fk_engine, expire_on_commit=False, class_=Session)
        with factory() as session:
            _seed_workspace(session, "01HWA00000000000000000WSCC", "cascade-we")
            session.add(
                _engagement(
                    id="01HWA00000000000000000WECC",
                    user_id="01HWA00000000000000000USCC",
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
                select(WorkEngagement).where(
                    WorkEngagement.workspace_id == "01HWA00000000000000000WSCC"
                )
            ).all()
            assert remaining == [], (
                "work_engagement rows survived a workspace hard-delete — "
                "FK ON DELETE CASCADE is not firing"
            )
