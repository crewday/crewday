"""Integration tests for the hourly ``rate_limit_gc`` scheduler job (cd-txrlz).

The sweep deletes stale rows from the two deployment-wide rate-limit
tables (``throttle_window`` from cd-0lnr9, ``rate_limit_bucket``
pre-existing) once they are older than
:data:`~app.worker.tasks.rate_limit_gc.RATE_LIMIT_GC_RETENTION_SECONDS`
so cold-key rows on the multi-worker Postgres backend can't grow
unbounded.

The load-bearing property this suite proves is the **safety** one: a
row still inside its window (fresh ``updated_at_epoch``) is NEVER
deleted. Deleting a live ``throttle_window`` row would reset that
IP/email's abuse counter and weaken a §15 control, so the test seeds a
stale row and a fresh row in each table and asserts only the stale rows
are swept.

See ``docs/specs/15-security-privacy.md`` §"Self-serve abuse
mitigations" and ``docs/specs/16-deployment-operations.md`` §"Worker
process".
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.ops.models import RateLimitBucket, ThrottleWindow, WorkerHeartbeat
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.worker.scheduler import (
    RATE_LIMIT_GC_JOB_ID,
    create_scheduler,
    register_jobs,
    wrap_job,
)
from app.worker.scheduler import (
    _make_rate_limit_gc_body as make_rate_limit_gc_body,
)
from app.worker.tasks.rate_limit_gc import (
    RATE_LIMIT_GC_RETENTION_SECONDS,
    sweep_stale_rate_limit_rows,
)

pytestmark = pytest.mark.integration


_NOW = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
# A retention window plus a minute past the cutoff — unambiguously stale.
_STALE_EPOCH = _NOW.timestamp() - RATE_LIMIT_GC_RETENTION_SECONDS - 60
# One minute inside the cutoff — a live bucket that must survive.
_FRESH_EPOCH = _NOW.timestamp() - RATE_LIMIT_GC_RETENTION_SECONDS + 60


@pytest.fixture
def real_make_uow(engine: Engine) -> Iterator[None]:
    """Redirect the process-wide default UoW to the integration engine.

    The sweep opens its own UoW via
    :func:`app.adapters.db.session.make_uow`; mirrors the redirect
    ``tests/integration/worker/test_idempotency_sweep.py`` uses.
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
def clean_tables(engine: Engine) -> Iterator[None]:
    """Empty the swept tables + heartbeat before and after each test."""
    tables = (ThrottleWindow, RateLimitBucket, WorkerHeartbeat)
    with engine.begin() as conn:
        for table in tables:
            conn.execute(delete(table))
    yield
    with engine.begin() as conn:
        for table in tables:
            conn.execute(delete(table))


def _seed(engine: Engine) -> None:
    """Seed one stale + one fresh row in each swept table."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        session.add_all(
            [
                ThrottleWindow(
                    scope="signup_start:ip",
                    bucket_key="stale-key",
                    hits_json=[_STALE_EPOCH],
                    updated_at_epoch=_STALE_EPOCH,
                ),
                ThrottleWindow(
                    scope="signup_start:ip",
                    bucket_key="fresh-key",
                    hits_json=[_FRESH_EPOCH],
                    updated_at_epoch=_FRESH_EPOCH,
                ),
                RateLimitBucket(
                    bucket_key="stale-bucket",
                    tokens=0.0,
                    updated_at_epoch=_STALE_EPOCH,
                ),
                RateLimitBucket(
                    bucket_key="fresh-bucket",
                    tokens=1.0,
                    updated_at_epoch=_FRESH_EPOCH,
                ),
            ]
        )
        session.commit()


def _throttle_keys(engine: Engine) -> set[str]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        return set(session.scalars(select(ThrottleWindow.bucket_key)))


def _bucket_keys(engine: Engine) -> set[str]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        return set(session.scalars(select(RateLimitBucket.bucket_key)))


def test_registered_with_hourly_interval_trigger() -> None:
    """Sweep lands under ``RATE_LIMIT_GC_JOB_ID`` on an hourly interval."""
    scheduler = create_scheduler()
    register_jobs(scheduler)
    job = scheduler.get_job(RATE_LIMIT_GC_JOB_ID)
    assert job is not None, "rate_limit_gc job not registered"
    assert job.max_instances == 1
    assert job.coalesce is True


def test_sweep_deletes_stale_keeps_fresh(
    engine: Engine,
    real_make_uow: None,
    clean_tables: None,
) -> None:
    """Only the stale rows are deleted; the within-window rows survive.

    The safety property: a fresh ``throttle_window`` row must never be
    dropped (that would reset a live abuse counter), and a fresh
    ``rate_limit_bucket`` row must never be dropped (that would refund a
    drained token bucket).
    """
    _seed(engine)
    clock = FrozenClock(_NOW)

    report = sweep_stale_rate_limit_rows(clock=clock)

    assert report.throttle_window_deleted == 1
    assert report.rate_limit_bucket_deleted == 1
    assert _throttle_keys(engine) == {"fresh-key"}
    assert _bucket_keys(engine) == {"fresh-bucket"}


def test_wrapped_tick_sweeps_and_heartbeats(
    engine: Engine,
    real_make_uow: None,
    clean_tables: None,
) -> None:
    """The body runs through ``wrap_job``: stale rows gone, heartbeat set."""
    _seed(engine)
    clock = FrozenClock(_NOW)

    wrapped = wrap_job(
        make_rate_limit_gc_body(clock),
        job_id=RATE_LIMIT_GC_JOB_ID,
        clock=clock,
    )
    asyncio.run(wrapped())

    assert _throttle_keys(engine) == {"fresh-key"}
    assert _bucket_keys(engine) == {"fresh-bucket"}

    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        heartbeat = session.scalars(
            select(WorkerHeartbeat).where(
                WorkerHeartbeat.worker_name == RATE_LIMIT_GC_JOB_ID
            )
        ).first()
    assert heartbeat is not None, "heartbeat row not written"


def test_empty_tables_is_a_noop(
    engine: Engine,
    real_make_uow: None,
    clean_tables: None,
) -> None:
    """An empty sweep deletes nothing and returns zero counts."""
    report = sweep_stale_rate_limit_rows(clock=FrozenClock(_NOW))
    assert report.throttle_window_deleted == 0
    assert report.rate_limit_bucket_deleted == 0
