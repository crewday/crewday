"""Integration tests for the hourly occurrence-generator fan-out (cd-dcl2).

End-to-end proof that
:func:`~app.worker.scheduler._make_generator_fanout_body` walks every
:class:`~app.adapters.db.workspace.models.Workspace` row, dispatches a
per-workspace :func:`~app.worker.tasks.generator.generate_task_occurrences`
call against the real UoW seam, and surfaces per-workspace +
aggregate counts in the structured log stream.

Unit coverage for the registration shape (the tick is registered
under :data:`GENERATOR_JOB_ID` with a top-of-hour
:class:`~apscheduler.triggers.cron.CronTrigger`) lives in
``tests/unit/worker/test_scheduler.py``. This suite covers what
that layer cannot:

* Two workspaces — one with a schedule, one without — produce
  ``tasks_created`` attributed to the right workspace_id in the
  per-workspace ``worker.generator.workspace.tick`` log payload.
* A workspace whose ``generate_task_occurrences`` call raises
  logs at WARNING and the OTHER workspace still materialises.
* The tick emits the ``worker.generator.tick.summary`` INFO record
  with the cd-dcl2-pinned aggregate field set
  (``total_workspaces`` / ``total_workspaces_skipped`` /
  ``total_workspaces_failed`` / ``total_schedules_walked`` /
  ``total_tasks_created`` / ``total_skipped_duplicate`` /
  ``total_skipped_for_closure``).

See ``docs/specs/06-tasks-and-scheduling.md`` §"Generation",
``docs/specs/24-demo-mode.md`` §"Garbage collection", and
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.places.models import Property
from app.adapters.db.tasks.models import Occurrence, Schedule, TaskTemplate
from app.adapters.db.workspace.models import Workspace
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import Clock, FrozenClock
from app.util.ulid import new_ulid
from app.worker import scheduler as scheduler_mod
from app.worker.scheduler import _make_generator_fanout_body
from app.worker.tasks.generator import GenerationReport

pytestmark = pytest.mark.integration


# Pinned wall-clock for the suite. The fan-out body threads the
# injected clock into :func:`generate_task_occurrences`, so the
# seeded schedule's ``active_from`` / ``dtstart_local`` are computed
# relative to this instant rather than ``datetime.now(UTC)`` (which
# would drift on slow CI runs and push the schedule outside the
# horizon between seed and tick).
_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_tenancy_context() -> Iterator[None]:
    """Every test starts without an active :class:`WorkspaceContext`.

    The fan-out body sets / resets the ContextVar inside its loop;
    a leaked ctx from a sibling case would silently satisfy the
    tenant filter when the body opens its own UoW outside the
    bracket. Same posture as the LLM-budget integration suite.
    """
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture
def real_make_uow(engine: Engine) -> Iterator[None]:
    """Redirect the process-wide default UoW to the integration engine.

    The fan-out body opens its own UoW via
    :func:`app.adapters.db.session.make_uow`, so we point the
    process-wide default at the integration engine — same plumbing
    the idempotency-sweep + LLM-budget integration suites use.
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
def clean_generator_tables(engine: Engine) -> Iterator[None]:
    """Empty workspace-related tables before AND after each test.

    The harness engine is session-scoped, so cross-test bleed would
    otherwise mask regressions: a stale ``Occurrence`` row from a
    sibling test would trivially satisfy the "tasks created" count
    even if the fan-out never ran. We delete in dependency order
    (children first) so FK CASCADEs do not surprise the next test.
    """
    with engine.begin() as conn:
        conn.execute(delete(Occurrence))
        conn.execute(delete(Schedule))
        conn.execute(delete(TaskTemplate))
        conn.execute(delete(Property))
        conn.execute(delete(Workspace))
    yield
    with engine.begin() as conn:
        conn.execute(delete(Occurrence))
        conn.execute(delete(Schedule))
        conn.execute(delete(TaskTemplate))
        conn.execute(delete(Property))
        conn.execute(delete(Workspace))


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _seed_workspace(engine: Engine, *, slug: str) -> str:
    """Insert one :class:`Workspace` row and return its id."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    workspace_id = new_ulid()
    with factory() as session, tenant_agnostic():
        session.add(
            Workspace(
                id=workspace_id,
                slug=slug,
                name=f"Workspace {slug}",
                plan="free",
                quota_json={},
                created_at=_PINNED,
            )
        )
        session.commit()
    return workspace_id


def _seed_schedule(engine: Engine, *, workspace_id: str) -> str:
    """Seed a property + template + weekly-Saturday schedule.

    Returns the schedule id. Mirrors the seed fixture in
    ``tests/integration/test_tasks_generator_run.py`` so the
    generator's per-branch coverage there carries over to the
    fan-out path here.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    schedule_id = new_ulid()
    with factory() as session, tenant_agnostic():
        property_id = new_ulid()
        session.add(
            Property(
                id=property_id,
                address="1 Villa Sud Way",
                timezone="Europe/Paris",
                tags_json=[],
                created_at=_PINNED,
            )
        )
        session.flush()

        template_id = new_ulid()
        session.add(
            TaskTemplate(
                id=template_id,
                workspace_id=workspace_id,
                title="Villa Sud pool",
                name="Villa Sud pool",
                description_md="",
                default_duration_min=60,
                duration_minutes=60,
                required_evidence="none",
                photo_required=False,
                default_assignee_role=None,
                role_id="role-housekeeper",
                property_scope="any",
                listed_property_ids=[],
                area_scope="any",
                listed_area_ids=[],
                checklist_template_json=[],
                photo_evidence="disabled",
                linked_instruction_ids=[],
                priority="normal",
                inventory_consumption_json={},
                llm_hints_md=None,
                created_at=_PINNED,
                updated_at=_PINNED,
            )
        )
        session.flush()

        session.add(
            Schedule(
                id=schedule_id,
                workspace_id=workspace_id,
                template_id=template_id,
                property_id=property_id,
                name="Villa Sud pool schedule",
                area_id=None,
                rrule_text="FREQ=WEEKLY;BYDAY=SA",
                dtstart=datetime(2026, 4, 18, 9, 0, tzinfo=UTC),
                dtstart_local="2026-04-18T09:00",
                until=None,
                duration_minutes=60,
                rdate_local="",
                exdate_local="",
                active_from="2026-04-01",
                active_until=None,
                paused_at=None,
                deleted_at=None,
                assignee_user_id=None,
                backup_assignee_user_ids=[],
                assignee_role=None,
                enabled=True,
                next_generation_at=None,
                created_at=_PINNED,
            )
        )
        session.commit()
    return schedule_id


