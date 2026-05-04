"""Unit tests for :mod:`app.worker.scheduler`.

Covers the public seam without a running event loop:

* :func:`create_scheduler` returns an :class:`AsyncIOScheduler`
  seeded with the injected clock.
* :func:`register_jobs` wires the expected job ids.
* :func:`start` / :func:`stop` are idempotent.
* :func:`wrap_job` runs the body, logs start/end, swallows
  exceptions, and upserts the heartbeat on success.

The heartbeat-path assertions use a monkey-patched
:func:`app.worker.scheduler._write_heartbeat` so the tests don't
need a DB — the heartbeat module itself has its own unit suite.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.worker import scheduler as scheduler_mod
from app.worker.jobs import identity as identity_jobs
from app.worker.jobs import llm_budget as llm_budget_jobs
from app.worker.jobs import maintenance as maintenance_jobs
from app.worker.jobs import messaging as messaging_jobs
from app.worker.scheduler import (
    AGENT_COMPACTION_JOB_ID,
    APPROVAL_TTL_JOB_ID,
    CHAT_GATEWAY_SWEEP_JOB_ID,
    DAILY_DIGEST_JOB_ID,
    DAILY_DIGEST_MISFIRE_GRACE_SECONDS,
    EXTRACT_DOCUMENT_JOB_ID,
    GENERATOR_JOB_ID,
    HEARTBEAT_JOB_ID,
    IDEMPOTENCY_SWEEP_JOB_ID,
    INVENTORY_REORDER_JOB_ID,
    INVITE_TTL_INTERVAL_SECONDS,
    INVITE_TTL_JOB_ID,
    LLM_BUDGET_REFRESH_INTERVAL_SECONDS,
    LLM_BUDGET_REFRESH_JOB_ID,
    OVERDUE_DETECT_INTERVAL_SECONDS,
    OVERDUE_DETECT_JOB_ID,
    POLL_ICAL_JOB_ID,
    RETENTION_ROTATION_JOB_ID,
    SIGNUP_GC_INTERVAL_SECONDS,
    SIGNUP_GC_JOB_ID,
    USER_WORKSPACE_REFRESH_INTERVAL_SECONDS,
    USER_WORKSPACE_REFRESH_JOB_ID,
    WEB_PUSH_DISPATCH_JOB_ID,
    WEBHOOK_DISPATCH_JOB_ID,
    create_scheduler,
    register_jobs,
    registered_job_ids,
    start,
    stop,
    wrap_job,
)
from app.worker.tasks.daily_digest import DailyDigestReport

# ---------------------------------------------------------------------------
# create_scheduler / register_jobs
# ---------------------------------------------------------------------------


class TestCreateScheduler:
    def test_returns_asyncio_scheduler_not_started(self) -> None:
        """Fresh scheduler is an AsyncIO one and is NOT running."""
        sched = create_scheduler()
        assert isinstance(sched, AsyncIOScheduler)
        assert sched.running is False

    def test_clock_stashed_for_wrap_job(self) -> None:
        """The injected clock is reachable via the private attribute.

        Kept as a white-box assertion because ``wrap_job`` depends
        on it — a refactor that moves the storage seam needs to
        update both sides in lockstep.
        """
        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        sched = create_scheduler(clock=clock)
        assert sched._crewday_clock is clock


class TestRegisterJobs:
    def test_registers_expected_ids(self) -> None:
        """Standard job set includes the core heartbeat and worker fan-outs."""
        sched = create_scheduler()
        register_jobs(sched)
        # Compare as a set so inserting a future job doesn't break
        # this assertion just by landing a new alphabetical neighbour.
        # The ``registered_job_ids`` helper already returns a sorted
        # tuple, so duplicates would still surface via the companion
        # ``test_is_idempotent_under_replace_existing`` path.
        assert set(registered_job_ids(sched)) == {
            APPROVAL_TTL_JOB_ID,
            AGENT_COMPACTION_JOB_ID,
            CHAT_GATEWAY_SWEEP_JOB_ID,
            DAILY_DIGEST_JOB_ID,
            EXTRACT_DOCUMENT_JOB_ID,
            GENERATOR_JOB_ID,
            IDEMPOTENCY_SWEEP_JOB_ID,
            INVENTORY_REORDER_JOB_ID,
            INVITE_TTL_JOB_ID,
            HEARTBEAT_JOB_ID,
            LLM_BUDGET_REFRESH_JOB_ID,
            OVERDUE_DETECT_JOB_ID,
            POLL_ICAL_JOB_ID,
            RETENTION_ROTATION_JOB_ID,
            SIGNUP_GC_JOB_ID,
            USER_WORKSPACE_REFRESH_JOB_ID,
            WEB_PUSH_DISPATCH_JOB_ID,
            WEBHOOK_DISPATCH_JOB_ID,
        }

    def test_is_idempotent_under_replace_existing(self) -> None:
        """Re-registering on the same scheduler does not raise.

        Covers the not-yet-started path: on a STOPPED scheduler,
        APScheduler's ``replace_existing=True`` does NOT dedupe — it
        appends to ``_pending_jobs`` unchecked, and the duplicate only
        trips ``ConflictingIdError`` (suppressed by ``replace_existing``)
        at :meth:`start` time. :func:`register_jobs` therefore calls
        :meth:`remove_job` first; this test proves the call works by
        asserting both ``registered_job_ids`` (which scans ``get_jobs``
        and would surface the duplicate as ``('generator', 'generator',
        'heartbeat', 'heartbeat')``) and the raw ``get_jobs`` length
        after two rounds.
        """
        sched = create_scheduler()
        register_jobs(sched)
        register_jobs(sched)  # must not raise
        # Job count unchanged — ``replace_existing=True`` is not
        # enough on its own; the explicit ``remove_job`` keeps the
        # pending list to exactly one entry per id. Set equality here
        # mirrors ``test_registers_expected_ids``; the raw
        # ``len(sched.get_jobs())`` below is what actually catches a
        # duplicate (a stale entry would make the list longer than
        # the set).
        ids = registered_job_ids(sched)
        expected_ids = {
            APPROVAL_TTL_JOB_ID,
            AGENT_COMPACTION_JOB_ID,
            CHAT_GATEWAY_SWEEP_JOB_ID,
            DAILY_DIGEST_JOB_ID,
            EXTRACT_DOCUMENT_JOB_ID,
            GENERATOR_JOB_ID,
            IDEMPOTENCY_SWEEP_JOB_ID,
            INVENTORY_REORDER_JOB_ID,
            INVITE_TTL_JOB_ID,
            HEARTBEAT_JOB_ID,
            LLM_BUDGET_REFRESH_JOB_ID,
            OVERDUE_DETECT_JOB_ID,
            POLL_ICAL_JOB_ID,
            RETENTION_ROTATION_JOB_ID,
            SIGNUP_GC_JOB_ID,
            USER_WORKSPACE_REFRESH_JOB_ID,
            WEB_PUSH_DISPATCH_JOB_ID,
            WEBHOOK_DISPATCH_JOB_ID,
        }
        assert set(ids) == expected_ids
        assert len(ids) == len(expected_ids)
        assert len(sched.get_jobs()) == len(expected_ids)


# ---------------------------------------------------------------------------
# Overdue sweeper job (cd-hurw)
# ---------------------------------------------------------------------------


class TestOverdueDetectJob:
    """Registration shape for the 5-minute overdue-sweeper job.

    The body's per-workspace fan-out (skip demo-expired tenants,
    isolate broken workspaces, sum flipped counts) is covered
    end-to-end in ``tests/integration/test_tasks_overdue_tick.py``
    against a real engine — the unit layer pins the registration
    metadata so a future refactor cannot silently change the
    operator-visible cadence.
    """

    def test_adds_overdue_detect_job_at_5min_interval(self) -> None:
        """Job is registered with the pinned interval + coalesce knobs."""
        from apscheduler.triggers.interval import IntervalTrigger

        sched = create_scheduler()
        register_jobs(sched)

        job = sched.get_job(OVERDUE_DETECT_JOB_ID)
        assert job is not None, (
            f"{OVERDUE_DETECT_JOB_ID} not registered by register_jobs"
        )

        # IntervalTrigger at 300 s (5 min).
        assert isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval.total_seconds() == 300.0
        assert OVERDUE_DETECT_INTERVAL_SECONDS == 300

        # Wrapper knobs: misfire grace = interval (one-tick-late is
        # idempotent; two-ticks-late is a stuck-scheduler signal),
        # single instance, coalesce on.
        assert job.misfire_grace_time == OVERDUE_DETECT_INTERVAL_SECONDS
        assert job.coalesce is True
        assert job.max_instances == 1


class TestChatGatewaySweepJob:
    """Registration shape for the 30 s chat-gateway dispatch safety net (cd-0gaa).

    The body's per-row fan-out (re-publishes ``chat.message.received``
    on the bus, audits per row, isolates per-row failures) is covered
    end-to-end in ``tests/unit/chat_gateway/test_sweep.py`` and
    ``tests/integration/chat_gateway/test_sweep_e2e.py`` — the unit
    layer here just pins the registration metadata so a future
    refactor cannot silently change the operator-visible cadence.
    """

    def test_adds_chat_gateway_sweep_job_at_30s_interval(self) -> None:
        from apscheduler.triggers.interval import IntervalTrigger

        from app.worker.scheduler import (
            CHAT_GATEWAY_SWEEP_INTERVAL_SECONDS,
        )

        sched = create_scheduler()
        register_jobs(sched)

        job = sched.get_job(CHAT_GATEWAY_SWEEP_JOB_ID)
        assert job is not None, (
            f"{CHAT_GATEWAY_SWEEP_JOB_ID} not registered by register_jobs"
        )

        assert isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval.total_seconds() == 30.0
        assert CHAT_GATEWAY_SWEEP_INTERVAL_SECONDS == 30

        # ``misfire_grace_time = CHAT_GATEWAY_SWEEP_INTERVAL_SECONDS``
        # — one-tick-late is idempotent (rows still carrying
        # ``dispatched_to_agent_at IS NULL`` are simply re-fired), and
        # two-ticks-late is the stuck-scheduler signal.
        assert job.misfire_grace_time == CHAT_GATEWAY_SWEEP_INTERVAL_SECONDS
        assert job.coalesce is True
        assert job.max_instances == 1


class TestDailyDigestJob:
    """Registration shape for the hourly daily-digest fan-out."""

    def test_adds_daily_digest_job_hourly(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        sched = create_scheduler()
        register_jobs(sched)

        job = sched.get_job(DAILY_DIGEST_JOB_ID)
        assert job is not None, f"{DAILY_DIGEST_JOB_ID} not registered"

        assert isinstance(job.trigger, CronTrigger)
        assert str(job.trigger.fields[6]) == "0"
        assert job.misfire_grace_time == DAILY_DIGEST_MISFIRE_GRACE_SECONDS
        assert job.coalesce is True
        assert job.max_instances == 1

    def test_fanout_body_delegates_recipient_local_7am(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FrozenClock(datetime(2026, 4, 29, 12, 0, tzinfo=UTC))
        calls: list[tuple[str, int | None]] = []

        class _FakeSettings:
            smtp_host: str | None = None
            smtp_port = 587
            smtp_from: str | None = None
            smtp_user: str | None = None
            smtp_password: object = None
            smtp_use_tls = True
            smtp_timeout = 10
            smtp_bounce_domain: str | None = None
            openrouter_api_key: object = None

        class _FakeMailer:
            def __init__(
                self,
                *,
                config_source: object,
            ) -> None:
                self.config_source = config_source

        class _FakeSmtpConfig:
            host = "smtp.db.example"
            from_addr = "crew@example.test"

        class _FakeSmtpSource:
            def __init__(self, **_: object) -> None:
                pass

            def config(self) -> _FakeSmtpConfig:
                return _FakeSmtpConfig()

        class _FakeRow:
            def __init__(self, id: str, slug: str) -> None:
                self.id = id
                self.slug = slug

        class _FakeResult:
            def all(self) -> list[_FakeRow]:
                return [_FakeRow("01HWA00000000000000000WSP1", "ws-one")]

        class _FakeScalarResult:
            def all(self) -> list[str]:
                return []

        class _FakeNestedTx:
            def __enter__(self) -> _FakeNestedTx:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        class _FakeSession:
            def execute(self, _stmt: object) -> _FakeResult:
                return _FakeResult()

            def scalars(self, _stmt: object) -> _FakeScalarResult:
                return _FakeScalarResult()

            def begin_nested(self) -> _FakeNestedTx:
                return _FakeNestedTx()

        class _FakeUow:
            def __enter__(self) -> _FakeSession:
                return _FakeSession()

            def __exit__(self, *exc: object) -> None:
                return None

        def fake_send_daily_digest(
            ctx: WorkspaceContext,
            *,
            session: object,
            mailer: object,
            llm: object,
            clock: object,
            due_local_hour: int | None = None,
        ) -> DailyDigestReport:
            del session, mailer, llm, clock
            calls.append((ctx.workspace_id, due_local_hour))
            return DailyDigestReport(
                recipients_considered=1,
                sent=1,
                skipped_not_due=0,
                skipped_empty=0,
                skipped_existing=0,
                llm_rendered=0,
                template_rendered=1,
            )

        monkeypatch.setattr(messaging_jobs, "get_settings", lambda: _FakeSettings())
        monkeypatch.setattr(messaging_jobs, "make_uow", lambda: _FakeUow())
        monkeypatch.setattr("app.adapters.mail.smtp.SMTPMailer", _FakeMailer)
        monkeypatch.setattr(
            "app.adapters.mail.smtp_config.DeploymentSmtpConfigSource",
            _FakeSmtpSource,
        )
        monkeypatch.setattr(
            "app.worker.tasks.daily_digest.send_daily_digest",
            fake_send_daily_digest,
        )
        import sqlalchemy.orm as _orm_mod

        monkeypatch.setattr(_orm_mod, "Session", _FakeSession)

        body = messaging_jobs._make_daily_digest_fanout_body(clock)
        body()

        assert calls == [("01HWA00000000000000000WSP1", 7)]


# ---------------------------------------------------------------------------
# LLM budget refresh job (cd-ca1k)
# ---------------------------------------------------------------------------


class TestLlmBudgetRefreshJob:
    """Registration shape + clock propagation for the 60 s refresh job.

    The body's fan-out behaviour (skip missing ledger, isolate broken
    workspaces, sum total_cents) is covered end-to-end in
    ``tests/integration/test_worker_llm_budget.py`` against a real
    engine — the unit layer only asserts what can be proven without a
    DB: the registration metadata, idempotent re-registration, and
    that the injected clock reaches the closure.
    """

    def test_adds_llm_budget_refresh_job_at_60s_interval(self) -> None:
        """Job is registered with the pinned interval + coalesce settings.

        Ties the concrete APScheduler trigger shape to the spec's
        60 s cadence. ``coalesce=True`` + ``max_instances=1`` + a 90 s
        ``misfire_grace_time`` are the three knobs the task description
        enumerates; asserting the exact values here pins them so a
        future registration refactor has to update this test in
        lockstep (and surface the operator-visible cadence change).
        """
        from apscheduler.triggers.interval import IntervalTrigger

        sched = create_scheduler()
        register_jobs(sched)

        job = sched.get_job(LLM_BUDGET_REFRESH_JOB_ID)
        assert job is not None, (
            f"{LLM_BUDGET_REFRESH_JOB_ID} not registered by register_jobs"
        )

        # Trigger: IntervalTrigger at 60 s.
        assert isinstance(job.trigger, IntervalTrigger)
        # APScheduler stores the interval as a :class:`datetime.timedelta`;
        # compare the total seconds to stay readable.
        assert job.trigger.interval.total_seconds() == 60.0
        assert LLM_BUDGET_REFRESH_INTERVAL_SECONDS == 60

        # Wrapper knobs: misfire grace 90 s, coalesce on, single
        # instance. A late restart up to 90 s catches up; beyond that
        # the next tick picks up and the skipped window is recovered
        # by the next refresh (the function is idempotent — it
        # rewrites the same sum).
        assert job.misfire_grace_time == 90
        assert job.coalesce is True
        assert job.max_instances == 1

    def test_is_idempotent(self) -> None:
        """Re-registering keeps exactly one budget-refresh job.

        Same invariant as :class:`TestRegisterJobs.
        test_is_idempotent_under_replace_existing` but pinned on
        the new job id — a regression that missed the new id in the
        ``remove_job`` loop would leave duplicate pending entries
        here without the suite-wide set assertion catching it. The
        head-level count already catches duplicates; this narrower
        test makes the regression signature obvious.
        """
        sched = create_scheduler()
        register_jobs(sched)
        register_jobs(sched)

        matching = [j for j in sched.get_jobs() if j.id == LLM_BUDGET_REFRESH_JOB_ID]
        assert len(matching) == 1

        # Trigger shape survives the re-register.
        from apscheduler.triggers.interval import IntervalTrigger

        assert isinstance(matching[0].trigger, IntervalTrigger)
        assert matching[0].trigger.interval.total_seconds() == 60.0

    def test_uses_resolved_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Injected :class:`FrozenClock` propagates into the refresh body.

        The factory closes over the scheduler's clock at registration
        time, not at tick time (matches the idempotency-sweep pattern).
        Without this assertion a future refactor that reached for
        :class:`~app.util.clock.SystemClock` inside the body would
        silently trip every FrozenClock-driven test by falling back to
        the OS clock — a hazard that cost the generator-fan-out work
        an iteration. We prove propagation by patching
        :func:`app.domain.llm.budget.refresh_aggregate` and observing
        the clock kwarg the body hands in.
        """
        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))

        seen_clocks: list[object] = []

        def fake_refresh_aggregate(
            session: object,
            ctx: object,
            *,
            clock: object | None = None,
        ) -> int:
            seen_clocks.append(clock)
            return 0

        monkeypatch.setattr(
            "app.domain.llm.budget.refresh_aggregate",
            fake_refresh_aggregate,
        )

        # Fabricate a single workspace row so the body's SELECT
        # returns one tenant to dispatch into the patched
        # ``refresh_aggregate``. A direct monkeypatch on the execute
        # path keeps the test DB-free — the scheduler body only
        # reaches SQLAlchemy through ``session.execute(select(...))``.
        class _FakeRow:
            def __init__(self, id: str, slug: str) -> None:
                self.id = id
                self.slug = slug

        fake_rows = [_FakeRow("01HWA00000000000000000WSP1", "ws-one")]

        class _FakeResult:
            def all(self) -> list[_FakeRow]:
                return list(fake_rows)

        class _FakeNestedTx:
            """Trivial context manager standing in for ``session.begin_nested()``.

            The real call opens a SAVEPOINT; here the body just needs
            a context manager that enters / exits cleanly so the
            ``with session.begin_nested(): ...`` block around the
            per-workspace refresh runs. We don't need rollback
            semantics because the happy-path test never raises.
            """

            def __enter__(self) -> _FakeNestedTx:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        class _FakeSession:
            """Minimal stand-in for ``sqlalchemy.orm.Session`` — only the
            methods the body touches are implemented. The ``isinstance``
            guard on ``Session`` in the body is patched below so the
            fake session survives the runtime type check.
            """

            def execute(self, _stmt: object) -> _FakeResult:
                return _FakeResult()

            def scalar(self, _stmt: object) -> str:
                """Stand-in for ``session.scalar(select(BudgetLedger.id)...)``.

                The body pre-checks ledger presence before calling
                :func:`refresh_aggregate` — a truthy return (any
                non-``None`` value) tells the body the ledger exists
                and the refresh path should fire. Returning a fixed
                ULID-shaped sentinel keeps the test DB-free while
                guiding the body past the ``no_ledger`` early-skip.
                """
                return "01HWA00000000000000000LGR0"

            def begin_nested(self) -> _FakeNestedTx:
                return _FakeNestedTx()

        class _FakeUow:
            """Context-manager shim imitating :class:`UnitOfWorkImpl`."""

            def __enter__(self) -> _FakeSession:
                return _FakeSession()

            def __exit__(self, *exc: object) -> None:
                return None

        # Patch the seams the body pulls from:
        #   * ``make_uow`` — hand back the fake UoW.
        #   * ``Session`` — isinstance check flips to ``_FakeSession``.
        monkeypatch.setattr(llm_budget_jobs, "make_uow", lambda: _FakeUow())
        import sqlalchemy.orm as _orm_mod

        monkeypatch.setattr(_orm_mod, "Session", _FakeSession)

        body = llm_budget_jobs._make_llm_budget_refresh_body(clock)
        body()

        # The body dispatched one ``refresh_aggregate`` call and
        # handed the patched clock through.
        assert len(seen_clocks) == 1
        assert seen_clocks[0] is clock


