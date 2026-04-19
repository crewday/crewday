"""Integration-layer pytest fixtures.

Per ``docs/specs/17-testing-quality.md`` §"Integration":

* Each test gets a session bound to a per-test transaction that is
  rolled back at teardown — no ``TRUNCATE`` dance needed because the
  rollback naturally reverts every insert/update/delete.
* Migrations run once per session via ``alembic upgrade head`` against
  the test engine. A worker that never reaches a real DB (for
  example, the smoke test below) pays only the fixture's setup cost;
  subsequent tests share it.
* The backend defaults to a fresh file-based SQLite under pytest's
  ``tmp_path_factory`` because alembic's ``env.py`` creates its own
  engine from the URL — an ``sqlite:///:memory:`` URL would hand
  alembic a different in-memory DB than the one the test holds. A
  temp file lets both sides see the same bytes without any special
  plumbing. Override the URL via ``CREWDAY_TEST_DATABASE_URL`` to
  target Postgres via ``testcontainers`` (tracked as cd-rhaj).

See ``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.session import make_engine
from app.config import get_settings


def _alembic_ini() -> Path:
    """Return the repo-root ``alembic.ini`` path."""
    return Path(__file__).resolve().parents[2] / "alembic.ini"


@pytest.fixture(scope="session")
def db_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Session-scoped test DB URL.

    Reads ``CREWDAY_TEST_DATABASE_URL`` from the environment when set
    so the same suite can be run against Postgres via
    ``testcontainers`` (cd-rhaj). Falls back to a fresh SQLite file
    under pytest's ``tmp_path_factory`` root.
    """
    env = os.environ.get("CREWDAY_TEST_DATABASE_URL")
    if env:
        return env
    root = tmp_path_factory.mktemp("crewday-db")
    return f"sqlite:///{root / 'test.db'}"


@pytest.fixture(scope="session")
def engine(db_url: str) -> Iterator[Engine]:
    """Session-scoped SQLAlchemy engine bound to :func:`db_url`.

    Shared across every integration test; disposal happens at session
    teardown.
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
    original = os.environ.get("CREWDAY_DATABASE_URL")
    os.environ["CREWDAY_DATABASE_URL"] = db_url
    get_settings.cache_clear()
    try:
        cfg = AlembicConfig(str(_alembic_ini()))
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")
        yield
    finally:
        if original is None:
            os.environ.pop("CREWDAY_DATABASE_URL", None)
        else:
            os.environ["CREWDAY_DATABASE_URL"] = original
        get_settings.cache_clear()


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
    """
    with engine.connect() as raw_connection:
        outer = raw_connection.begin()
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
            if outer.is_active:
                outer.rollback()
