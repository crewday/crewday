"""Integration tests for the iCal poller fan-out (cd-d48).

End-to-end proof that
:func:`app.worker.jobs.stays._make_poll_ical_fanout_body` walks every
:class:`~app.adapters.db.workspace.models.Workspace` row, binds a
per-workspace system-actor context, dispatches
:func:`app.worker.tasks.poll_ical.poll_ical` inside a SAVEPOINT, and —
critically — isolates a single broken workspace: one workspace raising
must be attributed in the per-workspace ``worker.poll_ical.workspace.failed``
log without aborting the tick for its siblings.

Mirrors :mod:`tests.integration.worker.test_overdue_fanout` for the
poll_ical path. ``poll_ical`` itself is stubbed here — the target under
test is the fan-out orchestration wrapper (enumerate → bind ctx →
per-workspace try/except → structured logging), not the poller's ICS
parsing, which :mod:`tests.unit.worker.test_poll_ical` covers.

See ``docs/specs/04-properties-and-stays.md`` §"iCal feed" §"Polling
behavior" and ``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, delete
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.demo.models import DemoWorkspace
from app.adapters.db.workspace.models import Workspace
from app.config import Settings
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import Clock, FrozenClock
from app.util.ulid import new_ulid
from app.worker.jobs.stays import _make_poll_ical_fanout_body
from app.worker.tasks.poll_ical import PollReport

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
_ROOT_KEY = "poll-ical-fanout-cd-d48-test-root-key+pad-to-32"
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


def _ok_report() -> PollReport:
    """A zero-count clean :class:`PollReport` for the healthy workspace."""
    return PollReport(
        feeds_walked=0,
        feeds_polled=0,
        feeds_not_modified=0,
        feeds_rate_limited=0,
        feeds_errored=0,
        feeds_skipped=0,
        reservations_created=0,
        reservations_updated=0,
        reservations_cancelled=0,
        closures_created=0,
        tick_started_at=_PINNED,
        tick_ended_at=_PINNED,
    )


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
def clean_workspace_tables(engine: Engine) -> Iterator[None]:
    def clean() -> None:
        with engine.begin() as conn:
            conn.execute(delete(AuditLog))
            conn.execute(delete(DemoWorkspace))
            conn.execute(delete(Workspace))

    clean()
    yield
    clean()


@pytest.fixture
def pinned_settings() -> Settings:
    # ``root_key`` is required so the fan-out body does not skip the tick
    # at WARN (``worker.poll_ical.skipped_no_root_key``); the envelope it
    # builds is never exercised because ``poll_ical`` is stubbed.
    return Settings.model_construct(
        root_key=SecretStr(_ROOT_KEY),
        ical_allow_private_addresses=False,
    )


def _seed_workspace(engine: Engine, *, slug: str) -> str:
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
                settings_json={},
                created_at=_PINNED,
            )
        )
        session.commit()
    return workspace_id


class TestPollIcalFanOut:
    """Drive :func:`_make_poll_ical_fanout_body` against the real engine."""

    def test_broken_workspace_does_not_abort_tick(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_workspace_tables: None,
        pinned_settings: Settings,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        allow_propagated_log_capture("app.worker.scheduler")
        monkeypatch.setattr("app.config.get_settings", lambda: pinned_settings)

        # Seed the broken workspace FIRST so the fan-out walks it
        # BEFORE the healthy one: the enumeration is
        # ``select(Workspace.id, ...)`` with no ORDER BY, so on the
        # SQLite gate rows come back in insertion (rowid) order. Putting
        # the healthy workspace BEHIND the broken one is what gives the
        # continuation assertion below teeth — a
        # ``break``-instead-of-``continue`` (or a re-raise) regression
        # would skip the sibling that follows the failure, collapsing
        # ``polled`` to ``{ws_broken}`` alone.
        ws_broken = _seed_workspace(engine, slug="broken")
        ws_healthy = _seed_workspace(engine, slug="healthy")

        polled_workspace_ids: list[str] = []

        def fake_poll_ical(
            ctx: WorkspaceContext,
            *,
            session: Session,
            envelope: object,
            now: datetime,
            clock: Clock,
            allow_private_addresses: bool,
            allow_self_signed_resolver: Callable[..., bool],
        ) -> PollReport:
            polled_workspace_ids.append(ctx.workspace_id)
            if ctx.workspace_id == ws_broken:
                raise RuntimeError("poisoned for test")
            return _ok_report()

        monkeypatch.setattr("app.worker.tasks.poll_ical.poll_ical", fake_poll_ical)

        body = _make_poll_ical_fanout_body(FrozenClock(_PINNED))
        with caplog.at_level(logging.INFO, logger="app.worker.scheduler"):
            body()

        # Both workspaces were attempted — the broken one (walked
        # first) did NOT short-circuit the healthy one BEHIND it in the
        # walk. A ``break`` or a re-raise would leave ``{ws_broken}``.
        assert set(polled_workspace_ids) == {ws_healthy, ws_broken}

        failed_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.poll_ical.workspace.failed"
        ]
        assert len(failed_events) == 1
        failed = failed_events[0]
        assert failed.levelno == logging.WARNING
        assert _log_payload(failed) == {
            "event": "worker.poll_ical.workspace.failed",
            "workspace_id": ws_broken,
            "workspace_slug": "broken",
            "error": "RuntimeError",
        }

        # The healthy workspace still emitted its per-workspace tick —
        # the failure was attributed per-workspace, not fatal to the fan-out.
        tick_workspace_ids = {
            getattr(rec, "workspace_id", None)
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.poll_ical.workspace.tick"
        }
        assert tick_workspace_ids == {ws_healthy}

        summary_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.poll_ical.tick.summary"
        ]
        assert len(summary_events) == 1
        summary = summary_events[0]
        assert summary.levelno == logging.INFO
        payload = _log_payload(summary)
        assert payload["total_workspaces"] == 2
        assert payload["total_workspaces_skipped"] == 0
        assert payload["total_workspaces_failed"] == 1