# ---------------------------------------------------------------------------
# user_workspace derive-refresh job (cd-yqm4)
# ---------------------------------------------------------------------------


class TestUserWorkspaceRefreshJob:
    """Registration shape + clock propagation for the cd-yqm4 derive-refresh job.

    The body's reconciliation behaviour (insert / delete / source-flip)
    is covered end-to-end against a real engine in the integration
    suite under ``tests/integration/identity/test_user_workspace_refresh.py``;
    the unit layer only asserts what can be proven without a DB:
    registration metadata, idempotent re-registration, and that the
    injected clock reaches the closure.
    """

    def test_adds_job_at_5min_interval(self) -> None:
        """Registered with the pinned interval + coalesce settings."""
        from apscheduler.triggers.interval import IntervalTrigger

        sched = create_scheduler()
        register_jobs(sched)

        job = sched.get_job(USER_WORKSPACE_REFRESH_JOB_ID)
        assert job is not None, (
            f"{USER_WORKSPACE_REFRESH_JOB_ID} not registered by register_jobs"
        )

        # Trigger: IntervalTrigger at the pinned cadence.
        assert isinstance(job.trigger, IntervalTrigger)
        assert (
            job.trigger.interval.total_seconds()
            == USER_WORKSPACE_REFRESH_INTERVAL_SECONDS
        )

        # Wrapper knobs: misfire grace == one full interval, coalesce
        # on, single instance. One tick late is tolerated (idempotent
        # reconcile); two ticks late skip rather than stack.
        assert job.misfire_grace_time == USER_WORKSPACE_REFRESH_INTERVAL_SECONDS
        assert job.coalesce is True
        assert job.max_instances == 1

    def test_is_idempotent(self) -> None:
        """Re-registering keeps exactly one user_workspace_refresh job."""
        sched = create_scheduler()
        register_jobs(sched)
        register_jobs(sched)

        matching = [
            j for j in sched.get_jobs() if j.id == USER_WORKSPACE_REFRESH_JOB_ID
        ]
        assert len(matching) == 1

    def test_uses_resolved_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Injected :class:`FrozenClock` propagates into the refresh body.

        The factory closes over the scheduler's clock at registration
        time (matches the LLM-budget-refresh / idempotency-sweep
        pattern). A regression that reached for
        :class:`~app.util.clock.SystemClock` inside the body would
        silently trip every FrozenClock-driven test by falling back to
        the OS clock; we prove propagation by patching
        :func:`reconcile_user_workspace` and observing the ``now`` arg.
        """
        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))

        seen_now: list[object] = []

        def fake_reconcile(session: object, *, now: object) -> object:
            seen_now.append(now)

            class _Report:
                rows_inserted = 0
                rows_deleted = 0
                rows_source_flipped = 0
                upstream_pairs_seen = 0

            return _Report()

        # Patch the deferred import target — the body imports
        # ``reconcile_user_workspace`` from the domain module, so we
        # patch it there.
        monkeypatch.setattr(
            "app.domain.identity.user_workspace_refresh.reconcile_user_workspace",
            fake_reconcile,
        )

        # Fake UoW + Session so the body never reaches a real DB.
        class _FakeSession:
            pass

        class _FakeUow:
            def __enter__(self) -> _FakeSession:
                return _FakeSession()

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(identity_jobs, "make_uow", lambda: _FakeUow())
        import sqlalchemy.orm as _orm_mod

        # Flip ``Session`` to ``_FakeSession`` so the body's
        # ``isinstance(session, Session)`` narrowing accepts the fake.
        monkeypatch.setattr(_orm_mod, "Session", _FakeSession)

        body = identity_jobs._make_user_workspace_refresh_body(clock)
        body()

        # The body dispatched one ``reconcile_user_workspace`` call
        # and handed the patched clock's ``now()`` through.
        assert seen_now == [clock.now()]


# ---------------------------------------------------------------------------
# Invite TTL sweep (cd-za45)
# ---------------------------------------------------------------------------


class TestInviteTtlJob:
    """Registration shape + clock propagation for the cd-za45 invite TTL sweep.

    The body's per-row state flip + event publish behaviour is covered
    against an in-memory engine in
    ``tests/unit/identity/test_membership.py::TestPruneStaleInvites``;
    the unit layer here pins the registration metadata + clock
    propagation so a future refactor cannot silently change the
    operator-visible cadence or break the FrozenClock-driven test seam.
    Mirrors :class:`TestUserWorkspaceRefreshJob` exactly.
    """

    def test_adds_invite_ttl_job_at_15min_interval(self) -> None:
        """Registered with the pinned interval + coalesce settings."""
        from apscheduler.triggers.interval import IntervalTrigger

        sched = create_scheduler()
        register_jobs(sched)

        job = sched.get_job(INVITE_TTL_JOB_ID)
        assert job is not None, f"{INVITE_TTL_JOB_ID} not registered by register_jobs"

        # IntervalTrigger at 900 s (15 min) — matches the sibling
        # approval-TTL cadence.
        assert isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval.total_seconds() == 900.0
        assert INVITE_TTL_INTERVAL_SECONDS == 900

        # Wrapper knobs: misfire grace == one full interval, coalesce
        # on, single instance. One tick late is tolerated (idempotent
        # sweep — rows in terminal state fall out of the predicate);
        # two-ticks-late skip rather than stack.
        assert job.misfire_grace_time == INVITE_TTL_INTERVAL_SECONDS
        assert job.coalesce is True
        assert job.max_instances == 1

    def test_is_idempotent(self) -> None:
        """Re-registering keeps exactly one invite_ttl_sweep job."""
        sched = create_scheduler()
        register_jobs(sched)
        register_jobs(sched)

        matching = [j for j in sched.get_jobs() if j.id == INVITE_TTL_JOB_ID]
        assert len(matching) == 1

    def test_uses_resolved_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Injected :class:`FrozenClock` propagates into the sweep body.

        The factory closes over the scheduler's clock at registration
        time (matches the approval-TTL / LLM-budget-refresh pattern).
        A regression that reached for :class:`SystemClock` inside the
        body would silently trip every FrozenClock-driven test by
        falling back to the OS clock; we prove propagation by patching
        the deferred-import target and observing the ``clock`` arg.
        """
        from app.worker.jobs import maintenance as maintenance_jobs

        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        seen_clocks: list[object] = []

        class _FakeReport:
            expired_count = 0
            expired_ids: tuple[str, ...] = ()

        def fake_sweep(*, clock: object) -> _FakeReport:  # type: ignore[no-untyped-def]
            seen_clocks.append(clock)
            return _FakeReport()

        # Patch the deferred-import target — the body imports
        # ``sweep_expired_invites`` from the task module, so we patch
        # it there.
        import app.worker.tasks.invite_ttl as _invite_ttl_mod

        monkeypatch.setattr(_invite_ttl_mod, "sweep_expired_invites", fake_sweep)

        body = maintenance_jobs._make_invite_ttl_body(clock)
        body()

        assert seen_clocks == [clock]


