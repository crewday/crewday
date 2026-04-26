"""Integration tests for the 15-min ``approval_ttl_sweep`` scheduler job.

End-to-end proof that the APScheduler-registered TTL sweep (cd-9ghv)
wires :func:`~app.worker.tasks.approval_ttl.sweep_expired_approvals`
through :func:`~app.worker.scheduler.wrap_job` against the real UoW
seam — flipping every ``approval_request`` row past its ``expires_at``
from ``status='pending'`` to ``status='timed_out'``, advancing the
``worker_heartbeat`` keyed by ``worker_name='approval_ttl_sweep'``,
and emitting one ``event=approval.ttl.sweep`` INFO record per tick.

Domain coverage for the in-process state machine (``expire_due``'s
TTL semantics, the legacy NULL-``expires_at`` carve-out, the cross-
tenant read under ``tenant_agnostic``) lives in
``tests/domain/agent/test_approval.py``. This suite covers what that
layer can't:

* The job id, trigger cadence, and wrapper hooks registered by
  :func:`~app.worker.scheduler.register_jobs`.
* The sweep body's full round-trip through ``wrap_job``: the body
  runs, expired rows flip, the heartbeat upserts, the INFO record
  carries the per-tick ``expired_count``.
* Idempotency under back-to-back ticks — a second run over the same
  data is a no-op because the predicate already excludes terminal
  rows.

Mirrors :mod:`tests.integration.worker.test_idempotency_sweep` for
shape consistency: same fixtures, same assertion vocabulary, same
``allow_propagated_log_capture`` ritual so the operator-facing INFO
record is observable. See ``docs/specs/11-llm-and-agents.md``
§"Approval pipeline" §"TTL", ``docs/specs/16-deployment-operations.md``
§"Worker process", and ``docs/specs/17-testing-quality.md``
§"Integration".
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.llm.models import ApprovalRequest
from app.adapters.db.ops.models import WorkerHeartbeat
from app.adapters.db.workspace.models import Workspace
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.scheduler import (
    APPROVAL_TTL_INTERVAL_SECONDS,
    APPROVAL_TTL_JOB_ID,
    create_scheduler,
    register_jobs,
    wrap_job,
)
from app.worker.scheduler import (
    _make_approval_ttl_body as make_approval_ttl_body,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_make_uow(engine: Engine) -> Iterator[None]:
    """Redirect the process-wide default UoW to the integration engine.

    The sweep body calls
    :func:`~app.worker.tasks.approval_ttl.sweep_expired_approvals`
    which opens its own UoW via :func:`app.adapters.db.session.make_uow`
    (the worker has no ambient session). Mirrors the sibling
    ``test_idempotency_sweep::real_make_uow`` so the two integration
    suites drive the real scheduler seam identically.
    """
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


@pytest.fixture
def clean_approval_tables(engine: Engine) -> Iterator[None]:
    """Empty ``approval_request`` + ``worker_heartbeat`` before/after each test.

    Mirrors ``test_idempotency_sweep::clean_sweep_tables``: the
    harness engine is session-scoped so cross-test bleed would
    otherwise mask regressions (a stale heartbeat row from an earlier
    test would trivially satisfy "row exists after the sweep" even if
    the job never ran).

    The ``user`` / ``workspace`` rows seeded by :func:`_seed_pending`
    are NOT cleaned because they carry FK targets the
    ``approval_request`` rows referenced via ``ON DELETE SET NULL``;
    leaving them in place across tests is harmless (each test seeds
    its own pair under fresh ULIDs).
    """
    with engine.begin() as conn:
        conn.execute(delete(ApprovalRequest))
        conn.execute(delete(WorkerHeartbeat))
    yield
    with engine.begin() as conn:
        conn.execute(delete(ApprovalRequest))
        conn.execute(delete(WorkerHeartbeat))


def _seed_workspace_and_user(engine: Engine) -> tuple[str, str]:
    """Insert a minimal :class:`Workspace` + :class:`User` and return ids.

    The :class:`ApprovalRequest` rows seeded in :func:`_seed_pending`
    carry a ``workspace_id`` (``NOT NULL``) and an optional
    ``requester_actor_id`` FK. The TTL sweep doesn't read the user
    or workspace columns directly, but the FKs are still enforced
    on flush — seeding the parents here keeps the helper honest.

    ULIDs and emails are uniquely suffixed per call so two seeds in
    the same test session don't collide on the unique
    ``email_lower`` index.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    workspace_id = new_ulid()
    user_id = new_ulid()
    suffix = workspace_id[-8:].lower()
    with factory() as session:
        session.add(
            User(
                id=user_id,
                email=f"ttl-{suffix}@example.com",
                email_lower=canonicalise_email(f"ttl-{suffix}@example.com"),
                display_name=f"TTL fixture {suffix}",
                created_at=datetime.now(UTC),
            )
        )
        session.add(
            Workspace(
                id=workspace_id,
                slug=f"ttl-ws-{suffix}",
                name=f"TTL workspace {suffix}",
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
    return workspace_id, user_id


def _seed_pending(
    engine: Engine,
    *,
    workspace_id: str,
    requester_actor_id: str,
    expires_at: datetime,
    created_at: datetime,
) -> str:
    """Insert one ``status='pending'`` :class:`ApprovalRequest` and return its id.

    Bypasses the runtime gate writer so the test controls
    ``expires_at`` and ``created_at`` directly — the runtime always
    derives the former from ``clock.now() + APPROVAL_REQUEST_TTL``,
    which would force a brittle ``freeze_time``-style monkeypatch
    here. Mirrors the sibling sweep suite's ``_seed_row``.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    row_id = new_ulid()
    with factory() as session:
        session.add(
            ApprovalRequest(
                id=row_id,
                workspace_id=workspace_id,
                requester_actor_id=requester_actor_id,
                action_json={
                    "tool_name": "tasks.complete",
                    "tool_call_id": f"tcall-{row_id[-8:].lower()}",
                    "tool_input": {"task_id": "tsk_42"},
                    "card_summary": "complete task",
                    "card_risk": "low",
                    "pre_approval_source": "manual",
                    "agent_correlation_id": new_ulid(),
                },
                status="pending",
                decided_by=None,
                decided_at=None,
                rationale_md=None,
                decision_note_md=None,
                result_json=None,
                expires_at=expires_at,
                inline_channel="web_owner_sidebar",
                for_user_id=None,
                resolved_user_mode=None,
                created_at=created_at,
            )
        )
        session.commit()
    return row_id


def _read_row(engine: Engine, row_id: str) -> ApprovalRequest | None:
    """Return the approval row by id, or ``None`` if absent.

    Reads under :func:`tenant_agnostic` because the integration engine
    has the tenant filter installed (via :func:`real_make_uow`) and
    we don't push a :class:`WorkspaceContext` here.
    """
    from app.tenancy import tenant_agnostic

    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session, tenant_agnostic():
        return session.scalars(
            select(ApprovalRequest).where(ApprovalRequest.id == row_id)
        ).first()


def _read_heartbeat(engine: Engine) -> WorkerHeartbeat | None:
    """Return the heartbeat row for the TTL job, or ``None``."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        return session.scalars(
            select(WorkerHeartbeat).where(
                WorkerHeartbeat.worker_name == APPROVAL_TTL_JOB_ID
            )
        ).first()


# ---------------------------------------------------------------------------
# Registration shape
# ---------------------------------------------------------------------------


class TestRegisterJobs:
    """The TTL sweep is registered with the expected id, interval trigger,
    and wrapper hooks."""

    def test_registered_with_interval_trigger_and_grace(self) -> None:
        """Sweep lands under ``APPROVAL_TTL_JOB_ID`` on a 15-min
        :class:`IntervalTrigger` with a one-tick grace window.

        Pinning the trigger shape here rather than only in the unit
        suite keeps the cadence visible at the integration layer —
        the place the operators' runbooks actually cite. Mirrors
        ``test_idempotency_sweep::test_registered_with_daily_cron_trigger``.
        """
        scheduler = create_scheduler()
        register_jobs(scheduler)
        job = scheduler.get_job(APPROVAL_TTL_JOB_ID)
        assert job is not None, "approval_ttl_sweep job not registered"

        # IntervalTrigger (not Cron) — the TTL is "every 15 min" not
        # "at HH:MM UTC". A future cadence change should swap the
        # constant, not the trigger class.
        assert isinstance(job.trigger, IntervalTrigger)
        # ``IntervalTrigger.interval`` carries the seconds value as a
        # :class:`timedelta`.
        assert job.trigger.interval == timedelta(seconds=APPROVAL_TTL_INTERVAL_SECONDS)
        assert APPROVAL_TTL_INTERVAL_SECONDS == 900

        # ``misfire_grace_time = APPROVAL_TTL_INTERVAL_SECONDS`` —
        # one tick late is acceptable; two ticks late should skip and
        # rely on the next tick (the predicate is idempotent).
        assert job.misfire_grace_time is not None
        assert job.misfire_grace_time >= APPROVAL_TTL_INTERVAL_SECONDS

        # ``max_instances=1`` + ``coalesce=True`` — a stuck tick must
        # not stack up. Matches the convention every other registered
        # job (heartbeat, generator fan-out, idempotency sweep) uses.
        assert job.max_instances == 1
        assert job.coalesce is True


# ---------------------------------------------------------------------------
# End-to-end: seeded row → job body → flip + heartbeat + log
# ---------------------------------------------------------------------------


class TestSweepJobEndToEnd:
    """Drive the TTL sweep body through :func:`wrap_job` against the real UoW.

    Running the body via the wrapper (rather than calling
    :func:`sweep_expired_approvals` directly) is what the cd-9ghv
    spec asks for — the heartbeat, log, and exception-swallow seams
    all sit in :func:`wrap_job`, and only a wrapper-driven tick
    proves the whole composition.
    """

    def test_expired_row_flipped_heartbeat_advances_count_logged(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_approval_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """Seed an expired ``pending`` row; one wrapped tick:

        1. Flips ``status='timed_out'``,
           ``decision_note_md='auto-expired'``, stamps ``decided_at``.
        2. Writes a :class:`WorkerHeartbeat` keyed by
           :data:`APPROVAL_TTL_JOB_ID`.
        3. Emits one ``event=approval.ttl.sweep`` INFO record with
           ``expired_count >= 1`` and the row id in ``expired_ids``.

        Alembic's ``fileConfig`` flips ``propagate=False`` on the
        scheduler logger during the session-scoped migration; the
        shared helper restores propagation for the duration of this
        test so ``caplog`` can see the sweep's INFO event.
        """
        allow_propagated_log_capture("app.worker.tasks.approval_ttl")
        allow_propagated_log_capture("app.worker.scheduler")

        # Fixed clock — the sweep body threads this through to
        # :func:`sweep_expired_approvals` (and therefore to
        # :func:`expire_due`), so the cutoff is deterministic.
        # The seeded ``expires_at`` lands one second past the frozen
        # instant so the predicate (`expires_at <= now`) lights up.
        frozen = FrozenClock(datetime(2026, 4, 24, 3, 0, tzinfo=UTC))

        workspace_id, requester_id = _seed_workspace_and_user(engine)
        expired_at = frozen.now() - timedelta(seconds=1)
        # Created well in the past so the audit trail's ordering
        # invariants stay sensible (``created_at < expires_at``,
        # ``expires_at < decided_at``).
        created_at = frozen.now() - timedelta(days=8)
        row_id = _seed_pending(
            engine,
            workspace_id=workspace_id,
            requester_actor_id=requester_id,
            expires_at=expired_at,
            created_at=created_at,
        )

        wrapped = wrap_job(
            make_approval_ttl_body(frozen),
            job_id=APPROVAL_TTL_JOB_ID,
            clock=frozen,
        )

        # Two propagated loggers feed this test: the task module
        # emits ``event=approval.ttl.sweep``, the scheduler wrapper
        # emits ``event=worker.tick.end``. Activate INFO on both so
        # caplog captures the full pair — a regression that drops
        # either side should fail loudly here.
        with (
            caplog.at_level(logging.INFO, logger="app.worker.tasks.approval_ttl"),
            caplog.at_level(logging.INFO, logger="app.worker.scheduler"),
        ):
            asyncio.run(wrapped())

        # 1. Status flipped + decision-note stamped + decided_at
        #    populated. ``decided_by`` MUST stay NULL — the auto-
        #    expiry has no human reviewer; a downstream audit reader
        #    keys off ``decision_note_md='auto-expired'`` to
        #    distinguish the auto path from a NULL-decider data
        #    corruption (see :func:`expire_due`).
        row = _read_row(engine, row_id)
        assert row is not None
        assert row.status == "timed_out"
        assert row.decision_note_md == "auto-expired"
        assert row.decided_by is None
        assert row.decided_at is not None

        # 2. Heartbeat row written under the TTL job's worker_name
        #    with the injected-clock timestamp.
        heartbeat = _read_heartbeat(engine)
        assert heartbeat is not None, "heartbeat row not written"
        # SQLite strips tzinfo off ``DateTime(timezone=True)`` on
        # read; Postgres keeps it. Compare in naive-UTC space to
        # stay portable (see :mod:`app.worker.tasks.generator`'s
        # ``_as_naive_utc`` for the same reasoning).
        expected = frozen.now().astimezone(UTC).replace(tzinfo=None)
        actual = heartbeat.heartbeat_at
        if actual.tzinfo is not None:
            actual = actual.astimezone(UTC).replace(tzinfo=None)
        assert actual == expected

        # 3. Exactly one ``event=approval.ttl.sweep`` INFO record
        #    with the expected counts + ids.
        sweep_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "approval.ttl.sweep"
        ]
        assert len(sweep_events) == 1
        assert sweep_events[0].levelno == logging.INFO
        assert getattr(sweep_events[0], "expired_count", None) == 1
        # ``expired_ids`` is serialised to a list at the log layer
        # (see :func:`sweep_expired_approvals`).
        assert getattr(sweep_events[0], "expired_ids", None) == [row_id]

        # The wrapper's end event must fire with ok=True — proves
        # the body didn't raise and the heartbeat seam ran.
        end_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.tick.end"
            and getattr(rec, "job_id", None) == APPROVAL_TTL_JOB_ID
        ]
        assert len(end_events) == 1
        assert getattr(end_events[0], "ok", None) is True

    def test_fresh_row_is_preserved(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_approval_tables: None,
    ) -> None:
        """Row whose ``expires_at`` is in the future survives the sweep.

        Regression guard: an over-eager cutoff (e.g. wrong sign on
        the comparison, ``>=`` vs. ``<=``) would silently drop live
        approvals and defeat the desk surface contract — every row
        the operator can see in ``/approvals`` must be either
        actionable or terminally decided, never a phantom.
        """
        frozen = FrozenClock(datetime(2026, 4, 24, 3, 0, tzinfo=UTC))
        workspace_id, requester_id = _seed_workspace_and_user(engine)
        # 1 hour in the future — well inside the 7-day default TTL.
        future_at = frozen.now() + timedelta(hours=1)
        row_id = _seed_pending(
            engine,
            workspace_id=workspace_id,
            requester_actor_id=requester_id,
            expires_at=future_at,
            created_at=frozen.now() - timedelta(minutes=5),
        )

        wrapped = wrap_job(
            make_approval_ttl_body(frozen),
            job_id=APPROVAL_TTL_JOB_ID,
            clock=frozen,
        )
        asyncio.run(wrapped())

        # Row preserved in ``pending`` state.
        row = _read_row(engine, row_id)
        assert row is not None
        assert row.status == "pending"
        assert row.decision_note_md is None
        assert row.decided_at is None

        # Heartbeat still advanced — the tick was a successful
        # no-op, not a skipped job.
        assert _read_heartbeat(engine) is not None

    def test_empty_table_still_heartbeats_and_logs_zero(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_approval_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """Empty table → ``expired_count=0``, heartbeat still upserts.

        Edge case operators will hit on day one and after every quiet
        window: the sweep must still prove liveness via the
        heartbeat and emit a log record so the absence of sweep
        activity isn't indistinguishable from the job never firing.
        Mirrors the sibling sweep's empty-table assertion.
        """
        allow_propagated_log_capture("app.worker.tasks.approval_ttl")

        frozen = FrozenClock(datetime(2026, 4, 24, 3, 0, tzinfo=UTC))
        wrapped = wrap_job(
            make_approval_ttl_body(frozen),
            job_id=APPROVAL_TTL_JOB_ID,
            clock=frozen,
        )
        with caplog.at_level(logging.INFO, logger="app.worker.tasks.approval_ttl"):
            asyncio.run(wrapped())

        assert _read_heartbeat(engine) is not None

        sweep_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "approval.ttl.sweep"
        ]
        assert len(sweep_events) == 1
        assert getattr(sweep_events[0], "expired_count", None) == 0
        assert getattr(sweep_events[0], "expired_ids", None) == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestSweepIdempotency:
    """Two ticks over the same data converge: the second is a no-op.

    The TTL predicate is ``status='pending' AND expires_at <= now``;
    a row already flipped to ``timed_out`` falls out of the predicate
    on the next tick. This is the operator-facing guarantee that
    APScheduler's ``coalesce=True`` rests on — a stuck-then-resumed
    scheduler that fires two ticks in quick succession must not
    emit two ``ApprovalDecided`` events per row or write a second
    audit-equivalent log line that overstates fleet activity.
    """

    def test_second_tick_is_no_op_on_already_expired_rows(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_approval_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """Run two ticks back-to-back; second emits ``expired_count=0``.

        First tick flips one row to ``timed_out`` and logs
        ``expired_count=1``; second tick walks the same table, finds
        no candidates (the row's status moved out of ``pending``),
        and logs ``expired_count=0``. The row's ``decided_at``
        timestamp from the first tick stays untouched — a regression
        that re-stamped already-terminal rows would corrupt the
        audit ordering.
        """
        allow_propagated_log_capture("app.worker.tasks.approval_ttl")

        frozen = FrozenClock(datetime(2026, 4, 24, 3, 0, tzinfo=UTC))
        workspace_id, requester_id = _seed_workspace_and_user(engine)
        row_id = _seed_pending(
            engine,
            workspace_id=workspace_id,
            requester_actor_id=requester_id,
            expires_at=frozen.now() - timedelta(seconds=1),
            created_at=frozen.now() - timedelta(days=8),
        )

        wrapped = wrap_job(
            make_approval_ttl_body(frozen),
            job_id=APPROVAL_TTL_JOB_ID,
            clock=frozen,
        )
        with caplog.at_level(logging.INFO, logger="app.worker.tasks.approval_ttl"):
            # First tick — does the work.
            asyncio.run(wrapped())
            # Snapshot the post-first-tick decided_at so we can
            # prove the second tick doesn't re-stamp it.
            row_after_first = _read_row(engine, row_id)
            assert row_after_first is not None
            decided_at_first = row_after_first.decided_at

            # Second tick — must be a no-op.
            asyncio.run(wrapped())

        sweep_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "approval.ttl.sweep"
        ]
        assert len(sweep_events) == 2
        assert getattr(sweep_events[0], "expired_count", None) == 1
        assert getattr(sweep_events[0], "expired_ids", None) == [row_id]
        assert getattr(sweep_events[1], "expired_count", None) == 0
        assert getattr(sweep_events[1], "expired_ids", None) == []

        # The row's decided_at MUST NOT have been re-stamped by the
        # second tick. Equality (rather than just non-None) catches
        # a regression that re-runs the UPDATE on terminal rows —
        # which would also re-publish the ``ApprovalDecided`` event,
        # double-counting expirations on the operator dashboard.
        row_after_second = _read_row(engine, row_id)
        assert row_after_second is not None
        assert row_after_second.decided_at == decided_at_first