def _count_occurrences(engine: Engine, *, workspace_id: str) -> int:
    """Count occurrences in ``workspace_id`` (cross-tenant view)."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session, tenant_agnostic():
        stmt = select(Occurrence).where(Occurrence.workspace_id == workspace_id)
        return len(list(session.scalars(stmt).all()))


# ---------------------------------------------------------------------------
# Multi-workspace fan-out
# ---------------------------------------------------------------------------


class TestGeneratorFanOut:
    """Drive :func:`_make_generator_fanout_body` against the real engine."""

    def test_per_workspace_attribution_in_log_payload(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_generator_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """Two workspaces — A has a schedule, B does not.

        The cd-dcl2 acceptance criterion: ``tasks_created`` lands in
        the per-workspace log payload keyed by ``workspace_id`` —
        not summed into a single global field that would lose
        attribution.

        After one tick:

        * A's per-workspace tick event reports ``schedules_walked >=
          1`` and ``tasks_created > 0``.
        * B's per-workspace tick event reports zeros across the
          board (no schedules to walk).
        * The aggregate summary event sums the per-workspace
          ``tasks_created`` and reports ``total_workspaces == 2``.
        """
        allow_propagated_log_capture("app.worker.scheduler")

        # Tick at a point AFTER the schedule's first Saturday so the
        # generator finds at least one candidate inside its 30-day
        # horizon. Mirrors the unit-suite anchor.
        frozen = FrozenClock(datetime(2026, 4, 20, 8, 0, tzinfo=UTC))

        ws_a = _seed_workspace(engine, slug="ws-with-schedule")
        ws_b = _seed_workspace(engine, slug="ws-without-schedule")
        _seed_schedule(engine, workspace_id=ws_a)

        body = _make_generator_fanout_body(frozen)
        with caplog.at_level(logging.INFO, logger="app.worker.scheduler"):
            body()

        # Per-workspace tick events — one per attempted workspace.
        ws_events: dict[str, logging.LogRecord] = {}
        for rec in caplog.records:
            if getattr(rec, "event", None) != "worker.generator.workspace.tick":
                continue
            workspace_id = getattr(rec, "workspace_id", None)
            assert isinstance(workspace_id, str), (
                "per-workspace tick event missing workspace_id field"
            )
            ws_events[workspace_id] = rec
        assert set(ws_events.keys()) == {ws_a, ws_b}

        a_event = ws_events[ws_a]
        a_tasks = getattr(a_event, "tasks_created", None)
        assert isinstance(a_tasks, int) and a_tasks > 0, (
            f"workspace A should have materialised at least one occurrence "
            f"from its weekly-Saturday schedule; got tasks_created={a_tasks!r}"
        )
        a_walked = getattr(a_event, "schedules_walked", None)
        assert isinstance(a_walked, int) and a_walked >= 1
        assert getattr(a_event, "workspace_slug", None) == "ws-with-schedule"

        b_event = ws_events[ws_b]
        assert getattr(b_event, "tasks_created", None) == 0
        assert getattr(b_event, "schedules_walked", None) == 0
        assert getattr(b_event, "workspace_slug", None) == "ws-without-schedule"

        # Aggregate summary event — the cd-dcl2-pinned shape.
        summary_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.generator.tick.summary"
        ]
        assert len(summary_events) == 1
        summary = summary_events[0]
        assert summary.levelno == logging.INFO
        assert getattr(summary, "total_workspaces", None) == 2
        assert getattr(summary, "total_workspaces_skipped", None) == 0
        assert getattr(summary, "total_workspaces_failed", None) == 0
        assert getattr(summary, "total_tasks_created", None) == a_tasks
        # Schedules walked is the sum across workspaces — A walked at
        # least one schedule, B walked zero.
        a_walked_count = getattr(a_event, "schedules_walked", 0)
        assert getattr(summary, "total_schedules_walked", None) == a_walked_count
        # No duplicate / closure skips on a clean fixture; pin both so
        # a regression that started double-counting one bucket would
        # surface here rather than only on the per-workspace event.
        assert getattr(summary, "total_skipped_duplicate", None) == 0
        assert getattr(summary, "total_skipped_for_closure", None) == 0

        # Backstop: the actual occurrence rows landed under A, not B.
        assert _count_occurrences(engine, workspace_id=ws_a) == a_tasks
        assert _count_occurrences(engine, workspace_id=ws_b) == 0

    def test_broken_workspace_does_not_abort_tick(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_generator_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Workspace D's :func:`generate_task_occurrences` raises;
        workspace A still materialises and the summary records the
        failure.

        The crash-safety invariant cd-dcl2 calls out: "Per-workspace
        errors must NOT abort the tick — log the failure with
        workspace_id + reason and continue." Without the SAVEPOINT
        + per-workspace catch, one bad tenant would starve the
        whole fleet at hourly cadence.
        """
        allow_propagated_log_capture("app.worker.scheduler")

        frozen = FrozenClock(datetime(2026, 4, 20, 8, 0, tzinfo=UTC))

        ws_a = _seed_workspace(engine, slug="healthy")
        ws_d = _seed_workspace(engine, slug="broken")
        _seed_schedule(engine, workspace_id=ws_a)
        _seed_schedule(engine, workspace_id=ws_d)

        from app.worker.tasks import generator as gen_mod

        real_generate = gen_mod.generate_task_occurrences

        def poisoned_generate(
            ctx: WorkspaceContext,
            *,
            session: Session,
            now: datetime | None = None,
            clock: Clock | None = None,
            horizon_days: int = 30,
        ) -> GenerationReport:
            """Pass through to the real helper unless ``ctx`` targets ws_d.

            The fan-out body hands in a real
            :class:`~app.tenancy.WorkspaceContext` plus the kwargs the
            generator's signature accepts; this wrapper keeps the
            shape strict-typed so a stale signature drift would
            surface at type-check time. AGENTS.md forbids
            ``# type: ignore`` — keeping the kwargs explicit here is
            the small price of staying clean.
            """
            if ctx.workspace_id == ws_d:
                raise RuntimeError("poisoned for test")
            return real_generate(
                ctx,
                session=session,
                now=now,
                clock=clock,
                horizon_days=horizon_days,
            )

        # The fan-out body does ``from app.worker.tasks.generator import
        # generate_task_occurrences`` inside its closure; patching the
        # source module is what re-binds the import on each call.
        monkeypatch.setattr(
            "app.worker.tasks.generator.generate_task_occurrences",
            poisoned_generate,
        )

        body = _make_generator_fanout_body(frozen)
        # Capture INFO + WARNING in the same window so both the
        # per-workspace WARNING for D and the INFO summary at the end
        # of the tick land in ``caplog.records``. ``at_level(WARNING)``
        # alone would drop the summary because the wrapper emits it
        # at INFO.
        with caplog.at_level(logging.INFO, logger="app.worker.scheduler"):
            body()  # MUST NOT raise

        # Per-workspace failure surfaced for D with the error class
        # name; A's per-workspace tick event still INFO-logged.
        failed_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.generator.workspace.failed"
        ]
        assert len(failed_events) == 1
        failed = failed_events[0]
        assert failed.levelno == logging.WARNING
        assert getattr(failed, "workspace_id", None) == ws_d
        assert getattr(failed, "error", None) == "RuntimeError"

        # A's occurrences landed despite D's failure.
        assert _count_occurrences(engine, workspace_id=ws_a) > 0
        assert _count_occurrences(engine, workspace_id=ws_d) == 0

        # Summary records the failure without aborting.
        summary_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.generator.tick.summary"
        ]
        assert len(summary_events) == 1
        summary = summary_events[0]
        assert getattr(summary, "total_workspaces", None) == 2
        assert getattr(summary, "total_workspaces_failed", None) == 1
        # Total tasks_created counts only the healthy workspace's
        # contribution — the failing one rolled back its SAVEPOINT.
        a_count = _count_occurrences(engine, workspace_id=ws_a)
        assert getattr(summary, "total_tasks_created", None) == a_count

    def test_contextvar_resets_after_each_workspace(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_generator_tables: None,
    ) -> None:
        """The per-workspace ``current`` ContextVar is reset after the tick.

        The fan-out body sets / resets the ContextVar inside its
        loop; a leaked ctx would silently satisfy the tenant filter
        for whatever code runs next on the same task (sibling jobs,
        subsequent fan-out ticks). Confirm the body restores the
        outer-task value (``None`` here) on exit, on both the success
        and failure paths.
        """
        from app.tenancy.current import get_current

        # Sanity — fixture installed ``None`` as the outer value.
        assert get_current() is None

        frozen = FrozenClock(_PINNED)
        _seed_workspace(engine, slug="ctx-probe-a")
        _seed_workspace(engine, slug="ctx-probe-b")

        body = _make_generator_fanout_body(frozen)
        body()

        # After a clean run the ContextVar is back at the outer task's
        # value (``None``). Without the ``finally: reset_current(token)``
        # bracket the per-workspace ctx for "ctx-probe-b" would still
        # be installed here.
        assert get_current() is None

    def test_empty_workspace_table_emits_zero_summary(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_generator_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """No workspaces → tick emits a zero-everything summary, no crash.

        Edge case the deployment hits on day one and during a fleet
        rotation: the fan-out must still prove liveness via the
        summary log + heartbeat (the wrapper's heartbeat upsert
        runs after this body returns; not asserted here because the
        wrapper is exercised in the LLM-budget + idempotency-sweep
        integration suites).
        """
        allow_propagated_log_capture("app.worker.scheduler")

        frozen = FrozenClock(_PINNED)
        body = _make_generator_fanout_body(frozen)
        with caplog.at_level(logging.INFO, logger="app.worker.scheduler"):
            body()

        summary_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.generator.tick.summary"
        ]
        assert len(summary_events) == 1
        summary = summary_events[0]
        assert getattr(summary, "total_workspaces", None) == 0
        assert getattr(summary, "total_workspaces_skipped", None) == 0
        assert getattr(summary, "total_workspaces_failed", None) == 0
        assert getattr(summary, "total_schedules_walked", None) == 0
        assert getattr(summary, "total_tasks_created", None) == 0
        assert getattr(summary, "total_skipped_duplicate", None) == 0
        assert getattr(summary, "total_skipped_for_closure", None) == 0


# ---------------------------------------------------------------------------
# Demo-expired filter — forward-compatible no-op until cd-otv3 lands
# ---------------------------------------------------------------------------


class TestDemoExpiredFilter:
    """The demo-expired skip is a forward-compat seam.

    :func:`~app.worker.scheduler._demo_expired_workspace_ids` returns
    an empty set today (the ``demo_workspace`` model does not exist
    yet — cd-otv3 + cd-h0ja are the open tasks that land it). Once
    the model is in place this suite gets a "seed expired
    demo_workspace, assert skip" companion. For now we pin the
    no-op so a regression that started raising ``ImportError`` would
    surface immediately.
    """

    def test_no_demo_workspace_table_returns_empty_set(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_generator_tables: None,
    ) -> None:
        """The helper short-circuits to an empty set when the model is absent.

        Direct call rather than going through the full body — the
        body's behaviour around the empty result is covered by
        :class:`TestGeneratorFanOut` (no skip event, no inflated
        ``total_workspaces_skipped`` count).
        """
        ws_a = _seed_workspace(engine, slug="demo-filter-probe")
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as session, tenant_agnostic():
            result = scheduler_mod._demo_expired_workspace_ids(
                session,
                [ws_a],
                now=_PINNED,
            )
        assert result == set()
