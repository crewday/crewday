"""Integration tests for the user_workspace derive-refresh fan-out (cd-yqm4).

End-to-end proof that
:func:`~app.worker.scheduler._make_user_workspace_refresh_body` opens
its own UoW, calls :func:`reconcile_user_workspace`, and surfaces the
per-tick aggregate counts in the
``worker.identity.user_workspace.tick.summary`` log record.

Unit coverage for the registration shape (the tick is registered under
:data:`USER_WORKSPACE_REFRESH_JOB_ID` with a 5 min
:class:`~apscheduler.triggers.interval.IntervalTrigger`) lives in
``tests/unit/worker/test_scheduler.py``. This suite covers what that
layer cannot:

* Add cycle — a fresh role_grant materialises ``user_workspace`` after
  one tick.
* Revoke cycle — a deleted role_grant drops the ``user_workspace`` row
  after one tick.
* Tick-summary log shape — counts match the cd-yqm4 acceptance.

See ``docs/specs/02-domain-model.md`` §"user_workspace",
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.tenancy import tenant_agnostic
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.scheduler import _make_user_workspace_refresh_body

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_tenancy_context() -> Iterator[None]:
    """Every test starts without an active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture
def real_make_uow(engine: Engine) -> Iterator[None]:
    """Redirect the process-wide default UoW to the integration engine.

    Mirrors the sibling fan-out suites — the body opens its own UoW
    via :func:`app.adapters.db.session.make_uow`, so we point the
    process-wide default at the integration engine.
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


@dataclass(frozen=True, slots=True)
class Counts:
    """Pre-test baseline of the rows the tick summary aggregates."""

    user_workspace: int


@pytest.fixture
def baseline_counts(engine: Engine) -> Counts:
    """Capture pre-test row counts for the upstream + junction tables.

    The harness engine is session-scoped (sibling integration tests
    leave rows behind in ``user``, ``workspace``, ``role_grant``, etc.,
    and a global wipe would have to chase every CASCADE / SET-NULL FK
    to avoid CHECK-constraint violations from partial deletes — see
    the ``api_token`` mutual-exclusion CHECK as one example).

    Instead of wiping, we record the baseline counts here and the
    tests assert *deltas* after the worker tick — the only invariant
    the cd-yqm4 acceptance pins ("a fresh role_grant materialises the
    derived row on the next tick" etc.) is local to the rows the test
    seeded, so the absolute count of every other table is irrelevant.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session, tenant_agnostic():
        return Counts(
            user_workspace=session.scalar(
                select(func.count()).select_from(UserWorkspace)
            )
            or 0,
        )


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _seed_workspace(engine: Engine, *, slug: str) -> str:
    """Insert one :class:`Workspace` and return its id."""
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


def _seed_user(engine: Engine, *, email: str) -> str:
    """Insert one :class:`User` and return its id."""
    from app.adapters.db.identity.models import canonicalise_email

    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    user_id = new_ulid()
    with factory() as session, tenant_agnostic():
        session.add(
            User(
                id=user_id,
                email=email,
                email_lower=canonicalise_email(email),
                display_name=email.split("@", 1)[0],
                created_at=_PINNED,
            )
        )
        session.commit()
    return user_id


def _seed_workspace_grant(engine: Engine, *, user_id: str, workspace_id: str) -> str:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    grant_id = new_ulid()
    with factory() as session, tenant_agnostic():
        session.add(
            RoleGrant(
                id=grant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                grant_role="manager",
                scope_kind="workspace",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        session.commit()
    return grant_id


def _delete_grant(engine: Engine, *, grant_id: str) -> None:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session, tenant_agnostic():
        session.execute(delete(RoleGrant).where(RoleGrant.id == grant_id))
        session.commit()


def _user_workspace(
    engine: Engine, *, user_id: str, workspace_id: str
) -> UserWorkspace | None:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session, tenant_agnostic():
        return session.scalar(
            select(UserWorkspace)
            .where(UserWorkspace.user_id == user_id)
            .where(UserWorkspace.workspace_id == workspace_id)
        )


# ---------------------------------------------------------------------------
# Add / revoke cycle through the worker body
# ---------------------------------------------------------------------------


class TestUserWorkspaceRefreshFanOut:
    """Drive :func:`_make_user_workspace_refresh_body` against the real engine.

    The harness engine is session-scoped — sibling integration suites
    leave rows behind in ``user``, ``workspace``, ``user_workspace``,
    ``role_grant``, etc. Wiping those tables before every test would
    have to chase every CASCADE / SET-NULL FK to avoid CHECK-constraint
    violations from partial deletes (the ``api_token`` mutual-exclusion
    CHECK is one such trap), so the tests instead seed unique-id rows
    and assert *deltas* against the pre-tick baseline.
    """

    def test_add_cycle_through_worker_body(
        self,
        engine: Engine,
        real_make_uow: None,
        baseline_counts: Counts,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """A fresh role_grant materialises ``user_workspace`` after one tick.

        The cd-yqm4 add-cycle acceptance: write the upstream, run the
        worker body once, observe the row.
        """
        allow_propagated_log_capture("app.worker.scheduler")

        ws_id = _seed_workspace(engine, slug=f"ws-add-{new_ulid()[:8]}")
        user_id = _seed_user(engine, email=f"add-{new_ulid()[:8]}@example.com")
        _seed_workspace_grant(engine, user_id=user_id, workspace_id=ws_id)

        # Pre-tick: no membership row for the freshly-seeded pair.
        assert _user_workspace(engine, user_id=user_id, workspace_id=ws_id) is None

        body = _make_user_workspace_refresh_body(FrozenClock(_PINNED))
        with caplog.at_level(logging.INFO, logger="app.worker.scheduler"):
            body()

        # Post-tick: the derived junction shows the membership.
        row = _user_workspace(engine, user_id=user_id, workspace_id=ws_id)
        assert row is not None
        assert row.source == "workspace_grant"

        # The tick-summary log fires once and records at least the one
        # row this test seeded. Asserting a strict equality on
        # ``rows_inserted`` would couple the test to whatever sibling
        # state happens to be in the harness DB — instead we read the
        # pre-tick baseline and assert deltas.
        summaries = [
            r
            for r in caplog.records
            if getattr(r, "event", None)
            == "worker.identity.user_workspace.tick.summary"
        ]
        assert len(summaries) == 1
        # ``getattr(..., None)`` keeps mypy happy on ``LogRecord`` —
        # the structured-log ``extra`` fields are dynamic and not
        # part of the stdlib stub.
        rows_inserted = getattr(summaries[0], "rows_inserted", None)
        assert isinstance(rows_inserted, int)
        assert rows_inserted >= 1
        # The post-tick total must be exactly baseline + rows_inserted -
        # rows_deleted, so a regression that double-counts surfaces.
        post_total = _count_user_workspace(engine)
        rows_deleted = getattr(summaries[0], "rows_deleted", None)
        assert isinstance(rows_deleted, int)
        assert (
            post_total == baseline_counts.user_workspace + rows_inserted - rows_deleted
        )

    def test_revoke_cycle_through_worker_body(
        self,
        engine: Engine,
        real_make_uow: None,
        baseline_counts: Counts,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """A revoked role_grant drops ``user_workspace`` after one tick.

        Cd-yqm4 revoke-cycle acceptance: kill the upstream, run the
        worker body once, observe the row gone.
        """
        allow_propagated_log_capture("app.worker.scheduler")

        ws_id = _seed_workspace(engine, slug=f"ws-revoke-{new_ulid()[:8]}")
        user_id = _seed_user(engine, email=f"revoke-{new_ulid()[:8]}@example.com")
        grant_id = _seed_workspace_grant(engine, user_id=user_id, workspace_id=ws_id)

        # Tick #1 — materialise the row.
        body = _make_user_workspace_refresh_body(FrozenClock(_PINNED))
        body()
        assert _user_workspace(engine, user_id=user_id, workspace_id=ws_id) is not None
        post_tick1_total = _count_user_workspace(engine)

        # Revoke the upstream.
        _delete_grant(engine, grant_id=grant_id)

        # Tick #2 — the orphaned row drops.
        with caplog.at_level(logging.INFO, logger="app.worker.scheduler"):
            body()

        assert _user_workspace(engine, user_id=user_id, workspace_id=ws_id) is None

        # The tick-summary log records the deletion. caplog accumulates
        # across both ticks — pick the second.
        summaries = [
            r
            for r in caplog.records
            if getattr(r, "event", None)
            == "worker.identity.user_workspace.tick.summary"
        ]
        assert len(summaries) >= 1
        last = summaries[-1]
        rows_deleted = getattr(last, "rows_deleted", None)
        assert isinstance(rows_deleted, int)
        assert rows_deleted >= 1
        # Total must drop by exactly ``rows_deleted - rows_inserted``
        # (none on the revoke-only tick) so the audit-balance holds.
        rows_inserted = getattr(last, "rows_inserted", None)
        assert isinstance(rows_inserted, int)
        post_tick2_total = _count_user_workspace(engine)
        assert post_tick2_total == post_tick1_total + rows_inserted - rows_deleted
        # Suppress unused-baseline lint — the fixture stays in the
        # signature so a future regression that shifts the assertion
        # to the absolute count has a single place to read from.
        del baseline_counts

    def test_tick_summary_emits_per_tick(
        self,
        engine: Engine,
        real_make_uow: None,
        baseline_counts: Counts,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """The summary log fires exactly once per tick, regardless of churn.

        Operator dashboards plot the summary as a continuous line; a
        regression that gates the emit on "did anything change" would
        leave gaps. Asserting one record per tick keeps the contract
        explicit even when the tick is a no-op against the test's
        own seeded state.
        """
        allow_propagated_log_capture("app.worker.scheduler")

        body = _make_user_workspace_refresh_body(FrozenClock(_PINNED))
        with caplog.at_level(logging.INFO, logger="app.worker.scheduler"):
            body()

        summaries = [
            r
            for r in caplog.records
            if getattr(r, "event", None)
            == "worker.identity.user_workspace.tick.summary"
        ]
        assert len(summaries) == 1
        # Every count field is present and non-negative.
        for field in (
            "rows_inserted",
            "rows_deleted",
            "rows_source_flipped",
            "upstream_pairs_seen",
        ):
            value = getattr(summaries[0], field, None)
            assert isinstance(value, int) and value >= 0
        # The baseline fixture keeps the count read warm for a future
        # regression that tightens this assertion to a delta on the
        # whole junction; for now the contract is "something fired".
        del baseline_counts


def _count_user_workspace(engine: Engine) -> int:
    """Return the number of ``user_workspace`` rows in the engine.

    Used by the delta-based assertions so a "rows_inserted=N"
    summary stays consistent with the post-tick total without the
    test having to enumerate every row by id.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session, tenant_agnostic():
        return session.scalar(select(func.count()).select_from(UserWorkspace)) or 0
