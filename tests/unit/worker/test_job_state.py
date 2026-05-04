"""Unit tests for :mod:`app.worker.job_state` (cd-8euz).

Drives the failure-state writers against an in-memory SQLite engine
patched into the process-wide UoW seam. The same pattern the
integration scheduler suite uses (``_default_engine`` /
``_default_sessionmaker_``) — but driven from a unit test because
the failure-state writers are pure SQL + audit-row writes, with no
APScheduler machinery in the loop.

Covers the three transitions the spec contract pins:

* ``record_success`` clears ``consecutive_failures`` and ``dead_at``,
  even when the row was previously dead.
* ``record_failure`` increments the counter, writes the
  ``worker.job.repeated_failure`` audit at the third failure, and
  flips ``dead_at`` + writes ``worker.job.killed`` at the fifth.
* ``reset_job`` clears the flags and writes ``worker.job.reset``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.adapters.db.session as _session_mod
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.ops.models import WorkerHeartbeat
from app.tenancy import tenant_agnostic
from app.util.clock import FrozenClock
from app.worker import job_state
from app.worker.job_state import (
    DEAD_THRESHOLD,
    REPEATED_FAILURE_THRESHOLD,
    is_dead,
    record_failure,
    record_success,
    reset_job,
)

_JOB_ID = "cd_8euz_unit"


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with every ORM table created.

    Same StaticPool shape ``test_heartbeat.py`` uses; the second
    connection inside :func:`make_uow` reuses the same in-memory DB
    only if the pool keeps the underlying connection live.
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
def patched_default_uow(engine: Engine) -> Iterator[Session]:
    """Redirect :func:`make_uow` to ``engine`` and yield a read session.

    The failure-state writers each call :func:`make_uow` directly, so
    we have to patch the module-level defaults the same way
    :class:`tests.integration.test_worker_scheduler.real_make_uow` does.
    The yielded session is a sibling factory bound to the same engine
    so the test can ``select`` rows the writers committed.
    """
    factory = sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=_session_mod.FilteredSession,
    )
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = factory
    read_session = factory()
    try:
        yield read_session
    finally:
        read_session.close()
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


def _read_row(session: Session) -> WorkerHeartbeat | None:
    # ``worker_heartbeat`` is deployment-scoped (no workspace_id), but
    # the orm-filter installs a no-op pass for unscoped tables. Wrap
    # in ``tenant_agnostic`` for parity with the audit-log read below
    # so a future migration that scopes the table doesn't break this
    # helper silently.
    with tenant_agnostic():
        return session.scalars(
            select(WorkerHeartbeat).where(WorkerHeartbeat.worker_name == _JOB_ID)
        ).one_or_none()


def _read_audit_actions(session: Session) -> list[str]:
    # ``audit_log`` is tenant-scoped; without a :class:`WorkspaceContext`
    # the ORM filter raises :class:`TenantFilterMissing`. The deployment-
    # scope rows under test here carry ``workspace_id=NULL``, so the
    # explicit ``tenant_agnostic`` is the documented escape hatch (§02).
    with tenant_agnostic():
        return list(
            session.scalars(
                select(AuditLog.action)
                .where(AuditLog.entity_id == _JOB_ID)
                .order_by(AuditLog.created_at, AuditLog.id)
            ).all()
        )


# ---------------------------------------------------------------------------
# record_success
# ---------------------------------------------------------------------------


class TestRecordSuccess:
    def test_first_call_inserts_row_with_zero_failures(
        self, patched_default_uow: Session
    ) -> None:
        """No row → INSERT with ``consecutive_failures=0`` and live state."""
        clock = FrozenClock(datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
        record_success(job_id=_JOB_ID, clock=clock)

        row = _read_row(patched_default_uow)
        assert row is not None
        assert row.consecutive_failures == 0
        assert row.dead_at is None
        assert row.heartbeat_at.replace(tzinfo=UTC) == clock.now()

    def test_clears_dead_state(self, patched_default_uow: Session) -> None:
        """A successful tick on a dead row clears ``dead_at`` automatically.

        Recovery without operator intervention is the documented
        contract — a job whose upstream came back leaves the dead
        state on the next clean tick.
        """
        failing_clock = FrozenClock(datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
        # Drive the row past the kill threshold.
        for _ in range(DEAD_THRESHOLD):
            record_failure(job_id=_JOB_ID, clock=failing_clock)
        row = _read_row(patched_default_uow)
        assert row is not None
        assert row.dead_at is not None
        assert row.consecutive_failures == DEAD_THRESHOLD

        success_clock = FrozenClock(datetime(2026, 5, 2, 12, 1, tzinfo=UTC))
        record_success(job_id=_JOB_ID, clock=success_clock)

        patched_default_uow.expire_all()
        row = _read_row(patched_default_uow)
        assert row is not None
        assert row.consecutive_failures == 0
        assert row.dead_at is None

    def test_back_to_back_success_upserts_in_one_tx_are_idempotent(
        self, patched_default_uow: Session
    ) -> None:
        """Two uncommitted success writes for one job update one row."""
        first = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
        second = first + timedelta(seconds=1)

        job_state._record_success_in_session(
            patched_default_uow,
            job_id=_JOB_ID,
            now=first,
        )
        patched_default_uow.flush()
        job_state._record_success_in_session(
            patched_default_uow,
            job_id=_JOB_ID,
            now=second,
        )
        patched_default_uow.commit()

        rows = patched_default_uow.scalars(select(WorkerHeartbeat)).all()
        assert len(rows) == 1
        row = rows[0]
        row_at = row.heartbeat_at
        if row_at.tzinfo is None:
            row_at = row_at.replace(tzinfo=UTC)
        assert row_at == second
        assert row.consecutive_failures == 0
        assert row.dead_at is None


# ---------------------------------------------------------------------------
# record_failure
# ---------------------------------------------------------------------------


class TestRecordFailure:
    def test_first_failure_inserts_row_with_count_1(
        self, patched_default_uow: Session
    ) -> None:
        """No row → INSERT with ``consecutive_failures=1`` and no audit."""
        clock = FrozenClock(datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
        outcome = record_failure(job_id=_JOB_ID, clock=clock)

        assert outcome.consecutive_failures == 1
        assert outcome.repeated_failure_audit_emitted is False
        assert outcome.killed is False

        row = _read_row(patched_default_uow)
        assert row is not None
        assert row.consecutive_failures == 1
        assert row.dead_at is None
        assert _read_audit_actions(patched_default_uow) == []

    def test_back_to_back_failure_upserts_in_one_tx_increment_atomically(
        self, patched_default_uow: Session
    ) -> None:
        """Two uncommitted failure writes for one job do not collide."""
        now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
        clock = FrozenClock(now)

        first = job_state._record_failure_in_session(
            patched_default_uow,
            job_id=_JOB_ID,
            clock=clock,
            now=now,
        )
        patched_default_uow.flush()
        second = job_state._record_failure_in_session(
            patched_default_uow,
            job_id=_JOB_ID,
            clock=clock,
            now=now + timedelta(seconds=1),
        )
        patched_default_uow.commit()

        assert [first.consecutive_failures, second.consecutive_failures] == [1, 2]
        assert first.repeated_failure_audit_emitted is False
        assert second.repeated_failure_audit_emitted is False
        assert first.killed is False
        assert second.killed is False

        rows = patched_default_uow.scalars(select(WorkerHeartbeat)).all()
        assert len(rows) == 1
        assert rows[0].consecutive_failures == 2
        assert rows[0].dead_at is None
        assert _read_audit_actions(patched_default_uow) == []

    def test_third_failure_emits_repeated_failure_audit(
        self, patched_default_uow: Session
    ) -> None:
        """Counter==3 → one ``worker.job.repeated_failure`` deployment audit."""
        clock = FrozenClock(datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
        outcomes = [
            record_failure(job_id=_JOB_ID, clock=clock)
            for _ in range(REPEATED_FAILURE_THRESHOLD)
        ]

        assert [o.consecutive_failures for o in outcomes] == [1, 2, 3]
        assert [o.repeated_failure_audit_emitted for o in outcomes] == [
            False,
            False,
            True,
        ]
        assert all(o.killed is False for o in outcomes)

        actions = _read_audit_actions(patched_default_uow)
        assert actions == ["worker.job.repeated_failure"]

        # The audit row carries the deployment scope sentinel.
        with tenant_agnostic():
            audit_row = patched_default_uow.scalars(
                select(AuditLog).where(AuditLog.entity_id == _JOB_ID)
            ).one()
        assert audit_row.workspace_id is None
        assert audit_row.scope_kind == "deployment"
        assert audit_row.via == "worker"
        assert audit_row.actor_kind == "system"
        assert audit_row.entity_kind == "worker_job"
        assert audit_row.diff == {
            "job_id": _JOB_ID,
            "consecutive_failures": REPEATED_FAILURE_THRESHOLD,
            "threshold": REPEATED_FAILURE_THRESHOLD,
        }

    def test_fifth_failure_tips_dead_and_emits_killed_audit(
        self, patched_default_uow: Session
    ) -> None:
        """Counter==5 → ``dead_at`` non-NULL + one ``worker.job.killed`` audit."""
        clock = FrozenClock(datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
        outcomes = [
            record_failure(job_id=_JOB_ID, clock=clock) for _ in range(DEAD_THRESHOLD)
        ]

        assert outcomes[-1].consecutive_failures == DEAD_THRESHOLD
        assert outcomes[-1].killed is True

        row = _read_row(patched_default_uow)
        assert row is not None
        assert row.dead_at is not None
        assert row.dead_at.replace(tzinfo=UTC) == clock.now()
        assert row.consecutive_failures == DEAD_THRESHOLD

        actions = _read_audit_actions(patched_default_uow)
        # One audit at count=3 (repeated_failure) + one at count=5 (killed).
        assert actions == ["worker.job.repeated_failure", "worker.job.killed"]

        with tenant_agnostic():
            killed_row = patched_default_uow.scalars(
                select(AuditLog).where(AuditLog.action == "worker.job.killed")
            ).one()
        assert killed_row.diff == {
            "job_id": _JOB_ID,
            "consecutive_failures": DEAD_THRESHOLD,
            "threshold": DEAD_THRESHOLD,
        }
        assert killed_row.scope_kind == "deployment"
        assert killed_row.via == "worker"

    def test_subsequent_failures_past_dead_do_not_re_emit_killed(
        self, patched_default_uow: Session
    ) -> None:
        """A sixth failure does not write another ``worker.job.killed`` audit.

        Audit-row volume is bounded by the two thresholds; the
        wrapper short-circuits subsequent ticks but if the killswitch
        path is exercised directly the writer must still be one-shot
        per crossing.
        """
        clock = FrozenClock(datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
        for _ in range(DEAD_THRESHOLD + 1):
            record_failure(job_id=_JOB_ID, clock=clock)

        actions = _read_audit_actions(patched_default_uow)
        # repeated_failure at 3; killed at 5; nothing else.
        assert actions == ["worker.job.repeated_failure", "worker.job.killed"]


# ---------------------------------------------------------------------------
# is_dead
# ---------------------------------------------------------------------------


class TestIsDead:
    def test_returns_false_for_unknown_job(self, patched_default_uow: Session) -> None:
        """No row exists → ``is_dead`` is ``False`` (a never-run job is live)."""
        del patched_default_uow  # patched seam only — read isn't needed.
        assert is_dead(job_id=_JOB_ID) is False

    def test_tracks_dead_transition(self, patched_default_uow: Session) -> None:
        """Driving past the kill threshold flips the flag, success clears it."""
        del patched_default_uow
        clock = FrozenClock(datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
        for _ in range(DEAD_THRESHOLD):
            record_failure(job_id=_JOB_ID, clock=clock)
        assert is_dead(job_id=_JOB_ID) is True

        record_success(job_id=_JOB_ID, clock=clock)
        assert is_dead(job_id=_JOB_ID) is False


# ---------------------------------------------------------------------------
# reset_job
# ---------------------------------------------------------------------------


class TestResetJob:
    def test_returns_false_when_no_row(self, patched_default_uow: Session) -> None:
        """A job that has never run cannot be dead → reset is a no-op."""
        clock = FrozenClock(datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
        assert reset_job(job_id=_JOB_ID, clock=clock) is False
        assert _read_row(patched_default_uow) is None
        assert _read_audit_actions(patched_default_uow) == []

    def test_clears_flags_and_writes_audit(self, patched_default_uow: Session) -> None:
        """A real reset clears both columns and emits ``worker.job.reset``."""
        failing_clock = FrozenClock(datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
        for _ in range(DEAD_THRESHOLD):
            record_failure(job_id=_JOB_ID, clock=failing_clock)
        row = _read_row(patched_default_uow)
        assert row is not None and row.dead_at is not None

        reset_clock = FrozenClock(datetime(2026, 5, 2, 12, 30, tzinfo=UTC))
        assert reset_job(job_id=_JOB_ID, clock=reset_clock) is True

        patched_default_uow.expire_all()
        row = _read_row(patched_default_uow)
        assert row is not None
        assert row.consecutive_failures == 0
        assert row.dead_at is None

        actions = _read_audit_actions(patched_default_uow)
        assert actions[-1] == "worker.job.reset"

        with tenant_agnostic():
            reset_audit = patched_default_uow.scalars(
                select(AuditLog).where(AuditLog.action == "worker.job.reset")
            ).one()
        assert reset_audit.diff == {
            "job_id": _JOB_ID,
            "previous_consecutive_failures": DEAD_THRESHOLD,
            "previous_dead": True,
        }
        assert reset_audit.scope_kind == "deployment"
        assert reset_audit.via == "worker"

    def test_no_audit_when_state_already_clean(
        self, patched_default_uow: Session
    ) -> None:
        """A reset on a clean row returns True but writes no audit row.

        Operators run reset to clear a state the row no longer holds;
        the audit feed should not surface a no-op intervention.
        """
        clock = FrozenClock(datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
        record_success(job_id=_JOB_ID, clock=clock)

        assert reset_job(job_id=_JOB_ID, clock=clock) is True
        assert _read_audit_actions(patched_default_uow) == []


# ---------------------------------------------------------------------------
# Module thresholds (regression guards)
# ---------------------------------------------------------------------------


class TestThresholds:
    def test_thresholds_match_spec(self) -> None:
        """The two thresholds match the cd-8euz spec contract."""
        assert job_state.REPEATED_FAILURE_THRESHOLD == 3
        assert job_state.DEAD_THRESHOLD == 5
