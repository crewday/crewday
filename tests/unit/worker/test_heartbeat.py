"""Unit tests for :func:`app.worker.heartbeat.upsert_heartbeat`.

The upsert contract is narrow and self-contained: one row per
``worker_name``, ``heartbeat_at`` advanced on every call. These
tests drive the body against an in-memory SQLite so the upsert's
single-statement ``INSERT ... ON CONFLICT DO UPDATE`` is
exercised end-to-end without any APScheduler machinery in the
loop. The Postgres branch is covered by compiling the statement
against the ``postgresql`` dialect and asserting the emitted SQL
shape — a real Postgres engine is overkill for this unit test
scope; the integration suite (``tests/integration/test_worker_
scheduler.py``) re-exercises the full path on whatever backend
``CREWDAY_TEST_DB`` selects.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, Insert, create_engine, select
from sqlalchemy.dialects import postgresql
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
    """Cover both branches of the ``INSERT ... ON CONFLICT DO UPDATE`` statement."""

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

    def test_back_to_back_upserts_in_one_tx_are_idempotent(
        self, session: Session
    ) -> None:
        """Two upserts inside one uncommitted transaction must be idempotent.

        The Recipe D / misconfigured-double-worker race from cd-76qx
        is two processes that both see no row and both INSERT before
        either commits. A pure unit test cannot model concurrent
        connections (SQLite serialises writes at the connection level
        and StaticPool shares one connection across this fixture), so
        the cross-process race itself is exercised in the integration
        suite. What we *can* pin here is the SQL-layer invariant the
        fix relies on: ``INSERT ... ON CONFLICT(worker_name) DO UPDATE``
        is idempotent for back-to-back calls in one transaction — no
        IntegrityError on the second flush, and the row reflects the
        second writer's ``heartbeat_at``. A future "optimisation" that
        re-introduces SELECT-then-INSERT loses that idempotence on
        Postgres and would re-open the race.
        """
        first = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
        second = first + timedelta(seconds=1)

        upsert_heartbeat(session, worker_name="scheduler_heartbeat", now=first)
        # Flush forces the INSERT to the DB without committing. The
        # second call must coexist with the first uncommitted write.
        session.flush()
        upsert_heartbeat(session, worker_name="scheduler_heartbeat", now=second)
        session.commit()

        rows = session.scalars(
            select(WorkerHeartbeat).where(
                WorkerHeartbeat.worker_name == "scheduler_heartbeat"
            )
        ).all()
        assert len(rows) == 1
        row_at = rows[0].heartbeat_at
        if row_at.tzinfo is None:
            row_at = row_at.replace(tzinfo=UTC)
        assert row_at == second

    def test_upsert_does_not_clobber_failure_state_columns(
        self, session: Session
    ) -> None:
        """``consecutive_failures`` and ``dead_at`` survive an upsert.

        cd-8euz added per-job failure-tracking columns owned by
        :mod:`app.worker.job_state`. A successful liveness tick
        through :func:`upsert_heartbeat` must leave those columns
        untouched — only :func:`record_success` is allowed to clear
        them. Regression test for the cd-76qx ``ON CONFLICT DO
        UPDATE`` set: must touch only ``heartbeat_at``.
        """
        first = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
        later = first + timedelta(seconds=45)

        upsert_heartbeat(session, worker_name="scheduler_heartbeat", now=first)
        session.commit()

        # Simulate a job-state advance that owns these columns.
        existing = session.scalars(
            select(WorkerHeartbeat).where(
                WorkerHeartbeat.worker_name == "scheduler_heartbeat"
            )
        ).one()
        existing.consecutive_failures = 4
        existing.dead_at = first
        session.commit()

        # A subsequent liveness tick must NOT reset failure state.
        upsert_heartbeat(session, worker_name="scheduler_heartbeat", now=later)
        session.commit()

        # ``upsert_heartbeat`` writes through Core (a dialect-native
        # ``INSERT ... ON CONFLICT DO UPDATE``); the ORM identity map
        # for ``existing`` still holds the pre-update ``heartbeat_at``.
        # Expire the cached state so the read-back reflects the row's
        # post-upsert columns.
        session.expire_all()
        row = session.scalars(
            select(WorkerHeartbeat).where(
                WorkerHeartbeat.worker_name == "scheduler_heartbeat"
            )
        ).one()
        row_at = row.heartbeat_at
        if row_at.tzinfo is None:
            row_at = row_at.replace(tzinfo=UTC)
        assert row_at == later
        assert row.consecutive_failures == 4
        dead_at = row.dead_at
        assert dead_at is not None
        if dead_at.tzinfo is None:
            dead_at = dead_at.replace(tzinfo=UTC)
        assert dead_at == first


class _FakePgBind:
    """Stand-in for a SQLAlchemy bind reporting the postgresql dialect.

    Only ``get_bind().dialect.name`` is read by ``upsert_heartbeat``;
    everything else can stay missing. The ``dialect`` attribute is
    constructed at instance time (rather than as a class attribute)
    so the ``postgresql.dialect()`` call site stays inside a typed
    function body.
    """

    def __init__(self) -> None:
        # SA's ``Dialect`` class is unstubbed; ``Any`` keeps the
        # untyped-call out of the strict-mypy diagnostic surface.
        self.dialect: Any = postgresql.dialect()  # type: ignore[no-untyped-call]


class _CapturingPgSession:
    """Minimal session stub that drives the Postgres branch of ``upsert_heartbeat``.

    Returns a postgresql-dialected bind from ``get_bind`` so the helper
    selects the ``pg_insert`` path, captures the executed statement so
    the test can compile-check it, and no-ops ``flush``. ``execute``
    must not actually run the statement — there is no real engine.
    """

    def __init__(self) -> None:
        self.captured: Insert | None = None

    def get_bind(self) -> _FakePgBind:
        return _FakePgBind()

    def execute(self, statement: Insert, params: Any = None) -> None:
        self.captured = statement

    def flush(self) -> None:
        return None


class TestUpsertHeartbeatPostgresDialect:
    """Drive the Postgres branch of ``upsert_heartbeat`` end-to-end.

    No live Postgres engine is needed: a fake session reports a
    postgresql-dialected bind, the helper executes its statement
    against the fake, and the test compiles the captured statement
    on the Postgres dialect. This exercises the actual production
    code path — a regression that swaps ``pg_insert`` for
    ``sqlite_insert`` or drops the conflict clause is caught here.
    The integration suite re-exercises the same path against
    whichever backend ``CREWDAY_TEST_DB`` selects.
    """

    def test_postgres_branch_emits_expected_conflict_clause(self) -> None:
        """The Postgres branch emits ``ON CONFLICT ... DO UPDATE`` SQL.

        Calls :func:`upsert_heartbeat` against a fake session that
        reports the postgresql dialect, captures the executed
        statement, and compiles it on the Postgres dialect so we
        can assert the conflict clause and the narrow ``SET`` list
        (only ``heartbeat_at``). Pins the SQL shape so a regression
        that silently expands the ``SET`` list to include
        ``consecutive_failures`` / ``dead_at`` is caught.
        """
        now = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
        fake = _CapturingPgSession()

        # ``upsert_heartbeat`` is typed against ``sqlalchemy.orm.Session``;
        # the fake duck-types just enough of it for this branch (``get_bind``,
        # ``execute``, ``flush``). Cast away the static type for the call.
        upsert_heartbeat(fake, worker_name="scheduler_heartbeat", now=now)  # type: ignore[arg-type]

        captured = fake.captured
        assert captured is not None
        # SA's ``postgresql.dialect()`` is unstubbed; the surrounding
        # ``compile`` call inherits the diagnostic.
        pg_dialect: Any = postgresql.dialect()  # type: ignore[no-untyped-call]
        compiled = str(
            captured.compile(
                dialect=pg_dialect,
                compile_kwargs={"literal_binds": False},
            )
        )
        # Expected emitted shape:
        # ``ON CONFLICT (worker_name) DO UPDATE SET heartbeat_at =
        #  excluded.heartbeat_at``.
        assert "ON CONFLICT" in compiled
        assert "(worker_name)" in compiled
        assert "DO UPDATE" in compiled
        # Isolate the ``DO UPDATE SET ...`` tail so the assertion checks
        # the conflict-update set without false positives from the
        # leading ``INSERT`` column list (the ORM's column-default
        # machinery includes ``consecutive_failures`` in the INSERT
        # VALUES because of its ``server_default='0'`` — that is fine;
        # the ``SET`` clause is what must stay narrow).
        do_update_tail = compiled.split("DO UPDATE", 1)[1]
        assert "heartbeat_at = excluded.heartbeat_at" in do_update_tail
        # The ``SET`` list must NOT touch the cd-8euz columns — those
        # belong to :mod:`app.worker.job_state` and a liveness tick is
        # forbidden from clobbering them.
        assert "consecutive_failures" not in do_update_tail
        assert "dead_at" not in do_update_tail
