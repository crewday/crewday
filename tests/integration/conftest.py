"""Integration-layer pytest fixtures.

Per ``docs/specs/17-testing-quality.md`` §"Integration":

* Each test gets a session bound to a per-test transaction that is
  rolled back at teardown — no ``TRUNCATE`` dance needed because the
  rollback naturally reverts every insert/update/delete.
* SQLite migrations run once into a shared template DB and each xdist
  worker gets a file copy. Non-SQLite and explicit URL overrides still
  run ``alembic upgrade head`` against the test engine.
* Two backend-selection knobs (cd-rhaj):
    - ``CREWDAY_TEST_DB={sqlite,postgres}`` picks the backend.
      ``sqlite`` (default) returns a per-session temp-file SQLite
      URL. ``postgres`` spins up a session-scoped
      :class:`testcontainers.postgres.PostgresContainer` (image
      ``postgres:15-alpine``) and returns its connection URL, with
      the ``psycopg2`` driver prefix rewritten to ``psycopg`` (v3,
      which is what this project pins — see
      ``app/adapters/db/session.py::normalise_sync_url``).
    - ``CREWDAY_TEST_DATABASE_URL`` is an explicit URL override and
      wins over the selector. Use it when CI has already started a
      PG service outside the test process.
  The backend defaults to a fresh file-based SQLite under pytest's
  ``tmp_path_factory`` because alembic's ``env.py`` creates its own
  engine from the URL — an ``sqlite:///:memory:`` URL would hand
  alembic a different in-memory DB than the one the test holds. A
  temp file lets both sides see the same bytes without any special
  plumbing.

See ``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.session import make_engine, normalise_sync_url
from app.config import get_settings

_PREMIGRATED_URL_ENV = "CREWDAY_TEST_DB_PREMIGRATED_URL"


def _alembic_ini() -> Path:
    """Return the repo-root ``alembic.ini`` path."""
    return Path(__file__).resolve().parents[2] / "alembic.ini"


def _backend() -> str:
    """Return the current backend name (``sqlite`` or ``postgres``).

    Resolves ``CREWDAY_TEST_DB`` (case-insensitive) with a ``sqlite``
    default. Raised into a function so collection-time and
    fixture-time paths agree on the value.
    """
    return os.environ.get("CREWDAY_TEST_DB", "sqlite").lower()


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path}"


@contextmanager
def _override_database_url(url: str) -> Iterator[None]:
    """Point Alembic's settings lookup at ``url`` for one operation."""
    original = os.environ.get("CREWDAY_DATABASE_URL")
    os.environ["CREWDAY_DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("CREWDAY_DATABASE_URL", None)
        else:
            os.environ["CREWDAY_DATABASE_URL"] = original
        get_settings.cache_clear()


def _run_migrations(url: str) -> None:
    with _override_database_url(url):
        cfg = AlembicConfig(str(_alembic_ini()))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")


def _shared_tmp_root(tmp_path_factory: pytest.TempPathFactory, worker_id: str) -> Path:
    """Return a temp directory shared by all xdist workers in this run."""
    base = tmp_path_factory.getbasetemp()
    if worker_id == "master":
        return base
    return base.parent


def _sqlite_template_path(
    tmp_path_factory: pytest.TempPathFactory, worker_id: str
) -> Path:
    """Build a migrated SQLite template once, then reuse it per worker."""
    root = _shared_tmp_root(tmp_path_factory, worker_id)
    template_dir = root / "crewday-sqlite-template"
    template_db = template_dir / "template.db"
    ready = template_dir / "ready"
    lock = template_dir / "lock"

    template_dir.mkdir(exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        deadline = time.monotonic() + 120
        while not ready.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for migrated SQLite template at {template_db}"
                ) from None
            time.sleep(0.05)
        return template_db

    try:
        if not ready.exists():
            if template_db.exists():
                template_db.unlink()
            _run_migrations(_sqlite_url(template_db))
            ready.write_text("ok\n", encoding="utf-8")
        return template_db
    finally:
        with suppress(OSError):
            lock.rmdir()


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip ``pg_only`` tests when the backend is SQLite.

    Tests that exercise Postgres-only behaviour (RLS predicates,
    PG-specific SQL, etc.) carry the ``pg_only`` marker. On the
    SQLite shard we skip them at collection time so they don't try
    to run against a backend that can't satisfy the prerequisite.
    """
    if _backend() != "sqlite":
        return
    skip_pg = pytest.mark.skip(reason="PG-only test; CREWDAY_TEST_DB=sqlite")
    for item in items:
        if "pg_only" in item.keywords:
            item.add_marker(skip_pg)


@pytest.fixture(scope="session")
def db_url(tmp_path_factory: pytest.TempPathFactory, worker_id: str) -> Iterator[str]:
    """Session-scoped test DB URL.

    Honours ``CREWDAY_TEST_DATABASE_URL`` (explicit URL override, used
    when CI already owns the container) first, then falls back to
    the ``CREWDAY_TEST_DB`` backend selector:

    * ``sqlite`` (default): a fresh file-based SQLite under pytest's
      ``tmp_path_factory`` root. Under ``pytest-xdist`` each worker
      gets its own file (``test-<worker_id>.db``) so parallel workers
      don't stomp on a shared SQLite file (locks + corruption).
    * ``postgres``: a session-scoped :class:`PostgresContainer`
      (``postgres:15-alpine``). The container is torn down at
      session end. Under xdist each worker stands up its own
      container — testcontainers picks a free host port per
      container, so there's no collision. We pass
      ``driver="psycopg"`` so testcontainers emits a
      ``postgresql+psycopg://`` URL directly — this repo pins
      psycopg 3, not psycopg2, and pinning the driver via the
      constructor avoids a brittle string substitution on the
      returned URL.

    ``worker_id`` is the built-in pytest-xdist fixture; when xdist is
    not active it resolves to the literal ``"master"``.
    """
    override = os.environ.get("CREWDAY_TEST_DATABASE_URL")
    if override:
        yield override
        return

    backend = _backend()
    if backend == "sqlite":
        root = tmp_path_factory.mktemp("crewday-db")
        db_path = root / f"test-{worker_id}.db"
        template = _sqlite_template_path(tmp_path_factory, worker_id)
        shutil.copy2(template, db_path)
        url = _sqlite_url(db_path)
        os.environ[_PREMIGRATED_URL_ENV] = url
        try:
            yield url
        finally:
            if os.environ.get(_PREMIGRATED_URL_ENV) == url:
                os.environ.pop(_PREMIGRATED_URL_ENV, None)
        return

    if backend == "postgres":
        # Imported lazily so the sqlite shard never pulls the docker
        # client (and fails ImportError-style on machines without it).
        from testcontainers.postgres import PostgresContainer

        # ``driver="psycopg"`` makes the container emit a
        # ``postgresql+psycopg://`` URL (psycopg 3, the driver this
        # repo actually pins). Without it testcontainers defaults to
        # ``+psycopg2`` which we don't install. Running the URL
        # through ``normalise_sync_url`` is belt-and-braces — it's a
        # no-op on a ``+psycopg`` URL but keeps parity with the
        # production URL-normalisation path.
        with PostgresContainer("postgres:15-alpine", driver="psycopg") as container:
            yield normalise_sync_url(container.get_connection_url())
        return

    raise ValueError(
        f"Unknown CREWDAY_TEST_DB={backend!r}; expected 'sqlite' or 'postgres'"
    )


@pytest.fixture(scope="session")
def engine(db_url: str) -> Iterator[Engine]:
    """Session-scoped SQLAlchemy engine bound to :func:`db_url`.

    Shared across every integration test; disposal happens at session
    teardown. The pysqlite SAVEPOINT-isolation workaround needed by
    :func:`db_session` is applied per-connection inside that fixture
    rather than via engine-wide listeners — see :func:`db_session`
    for why.
    """
    eng = make_engine(db_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrate_once(engine: Engine, db_url: str) -> Iterator[None]:
    """Run ``alembic upgrade head`` exactly once per worker.

    ``migrations/env.py`` reads the DB URL from
    :func:`app.config.get_settings` rather than from the Alembic
    config file (§01 "Migrations stay shared"), so we set
    ``CREWDAY_DATABASE_URL`` in the process environment for the
    duration of the upgrade and clear ``get_settings``'s lru_cache
    either side so the test URL actually reaches ``env.py``. The
    original value (if any) is restored on teardown.

    Current state has no migration revisions, so this is a no-op on a
    fresh checkout — the fixture still exercises the alembic wiring
    so the first real migration (cd-w7h successors) lights up tests
    immediately.
    """
    if os.environ.get(_PREMIGRATED_URL_ENV) == db_url:
        yield
        return

    _run_migrations(db_url)
    yield


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """Function-scoped DB session wrapped in a rollback-on-exit transaction.

    Opens a top-level connection + transaction, binds a
    :class:`Session` to that connection with ``join_transaction_mode=
    "create_savepoint"``, yields it to the test, then rolls back
    everything — including savepoints from nested ``commit()`` calls
    — on teardown.

    This is the "SAVEPOINT per test" pattern the SQLAlchemy docs call
    out as the canonical way to isolate tests without per-test
    schema reset. Much faster than ``TRUNCATE`` on Postgres and
    completely free on SQLite.

    **SQLite (pysqlite) workaround.** pysqlite carries a long-standing
    isolation-level quirk: it auto-issues ``BEGIN`` / ``COMMIT`` around
    statements and treats ``RELEASE SAVEPOINT`` as a transaction
    boundary, which auto-commits the outer transaction's writes — so
    INSERTs from one test leak into the next under the SAVEPOINT
    pattern alone. The SQLAlchemy docs document the fix under
    `Serializable isolation / Savepoints / Transactional DDL
    <https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#serializable-isolation-savepoints-transactional-ddl>`_:
    set the DBAPI ``isolation_level`` to ``None`` to disable pysqlite's
    autobegin, then manage ``BEGIN`` explicitly.

    Rather than attach those as engine-wide event listeners (the docs'
    canonical shape), we apply the workaround on this single
    connection only. The broader integration suite holds many
    ``session_factory()`` sessions against the same shared engine
    that don't use SAVEPOINTs and rely on pysqlite's legacy autobegin
    — global engine listeners would either fire spurious nested
    ``BEGIN`` statements on those sessions ("cannot start a
    transaction within a transaction") or break their own commit
    bookkeeping. Keeping the tweak per-connection isolates the fix
    to exactly the connection that needs it.

    The Postgres path is untouched and uses SQLAlchemy's normal
    transaction wrapper.
    """
    raw_connection = engine.connect()
    is_sqlite = engine.dialect.name == "sqlite"
    try:
        if is_sqlite:
            dbapi_conn = raw_connection.connection.dbapi_connection
            assert isinstance(dbapi_conn, sqlite3.Connection)
            dbapi_conn.isolation_level = None
            raw_connection.exec_driver_sql("BEGIN")
        else:
            raw_connection.begin()

        factory = sessionmaker(
            bind=raw_connection,
            expire_on_commit=False,
            class_=Session,
            join_transaction_mode="create_savepoint",
        )
        session = factory()
        try:
            yield session
        finally:
            session.close()
            if is_sqlite:
                # The DBAPI rollback unwinds the explicit BEGIN we
                # issued above (and every SAVEPOINT layered on it).
                # SQLAlchemy does record a ``RootTransaction`` for the
                # ``exec_driver_sql("BEGIN")`` call, so its own
                # ``rollback()`` would also unwind correctly — going
                # through the DBAPI keeps the rollback paired with
                # the ``isolation_level`` restore that has to happen
                # at the same layer.
                dbapi_conn = raw_connection.connection.dbapi_connection
                assert isinstance(dbapi_conn, sqlite3.Connection)
                try:
                    dbapi_conn.rollback()
                finally:
                    # Restore pysqlite's default autobegin mode so the
                    # pool can hand the connection back to a sibling
                    # caller that relies on that shape.
                    dbapi_conn.isolation_level = ""
            else:
                outer = raw_connection.get_transaction()
                if outer is not None and outer.is_active:
                    outer.rollback()
    finally:
        # Always release the pool connection. On the SQLite path,
        # also defensively restore ``isolation_level`` even if setup
        # raised before the inner try block — otherwise a poisoned
        # connection returns to the pool with autobegin disabled and
        # breaks sibling checkouts.
        if is_sqlite:
            dbapi_conn = raw_connection.connection.dbapi_connection
            if isinstance(dbapi_conn, sqlite3.Connection):
                dbapi_conn.isolation_level = ""
        raw_connection.close()
