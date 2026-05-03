"""Integration tests for the soft-overdue sweeper fan-out (cd-awmp).

End-to-end proof that
:func:`~app.worker.scheduler._make_overdue_fanout_body` walks every
:class:`~app.adapters.db.workspace.models.Workspace` row, dispatches a
per-workspace :func:`~app.worker.tasks.overdue.detect_overdue` call
against the real UoW seam, and surfaces per-workspace + aggregate
counts in the structured log stream.

This mirrors ``tests/integration/worker/test_generator_fanout.py`` for
the cd-hurw overdue sweeper path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.demo.models import DemoWorkspace
from app.adapters.db.places.models import Property
from app.adapters.db.tasks.models import Occurrence, Schedule, TaskTemplate
from app.adapters.db.workspace.models import Workspace
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import Clock, FrozenClock
from app.util.ulid import new_ulid
from app.worker.scheduler import _make_overdue_fanout_body
from app.worker.tasks.overdue import OverdueReport

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_LOG_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "asctime",
    "message",
}


def _log_payload(record: logging.LogRecord) -> dict[str, object]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _LOG_RECORD_ATTRS and not key.startswith("_")
    }


@pytest.fixture(autouse=True)
def _reset_tenancy_context() -> Iterator[None]:
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture
def real_make_uow(engine: Engine) -> Iterator[None]:
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
def clean_overdue_tables(engine: Engine) -> Iterator[None]:
    def clean() -> None:
        with engine.begin() as conn:
            conn.execute(delete(AuditLog))
            conn.execute(delete(Occurrence))
            conn.execute(delete(Schedule))
            conn.execute(delete(TaskTemplate))
            conn.execute(delete(Property))
            conn.execute(delete(DemoWorkspace))
            conn.execute(delete(Workspace))

    clean()
    yield
    clean()


def _seed_workspace_property(engine: Engine, *, slug: str) -> tuple[str, str]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    workspace_id = new_ulid()
    property_id = new_ulid()
    with factory() as session, tenant_agnostic():
        session.add(
            Workspace(
                id=workspace_id,
                slug=slug,
                name=f"Workspace {slug}",
                plan="free",
                quota_json={},
                settings_json={},
                created_at=_PINNED,
            )
        )
        session.flush()
        session.add(
            Property(
                id=property_id,
                address=f"{slug} Villa",
                timezone="Europe/Paris",
                tags_json=[],
                created_at=_PINNED,
            )
        )
        session.commit()
    return workspace_id, property_id


def _seed_demo_workspace(
    engine: Engine,
    *,
    workspace_id: str,
    expires_at: datetime,
) -> None:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    seeded_at = _PINNED - timedelta(days=1)
    with factory() as session, tenant_agnostic():
        session.add(
            DemoWorkspace(
                id=workspace_id,
                scenario_key="probe",
                seed_digest="x" * 64,
                created_at=seeded_at,
                last_activity_at=seeded_at,
                expires_at=expires_at,
                cookie_binding_digest="x" * 64,
            )
        )
        session.commit()


def _seed_occurrence(
    engine: Engine,
    *,
    workspace_id: str,
    property_id: str,
    state: str = "pending",
    ends_at: datetime | None = None,
) -> str:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    resolved_ends_at = (
        ends_at if ends_at is not None else _PINNED - timedelta(minutes=30)
    )
    occurrence_id = new_ulid()
    with factory() as session, tenant_agnostic():
        session.add(
            Occurrence(
                id=occurrence_id,
                workspace_id=workspace_id,
                schedule_id=None,
                template_id=None,
                property_id=property_id,
                assignee_user_id=None,
                starts_at=resolved_ends_at - timedelta(hours=1),
                ends_at=resolved_ends_at,
                scheduled_for_local="2026-04-19T10:00",
                originally_scheduled_for="2026-04-19T10:00",
                state=state,
                cancellation_reason=None,
                title="Pool clean",
                description_md="",
                priority="normal",
                photo_evidence="disabled",
                duration_minutes=60,
                area_id=None,
                unit_id=None,
                expected_role_id=None,
                linked_instruction_ids=[],
                inventory_consumption_json={},
                is_personal=False,
                created_by_user_id=None,
                created_at=_PINNED,
            )
        )
        session.commit()
    return occurrence_id


def _state_counts(engine: Engine, *, workspace_id: str) -> dict[str, int]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    counts: dict[str, int] = {}
    with factory() as session, tenant_agnostic():
        rows = session.scalars(
            select(Occurrence.state).where(Occurrence.workspace_id == workspace_id)
        ).all()
    for state in rows:
        counts[state] = counts.get(state, 0) + 1
    return counts


class TestOverdueFanOut:
    """Drive :func:`_make_overdue_fanout_body` against the real engine."""

    def test_per_workspace_attribution_in_log_payload(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_overdue_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        allow_propagated_log_capture("app.worker.scheduler")

        frozen = FrozenClock(_PINNED)
        ws_a, prop_a = _seed_workspace_property(engine, slug="ws-with-stuck-task")
        ws_b, _prop_b = _seed_workspace_property(engine, slug="ws-without-task")
        _seed_occurrence(engine, workspace_id=ws_a, property_id=prop_a)

        body = _make_overdue_fanout_body(frozen)
        with caplog.at_level(logging.INFO, logger="app.worker.scheduler"):
            body()

        ws_events: dict[str, logging.LogRecord] = {}
        for rec in caplog.records:
            if getattr(rec, "event", None) != "worker.overdue.workspace.tick":
                continue
            workspace_id = getattr(rec, "workspace_id", None)
            assert isinstance(workspace_id, str)
            ws_events[workspace_id] = rec
        assert set(ws_events.keys()) == {ws_a, ws_b}

        a_event = ws_events[ws_a]
        assert _log_payload(a_event) == {
            "event": "worker.overdue.workspace.tick",
            "workspace_id": ws_a,
            "workspace_slug": "ws-with-stuck-task",
            "flipped_count": 1,
            "skipped_already_overdue": 0,
            "skipped_manual_transition": 0,
        }

        b_event = ws_events[ws_b]
        assert _log_payload(b_event) == {
            "event": "worker.overdue.workspace.tick",
            "workspace_id": ws_b,
            "workspace_slug": "ws-without-task",
            "flipped_count": 0,
            "skipped_already_overdue": 0,
            "skipped_manual_transition": 0,
        }

        summary_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.overdue.tick.summary"
        ]
        assert len(summary_events) == 1
        summary = summary_events[0]
        assert summary.levelno == logging.INFO
        assert _log_payload(summary) == {
            "event": "worker.overdue.tick.summary",
            "total_workspaces": 2,
            "total_workspaces_skipped": 0,
            "total_workspaces_failed": 0,
            "total_flipped": 1,
            "total_skipped_manual_transition": 0,
        }

        assert _state_counts(engine, workspace_id=ws_a) == {"overdue": 1}
        assert _state_counts(engine, workspace_id=ws_b) == {}

    def test_broken_workspace_does_not_abort_tick(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_overdue_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        allow_propagated_log_capture("app.worker.scheduler")

        frozen = FrozenClock(_PINNED)
        ws_a, prop_a = _seed_workspace_property(engine, slug="healthy")
        ws_d, prop_d = _seed_workspace_property(engine, slug="broken")
        _seed_occurrence(engine, workspace_id=ws_a, property_id=prop_a)
        _seed_occurrence(engine, workspace_id=ws_d, property_id=prop_d)

        from app.worker.tasks import overdue as overdue_mod

        real_detect = overdue_mod.detect_overdue

        def poisoned_detect(
            ctx: WorkspaceContext,
            *,
            session: Session,
            now: datetime | None = None,
            clock: Clock | None = None,
            grace_minutes: int | None = None,
        ) -> OverdueReport:
            if ctx.workspace_id == ws_d:
                raise RuntimeError("poisoned for test")
            return real_detect(
                ctx,
                session=session,
                now=now,
                clock=clock,
                grace_minutes=grace_minutes,
            )

        monkeypatch.setattr("app.worker.tasks.overdue.detect_overdue", poisoned_detect)

        body = _make_overdue_fanout_body(frozen)
        with caplog.at_level(logging.INFO, logger="app.worker.scheduler"):
            body()

        failed_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.overdue.workspace.failed"
        ]
        assert len(failed_events) == 1
        failed = failed_events[0]
        assert failed.levelno == logging.WARNING
        assert _log_payload(failed) == {
            "event": "worker.overdue.workspace.failed",
            "workspace_id": ws_d,
            "workspace_slug": "broken",
            "error": "RuntimeError",
        }

        tick_workspace_ids = {
            getattr(rec, "workspace_id", None)
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.overdue.workspace.tick"
        }
        assert tick_workspace_ids == {ws_a}
        assert _state_counts(engine, workspace_id=ws_a) == {"overdue": 1}
        assert _state_counts(engine, workspace_id=ws_d) == {"pending": 1}

        summary_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.overdue.tick.summary"
        ]
        assert len(summary_events) == 1
        summary = summary_events[0]
        assert _log_payload(summary) == {
            "event": "worker.overdue.tick.summary",
            "total_workspaces": 2,
            "total_workspaces_skipped": 0,
            "total_workspaces_failed": 1,
            "total_flipped": 1,
            "total_skipped_manual_transition": 0,
        }


class TestDemoExpiredFilter:
    """The fan-out body skips demo workspaces past their TTL (§24)."""

    def test_expired_demo_workspace_is_skipped(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_overdue_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        allow_propagated_log_capture("app.worker.scheduler")

        frozen = FrozenClock(_PINNED)
        ws_live, prop_live = _seed_workspace_property(engine, slug="ws-live")
        ws_expired, prop_expired = _seed_workspace_property(
            engine, slug="ws-expired-demo"
        )
        _seed_occurrence(engine, workspace_id=ws_live, property_id=prop_live)
        _seed_occurrence(engine, workspace_id=ws_expired, property_id=prop_expired)
        _seed_demo_workspace(
            engine,
            workspace_id=ws_expired,
            expires_at=_PINNED - timedelta(hours=1),
        )

        body = _make_overdue_fanout_body(frozen)
        with caplog.at_level(logging.INFO, logger="app.worker.scheduler"):
            body()

        tick_workspace_ids = {
            getattr(rec, "workspace_id", None)
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.overdue.workspace.tick"
        }
        assert tick_workspace_ids == {ws_live}
        assert _state_counts(engine, workspace_id=ws_live) == {"overdue": 1}
        assert _state_counts(engine, workspace_id=ws_expired) == {"pending": 1}

        summary_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.overdue.tick.summary"
        ]
        assert len(summary_events) == 1
        summary = summary_events[0]
        assert _log_payload(summary) == {
            "event": "worker.overdue.tick.summary",
            "total_workspaces": 2,
            "total_workspaces_skipped": 1,
            "total_workspaces_failed": 0,
            "total_flipped": 1,
            "total_skipped_manual_transition": 0,
        }
