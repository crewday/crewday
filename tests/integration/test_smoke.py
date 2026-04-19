"""Integration smoke test.

Proves the session-scoped ``engine`` + ``migrate_once`` + function-
scoped ``db_session`` wiring works end-to-end against SQLite. Real
integration tests (repositories, UoW behaviour under concurrent
writers, RLS assertions on PG) land on top of this harness in
follow-up tasks (see ``docs/specs/17-testing-quality.md``
§"Integration").
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration


def test_db_session_executes_trivial_select(db_session: Session) -> None:
    row = db_session.execute(text("select 1 as one")).one()
    assert row.one == 1


def test_db_session_rolls_back_per_test(db_session: Session) -> None:
    # Two tests share the same engine + schema (empty at this stage),
    # so we just assert the session is clean and usable. ``begin()``
    # is a no-op on a session that already has an outer transaction,
    # but ``in_transaction()`` only flips to True once the session
    # has emitted its first SQL — issue a trivial read to cross that
    # threshold.
    db_session.execute(text("select 1"))
    assert db_session.in_transaction() is True


def test_db_session_setup_under_100ms_after_warmup(engine: Engine) -> None:
    """Acceptance-criterion budget: once the session-scoped ``engine``
    and ``migrate_once`` have primed the DB, each new per-test session
    (connection + outer transaction + sessionmaker + trivial SELECT)
    must resolve in well under 100 ms. We measure the same steps
    ``db_session`` performs so a regression in any of them trips the
    budget. The threshold is deliberately generous (400 ms) to stay
    green on slow CI runners; the real goal is catching an order-of-
    magnitude regression (seconds, not milliseconds).
    """

    def _open_and_close_session() -> Iterator[Session]:
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
                session.execute(text("select 1"))
                yield session
            finally:
                session.close()
                if outer.is_active:
                    outer.rollback()

    start = time.perf_counter()
    for _ in _open_and_close_session():
        pass
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 400, (
        f"per-test DB fixture took {elapsed_ms:.1f} ms (budget: <100 ms "
        f"after warm-up, guard threshold 400 ms to tolerate slow CI)"
    )
