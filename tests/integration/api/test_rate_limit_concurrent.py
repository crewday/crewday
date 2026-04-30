"""Integration tests for the persisted API rate-limit backend."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from threading import Barrier

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.ops.models import RateLimitBucket
from app.adapters.db.ports import DbSession
from app.api.middleware.rate_limit import (
    PersistentRateLimitBackend,
    RateLimitDecision,
)


class FrozenRateLimitClock:
    def __init__(self, *, now: float = 1_000.0) -> None:
        self._now = now

    def monotonic(self) -> float:
        return self._now

    def time(self) -> float:
        return self._now


def _uow_factory(engine: Engine) -> Callable[[], AbstractContextManager[DbSession]]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    @contextmanager
    def _uow() -> Iterator[DbSession]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _uow


def test_persistent_backend_consumes_and_persists_bucket(engine: Engine) -> None:
    backend = PersistentRateLimitBackend(_uow_factory(engine))
    clock = FrozenRateLimitClock(now=10_000.0)
    bucket_key = "test:rate-limit:persisted"

    first = backend.consume(
        bucket_key=bucket_key,
        limit_per_minute=2,
        clock=clock,
    )
    second = backend.consume(
        bucket_key=bucket_key,
        limit_per_minute=2,
        clock=clock,
    )
    third = backend.consume(
        bucket_key=bucket_key,
        limit_per_minute=2,
        clock=clock,
    )

    assert first.allowed
    assert first.remaining == 1
    assert second.allowed
    assert second.remaining == 0
    assert not third.allowed
    assert third.retry_after_seconds == 30

    with sessionmaker(bind=engine, expire_on_commit=False, class_=Session)() as session:
        row = session.scalars(
            select(RateLimitBucket).where(RateLimitBucket.bucket_key == bucket_key)
        ).one()
    assert row.tokens == 0.0
    assert row.updated_at_epoch == 10_000.0


@pytest.mark.pg_only
def test_postgres_backend_serializes_four_concurrent_workers(engine: Engine) -> None:
    backend = PersistentRateLimitBackend(_uow_factory(engine))
    clock = FrozenRateLimitClock(now=20_000.0)
    bucket_key = "test:rate-limit:postgres-concurrent"
    start = Barrier(4)

    def hit() -> RateLimitDecision:
        start.wait(timeout=5)
        return backend.consume(
            bucket_key=bucket_key,
            limit_per_minute=2,
            clock=clock,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: hit(), range(4)))

    assert sum(1 for result in results if result.allowed) == 2
    assert sum(1 for result in results if not result.allowed) == 2
    assert sorted(result.remaining for result in results if result.allowed) == [0, 1]
    assert {result.retry_after_seconds for result in results if not result.allowed} == {
        30
    }