# ---------------------------------------------------------------------------
# Signup GC (cd-hnk40)
# ---------------------------------------------------------------------------


class TestSignupGcJob:
    """Registration shape + clock/session propagation for signup GC."""

    def test_adds_signup_gc_job_at_hourly_interval(self) -> None:
        """Registered with the pinned interval + coalesce settings."""
        from apscheduler.triggers.interval import IntervalTrigger

        sched = create_scheduler()
        register_jobs(sched)

        job = sched.get_job(SIGNUP_GC_JOB_ID)
        assert job is not None, f"{SIGNUP_GC_JOB_ID} not registered by register_jobs"

        assert isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval.total_seconds() == 3600.0
        assert SIGNUP_GC_INTERVAL_SECONDS == 3600

        assert job.misfire_grace_time == SIGNUP_GC_INTERVAL_SECONDS
        assert job.coalesce is True
        assert job.max_instances == 1

    def test_is_idempotent(self) -> None:
        """Re-registering keeps exactly one signup_gc job."""
        sched = create_scheduler()
        register_jobs(sched)
        register_jobs(sched)

        matching = [j for j in sched.get_jobs() if j.id == SIGNUP_GC_JOB_ID]
        assert len(matching) == 1

    def test_uses_resolved_clock_and_uow_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Body calls ``prune_stale_signups`` with the injected clock's now."""
        import app.auth.signup as _signup_mod

        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        seen_calls: list[tuple[object, datetime]] = []

        class _FakeSession:
            pass

        fake_session = _FakeSession()

        def fake_prune(session: object, *, now: datetime) -> list[str]:
            seen_calls.append((session, now))
            return ["01HWA00000000000000000WSP1"]

        class _FakeUow:
            def __enter__(self) -> _FakeSession:
                return fake_session

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(_signup_mod, "prune_stale_signups", fake_prune)
        monkeypatch.setattr(maintenance_jobs, "make_uow", lambda: _FakeUow())
        import sqlalchemy.orm as _orm_mod

        monkeypatch.setattr(_orm_mod, "Session", _FakeSession)

        body = maintenance_jobs._make_signup_gc_body(clock)
        body()

        assert seen_calls == [(fake_session, clock.now())]


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------


