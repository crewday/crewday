"""Unit tests for :func:`app.worker.heartbeat.upsert_heartbeat`.

The upsert contract is narrow and self-contained: one row per
``worker_name``, ``heartbeat_at`` advanced on every call. These
tests drive the body against an in-memory SQLite so the upsert's
SELECT-then-INSERT / UPDATE branches are exercised end-to-end
without any APScheduler machinery in the loop.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.base import Base
from app.adapters.db.ops.models import WorkerHeartbeat
from app.worker.heartbeat import upsert_heartbeat


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with every ORM table created.

    StaticPool keeps the same underlying SQLite DB across checkouts
    (without it every connection opens a fresh in-memory DB and the
    fixture's ``create_all`` is invisible to the session the test
    opens).
    """
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
def session(engine: Engine) -> Iterator[Session]:
    """Fresh :class:`Session` bound to :func:`engine`.

    Closed at teardown; the in-memory DB is disposed by the engine
    fixture. No SAVEPOINT dance — each test gets a clean slate.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    s = factory()
    try:
        yield s
    finally:
        s.close()


class TestUpsertHeartbeat:
    """Cover both branches of the SELECT-then-INSERT/UPDATE path."""

    def test_first_call_inserts_row(self, session: Session) -> None:
        """No existing row → INSERT with ``worker_name`` + ``heartbeat_at``."""
        now = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)

        upsert_heartbeat(session, worker_name="scheduler_heartbeat", now=now)
        session.commit()

        row = session.scalars(
            select(WorkerHeartbeat).where(
                WorkerHeartbeat.worker_name == "scheduler_heartbeat"
            )
        ).one()
        assert row.worker_name == "scheduler_heartbeat"
        # SQLite ``DateTime(timezone=True)`` can strip tzinfo on read
        # depending on driver config. Compare via UTC attachment so
        # the assertion is portable between sqlite and postgres.
        row_at = row.heartbeat_at
        if row_at.tzinfo is None:
            row_at = row_at.replace(tzinfo=UTC)
        assert row_at == now
        # ULID-shaped id (26-char Crockford base32).
        assert isinstance(row.id, str)
        assert len(row.id) == 26

    def test_second_call_updates_existing(self, session: Session) -> None:
        """Existing row → UPDATE ``heartbeat_at`` in place, no new row."""
        first = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
        second = first + timedelta(seconds=45)

        upsert_heartbeat(session, worker_name="scheduler_heartbeat", now=first)
        session.commit()
        first_id = session.scalars(
            select(WorkerHeartbeat.id).where(
                WorkerHeartbeat.worker_name == "scheduler_heartbeat"
            )
        ).one()

        upsert_heartbeat(session, worker_name="scheduler_heartbeat", now=second)
        session.commit()

        rows = session.scalars(
            select(WorkerHeartbeat).where(
                WorkerHeartbeat.worker_name == "scheduler_heartbeat"
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].id == first_id
        row_at = rows[0].heartbeat_at
        if row_at.tzinfo is None:
            row_at = row_at.replace(tzinfo=UTC)
        assert row_at == second

    def test_distinct_worker_names_produce_distinct_rows(
        self, session: Session
    ) -> None:
        """Two names → two rows; one name's update never touches the other."""
        now = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)

        upsert_heartbeat(session, worker_name="scheduler_heartbeat", now=now)
        upsert_heartbeat(
            session,
            worker_name="generate_task_occurrences",
            now=now + timedelta(seconds=1),
        )
        session.commit()

        rows = {
            row.worker_name: row
            for row in session.scalars(select(WorkerHeartbeat)).all()
        }
        assert set(rows) == {"scheduler_heartbeat", "generate_task_occurrences"}
        assert rows["scheduler_heartbeat"].id != rows["generate_task_occurrences"].id

    def test_does_not_commit(self, session: Session) -> None:
        """Caller's UoW owns the transaction; upsert stays uncommitted."""
        now = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)

        upsert_heartbeat(session, worker_name="scheduler_heartbeat", now=now)
        # Do NOT call commit; rollback and confirm no row survives.
        session.rollback()

        assert (
            session.scalars(
                select(WorkerHeartbeat).where(
                    WorkerHeartbeat.worker_name == "scheduler_heartbeat"
                )
            ).one_or_none()
            is None
        )