class TestStartStop:
    def test_start_is_idempotent(self) -> None:
        """Calling :func:`start` on a running scheduler is a no-op.

        Drives the coroutine via :func:`asyncio.run` so the AsyncIO
        scheduler's internal loop reference resolves correctly.
        """

        async def _run() -> None:
            sched = create_scheduler()
            start(sched)
            assert sched.running
            # Second start must not raise SchedulerAlreadyRunningError.
            start(sched)
            assert sched.running
            stop(sched)

        asyncio.run(_run())

    def test_stop_is_idempotent(self) -> None:
        """Calling :func:`stop` on a stopped scheduler is a no-op."""
        sched = create_scheduler()
        # Never started — stop must not raise SchedulerNotRunningError.
        stop(sched)
        assert sched.running is False


# ---------------------------------------------------------------------------
# wrap_job
# ---------------------------------------------------------------------------


class TestWrapJob:
    """Cover the three wrapper responsibilities: run, log, heartbeat."""

    def test_runs_body_and_writes_heartbeat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Successful body → heartbeat upsert keyed by ``job_id``."""
        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        seen_calls: list[tuple[str, datetime]] = []

        def fake_write(job_id: str, injected_clock: object) -> None:
            assert injected_clock is clock
            seen_calls.append((job_id, clock.now()))

        monkeypatch.setattr(scheduler_mod, "_write_heartbeat", fake_write)
        monkeypatch.setattr(scheduler_mod, "_is_dead", lambda _job_id: False)

        body = MagicMock()
        wrapped = wrap_job(body, job_id="test_job", clock=clock)
        asyncio.run(wrapped())

        body.assert_called_once_with()
        assert seen_calls == [("test_job", clock.now())]

    def test_body_exception_is_swallowed_and_logged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        """A raising body logs at ERROR and the tick completes without raising.

        The heartbeat must NOT advance on a failed run — the whole
        point of the staleness window is that a broken job stops
        bumping the row.
        """
        # Alembic's fileConfig can flip ``propagate=False`` on named
        # loggers across the test session. Enable propagation so
        # ``caplog`` sees the records.
        allow_propagated_log_capture("app.worker.scheduler")  # type: ignore[operator]

        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        write_calls: list[str] = []
        monkeypatch.setattr(
            scheduler_mod,
            "_write_heartbeat",
            lambda job_id, _clock: write_calls.append(job_id),
        )
        # cd-8euz: a failing body now invokes the failure-state writer
        # too. Stub it so the unit test does not reach the DB; the
        # call IS expected and is what carries the consecutive-failure
        # bookkeeping in production.
        failure_calls: list[str] = []
        monkeypatch.setattr(
            scheduler_mod,
            "_record_failure",
            lambda job_id, _clock: failure_calls.append(job_id),
        )
        monkeypatch.setattr(scheduler_mod, "_is_dead", lambda _job_id: False)

        def body() -> None:
            raise RuntimeError("boom")

        wrapped = wrap_job(body, job_id="flaky", clock=clock)
        with caplog.at_level(logging.ERROR, logger="app.worker.scheduler"):
            asyncio.run(wrapped())  # must not raise

        assert write_calls == []
        assert failure_calls == ["flaky"]
        error_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.tick.error"
        ]
        assert len(error_events) == 1
        assert getattr(error_events[0], "job_id", None) == "flaky"

    def test_base_exception_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SystemExit / KeyboardInterrupt must bubble past the wrapper.

        The shutdown path relies on these propagating — catching
        :class:`BaseException` would wedge the process on Ctrl+C.
        """
        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        monkeypatch.setattr(
            scheduler_mod,
            "_write_heartbeat",
            lambda job_id, _clock: None,
        )
        monkeypatch.setattr(
            scheduler_mod,
            "_record_failure",
            lambda job_id, _clock: None,
        )
        monkeypatch.setattr(scheduler_mod, "_is_dead", lambda _job_id: False)

        def body() -> None:
            raise KeyboardInterrupt()

        wrapped = wrap_job(body, job_id="ctrl_c", clock=clock)
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(wrapped())

    def test_heartbeat_can_be_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``heartbeat=False`` opts a job out of the upsert."""
        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        write_calls: list[str] = []
        monkeypatch.setattr(
            scheduler_mod,
            "_write_heartbeat",
            lambda job_id, _clock: write_calls.append(job_id),
        )

        body = MagicMock()
        wrapped = wrap_job(body, job_id="silent", clock=clock, heartbeat=False)
        asyncio.run(wrapped())

        body.assert_called_once_with()
        assert write_calls == []

    def test_heartbeat_failure_does_not_raise(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        """A heartbeat-write crash logs at ERROR and the tick returns.

        The scheduler must survive a transient DB outage — the next
        tick retries and the staleness window escalates if the DB
        stays down.
        """
        allow_propagated_log_capture("app.worker.scheduler")  # type: ignore[operator]

        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))

        def failing_write(job_id: str, _clock: object) -> None:
            raise RuntimeError("db down")

        monkeypatch.setattr(scheduler_mod, "_write_heartbeat", failing_write)
        # cd-8euz: a heartbeat write that raises flips the tick into
        # the error branch; stub the failure-state writer so the test
        # stays DB-free.
        monkeypatch.setattr(
            scheduler_mod, "_record_failure", lambda _job_id, _clock: None
        )
        monkeypatch.setattr(scheduler_mod, "_is_dead", lambda _job_id: False)

        body = MagicMock()
        wrapped = wrap_job(body, job_id="hb_flap", clock=clock)
        with caplog.at_level(logging.ERROR, logger="app.worker.scheduler"):
            asyncio.run(wrapped())  # must not raise

        hb_errors = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.heartbeat.error"
        ]
        assert len(hb_errors) == 1

    def test_async_body_is_awaited_on_event_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ``async def`` body is awaited — not silently skipped.

        Regression guard for the ``asyncio.to_thread(func)`` trap:
        calling ``to_thread`` on a coroutine function returns an
        un-awaited coroutine object and the body never executes. The
        heartbeat would still upsert, so ``/readyz`` would stay green
        while the real work vanished into a :class:`RuntimeWarning`.
        Downstream tasks planning an async body (LLM fan-out, async
        HTTP clients) rely on this path.
        """
        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        monkeypatch.setattr(
            scheduler_mod,
            "_write_heartbeat",
            lambda job_id, _clock: None,
        )
        monkeypatch.setattr(scheduler_mod, "_is_dead", lambda _job_id: False)

        run_count = 0

        async def async_body() -> None:
            nonlocal run_count
            run_count += 1

        wrapped = wrap_job(async_body, job_id="async_tick", clock=clock)
        asyncio.run(wrapped())

        assert run_count == 1

    def test_async_body_exception_is_swallowed_and_logged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        """A raising ``async def`` body is caught the same as a sync one.

        Parity assertion: the ``Exception`` handler must fire for
        awaitable bodies too, otherwise a single async job crash
        would escape into APScheduler's own error handling and lose
        the ``worker.tick.error`` event marker.
        """
        allow_propagated_log_capture("app.worker.scheduler")  # type: ignore[operator]

        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        write_calls: list[str] = []
        monkeypatch.setattr(
            scheduler_mod,
            "_write_heartbeat",
            lambda job_id, _clock: write_calls.append(job_id),
        )
        # cd-8euz: keep the async-failure test DB-free by stubbing the
        # killswitch read + failure-state writer.
        failure_calls: list[str] = []
        monkeypatch.setattr(
            scheduler_mod,
            "_record_failure",
            lambda job_id, _clock: failure_calls.append(job_id),
        )
        monkeypatch.setattr(scheduler_mod, "_is_dead", lambda _job_id: False)

        async def async_body() -> None:
            raise RuntimeError("async boom")

        wrapped = wrap_job(async_body, job_id="async_flaky", clock=clock)
        with caplog.at_level(logging.ERROR, logger="app.worker.scheduler"):
            asyncio.run(wrapped())  # must not raise

        assert write_calls == []
        assert failure_calls == ["async_flaky"]
        error_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.tick.error"
        ]
        assert len(error_events) == 1
        assert getattr(error_events[0], "job_id", None) == "async_flaky"


# ---------------------------------------------------------------------------
# Failure metrics + killswitch (cd-8euz)
# ---------------------------------------------------------------------------


class TestFailureMetricsAndKillswitch:
    """cd-8euz: per-tick counter labels + killswitch short-circuit.

    The Prometheus counter and the audit-write side effects are
    durably tested in ``tests/unit/worker/test_job_state.py`` against a
    real in-memory engine; this class pins the wrapper-level behaviour
    that:

    * a successful tick increments ``status="ok"`` and a failing tick
      increments ``status="error"``,
    * a job whose ``worker_heartbeat.dead_at`` is non-NULL skips the
      body and increments ``status="dead"`` instead.

    Both labels share the spec-pinned ``crewday_worker_jobs_total``
    metric (§16 "Metrics") — the cd-8euz slice only adds the new
    ``"dead"`` value to the existing label set.
    """

    @staticmethod
    def _label_value(label_status: str) -> float:
        from app.observability.metrics import WORKER_JOBS_TOTAL

        # ``Counter._value`` is the documented private accessor used by
        # the prometheus_client docs themselves; reaching for ``_value``
        # avoids spinning up an HTTP exposition pipeline just to read
        # one increment in a unit test.
        return WORKER_JOBS_TOTAL.labels(  # type: ignore[no-any-return]
            job="cd_8euz_test",
            status=label_status,
        )._value.get()

    def test_success_increments_ok_counter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clean tick advances ``crewday_worker_jobs_total{status="ok"}``."""
        clock = FrozenClock(datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
        monkeypatch.setattr(
            scheduler_mod, "_write_heartbeat", lambda _job_id, _clock: None
        )
        monkeypatch.setattr(scheduler_mod, "_is_dead", lambda _job_id: False)

        before_ok = self._label_value("ok")
        before_error = self._label_value("error")
        before_dead = self._label_value("dead")

        wrapped = wrap_job(MagicMock(), job_id="cd_8euz_test", clock=clock)
        asyncio.run(wrapped())

        assert self._label_value("ok") == before_ok + 1
        assert self._label_value("error") == before_error
        assert self._label_value("dead") == before_dead

    def test_failure_increments_error_counter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        allow_propagated_log_capture: object,
    ) -> None:
        """A failing tick advances ``crewday_worker_jobs_total{status="error"}``."""
        allow_propagated_log_capture("app.worker.scheduler")  # type: ignore[operator]

        clock = FrozenClock(datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
        monkeypatch.setattr(
            scheduler_mod, "_write_heartbeat", lambda _job_id, _clock: None
        )
        monkeypatch.setattr(
            scheduler_mod, "_record_failure", lambda _job_id, _clock: None
        )
        monkeypatch.setattr(scheduler_mod, "_is_dead", lambda _job_id: False)

        before_ok = self._label_value("ok")
        before_error = self._label_value("error")
        before_dead = self._label_value("dead")

        def body() -> None:
            raise RuntimeError("boom")

        wrapped = wrap_job(body, job_id="cd_8euz_test", clock=clock)
        asyncio.run(wrapped())

        assert self._label_value("error") == before_error + 1
        assert self._label_value("ok") == before_ok
        assert self._label_value("dead") == before_dead

    def test_killswitch_read_failure_fails_open_and_runs_body(
        self,
        monkeypatch: pytest.MonkeyPatch,
        allow_propagated_log_capture: object,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A DB hiccup on the killswitch read must NOT escape the wrapper.

        The cd-7c0p contract is "wrap_job swallows every ``Exception``
        so the scheduler / broker stays alive". cd-8euz added an
        ``is_dead`` read at the top of the wrapper that opens its own
        UoW; without an explicit guard, a transient DB outage on that
        read would propagate up into APScheduler. We fail-open
        (run the body) on a read failure because fail-closed would
        skip every tick on a momentary blip and the staleness window
        already escalates a persistent outage via ``/readyz``.
        """
        allow_propagated_log_capture("app.worker.scheduler")  # type: ignore[operator]

        clock = FrozenClock(datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
        write_calls: list[str] = []
        monkeypatch.setattr(
            scheduler_mod,
            "_write_heartbeat",
            lambda job_id, _clock: write_calls.append(job_id),
        )

        def boom(_job_id: str) -> bool:
            raise RuntimeError("db down")

        monkeypatch.setattr(scheduler_mod, "_is_dead", boom)

        body = MagicMock()
        wrapped = wrap_job(body, job_id="cd_8euz_test", clock=clock)
        with caplog.at_level(logging.ERROR, logger="app.worker.scheduler"):
            asyncio.run(wrapped())  # must not raise

        body.assert_called_once_with()
        assert write_calls == ["cd_8euz_test"]
        read_errors = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.job.killswitch_read_error"
        ]
        assert len(read_errors) == 1

    def test_dead_job_skips_body_and_increments_dead_counter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        allow_propagated_log_capture: object,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A dead job skips the body, the heartbeat, and bumps ``status="dead"``.

        The killswitch read fires before anything else in ``_runner``;
        the body, the heartbeat upsert, and the ``ok``/``error``
        counter are all bypassed. The skip is logged at WARNING with
        ``event="worker.tick.dead_skip"`` so an operator scraping the
        JSON log stream can isolate killswitch-skipped ticks from
        legitimate runs.
        """
        allow_propagated_log_capture("app.worker.scheduler")  # type: ignore[operator]

        clock = FrozenClock(datetime(2026, 5, 2, 12, 0, tzinfo=UTC))

        write_calls: list[str] = []
        failure_calls: list[str] = []
        monkeypatch.setattr(
            scheduler_mod,
            "_write_heartbeat",
            lambda job_id, _clock: write_calls.append(job_id),
        )
        monkeypatch.setattr(
            scheduler_mod,
            "_record_failure",
            lambda job_id, _clock: failure_calls.append(job_id),
        )
        monkeypatch.setattr(scheduler_mod, "_is_dead", lambda _job_id: True)

        before_ok = self._label_value("ok")
        before_error = self._label_value("error")
        before_dead = self._label_value("dead")

        body = MagicMock()
        wrapped = wrap_job(body, job_id="cd_8euz_test", clock=clock)
        with caplog.at_level(logging.WARNING, logger="app.worker.scheduler"):
            asyncio.run(wrapped())

        body.assert_not_called()
        assert write_calls == []
        assert failure_calls == []
        assert self._label_value("dead") == before_dead + 1
        assert self._label_value("ok") == before_ok
        assert self._label_value("error") == before_error

        skip_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.tick.dead_skip"
        ]
        assert len(skip_events) == 1
        assert getattr(skip_events[0], "job_id", None) == "cd_8euz_test"
