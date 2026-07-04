"""Integration tests for the DB-backed shared throttle window store.

The point of :class:`app.abuse.window_store.DbWindowStore` is that a
per-deployment cap holds across *every* worker, not per process. These
tests simulate multiple workers by pointing **two independent store
instances at one database** and asserting a bucket filled through
instance A is seen — and refused — through instance B.

The single-worker in-memory path is covered by
``tests/unit/abuse/test_throttle.py`` and the ``Throttle`` unit tests;
those stay green because ``MemoryWindowStore`` is unchanged behaviour.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.auth._throttle as throttle_module
from app.abuse.throttle import ShieldStore
from app.abuse.window_store import DbWindowStore, WindowCheck
from app.adapters.db.ports import DbSession
from app.auth._throttle import SignupRateLimited, Throttle

_PINNED = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)
_MINUTE = timedelta(seconds=60)


def _uow_factory(engine: Engine) -> Callable[[], AbstractContextManager[DbSession]]:
    """A UoW bound to ``engine`` — one per simulated worker."""
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


@pytest.fixture(autouse=True)
def _clear_throttle_window(engine: Engine) -> Iterator[None]:
    """Drop every throttle_window row before and after each case.

    The ``engine`` fixture is session-scoped, so rows would otherwise
    leak between cases in this file (and every case here re-uses a small
    set of scope/key pairs). A clean slate keeps each assertion honest.
    """
    DbWindowStore(_uow_factory(engine)).clear()
    yield
    DbWindowStore(_uow_factory(engine)).clear()


def _check(store: DbWindowStore, *, scope: str, key: str, limit: int) -> bool:
    """Return ``True`` when a single-bucket hit is recorded (under cap)."""
    rejection = store.check_and_record_all(
        [WindowCheck(scope=scope, key=key, limit=limit, window=_MINUTE)],
        now=_PINNED,
    )
    return rejection is None


def test_two_stores_share_one_global_bucket(engine: Engine) -> None:
    """Filling a bucket through store A is seen — and refused — via store B.

    This is the multi-worker fix in miniature: two ``DbWindowStore``
    objects (two "workers") pointed at the same DB share the count, so
    the deployment-global cap is enforced across them rather than
    multiplied.
    """
    store_a = DbWindowStore(_uow_factory(engine))
    store_b = DbWindowStore(_uow_factory(engine))
    scope, key, limit = "test:signup:global", "__global__", 3

    # Worker A records two hits; worker B records the third.
    assert _check(store_a, scope=scope, key=key, limit=limit) is True
    assert _check(store_a, scope=scope, key=key, limit=limit) is True
    assert _check(store_b, scope=scope, key=key, limit=limit) is True

    # The bucket is now full (3/3). Both workers must refuse the next
    # hit — B sees A's writes and vice versa.
    assert _check(store_b, scope=scope, key=key, limit=limit) is False
    assert _check(store_a, scope=scope, key=key, limit=limit) is False


def test_distinct_keys_do_not_bleed_across_stores(engine: Engine) -> None:
    """Per-IP means per-IP even across store instances."""
    store_a = DbWindowStore(_uow_factory(engine))
    store_b = DbWindowStore(_uow_factory(engine))
    scope, limit = "test:signup:ip", 2

    assert _check(store_a, scope=scope, key="ipA", limit=limit) is True
    assert _check(store_a, scope=scope, key="ipA", limit=limit) is True
    # ipA is full through A; B sees that…
    assert _check(store_b, scope=scope, key="ipA", limit=limit) is False
    # …but ipB is untouched.
    assert _check(store_b, scope=scope, key="ipB", limit=limit) is True


def test_shared_window_slides(engine: Engine) -> None:
    """Old hits evict individually; a later window reopens the budget."""
    store_a = DbWindowStore(_uow_factory(engine))
    store_b = DbWindowStore(_uow_factory(engine))
    scope, key, limit = "test:slide", "k", 2

    assert _check(store_a, scope=scope, key=key, limit=limit) is True
    assert _check(store_a, scope=scope, key=key, limit=limit) is True
    assert _check(store_b, scope=scope, key=key, limit=limit) is False

    # Past the window every earlier hit evicts, so a fresh burst fits —
    # proven through the *other* store instance.
    later = _PINNED + timedelta(seconds=61)
    rejection = store_b.check_and_record_all(
        [WindowCheck(scope=scope, key=key, limit=limit, window=_MINUTE)],
        now=later,
    )
    assert rejection is None


def test_refusal_reports_oldest_for_retry_after(engine: Engine) -> None:
    """A refusal carries the oldest live hit so callers derive Retry-After."""
    store = DbWindowStore(_uow_factory(engine))
    scope, key, limit = "test:retry", "k", 1

    assert _check(store, scope=scope, key=key, limit=limit) is True
    rejection = store.check_and_record_all(
        [WindowCheck(scope=scope, key=key, limit=limit, window=_MINUTE)],
        now=_PINNED + timedelta(seconds=10),
    )
    assert rejection is not None
    assert rejection.index == 0
    # Oldest hit was recorded at _PINNED; round-tripped through epoch
    # seconds it is equal to the original instant.
    assert rejection.oldest_in_window == _PINNED


def test_two_shield_stores_share_backend(engine: Engine) -> None:
    """``ShieldStore`` over a shared backend enforces the cap across workers."""
    shield_a = ShieldStore(store=DbWindowStore(_uow_factory(engine)))
    shield_b = ShieldStore(store=DbWindowStore(_uow_factory(engine)))

    def hit(shield: ShieldStore) -> bool:
        return shield.check_and_record(
            scope="test:login:begin", key="ip", limit=2, window=_MINUTE, now=_PINNED
        )

    assert hit(shield_a) is True
    assert hit(shield_b) is True
    # Cap of 2 reached across the two "workers" — both now refuse.
    assert hit(shield_a) is False
    assert hit(shield_b) is False


def test_two_throttles_share_signup_global_cap(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spec §15 deployment-global signup cap holds across two workers.

    ``check_signup_start`` reads ``_SIGNUP_GLOBAL_LIMIT`` at call time, so
    the tight patched value flows into the ``WindowCheck``. Each call uses
    a distinct IP + email so only the global bucket can trip.
    """
    monkeypatch.setattr(throttle_module, "_SIGNUP_GLOBAL_LIMIT", 3)
    throttle_a = Throttle(window_store=DbWindowStore(_uow_factory(engine)))
    throttle_b = Throttle(window_store=DbWindowStore(_uow_factory(engine)))

    for index in range(3):
        # Alternate workers to prove the shared global counter.
        throttle = throttle_a if index % 2 == 0 else throttle_b
        throttle.check_signup_start(
            ip_hash=f"ip{index}", email_hash=f"email{index}", now=_PINNED
        )

    # A fourth start from either worker trips the deployment-global cap.
    with pytest.raises(SignupRateLimited) as exc_info:
        throttle_b.check_signup_start(
            ip_hash="ip-fresh", email_hash="email-fresh", now=_PINNED
        )
    assert exc_info.value.scope == "global"
    assert exc_info.value.retry_after_seconds >= 1


def test_two_throttles_share_signup_email_cap(engine: Engine) -> None:
    """The per-email signup cap (3/hour) holds across two workers."""
    throttle_a = Throttle(window_store=DbWindowStore(_uow_factory(engine)))
    throttle_b = Throttle(window_store=DbWindowStore(_uow_factory(engine)))

    # Three starts for one email from distinct IPs (ip cap 5 not tripped).
    for index in range(3):
        throttle = throttle_a if index % 2 == 0 else throttle_b
        throttle.check_signup_start(
            ip_hash=f"ip{index}", email_hash="victim", now=_PINNED
        )

    with pytest.raises(SignupRateLimited) as exc_info:
        throttle_b.check_signup_start(
            ip_hash="ip-fresh", email_hash="victim", now=_PINNED
        )
    assert exc_info.value.scope == "email"


@pytest.mark.pg_only
def test_postgres_serializes_concurrent_workers(engine: Engine) -> None:
    """Concurrent workers on one Postgres DB never over-admit past the cap.

    Four "workers" (independent stores) race to record hits against one
    bucket under a cap of 2. The advisory lock must serialise the
    check-then-record so exactly two are admitted — never three or four.
    """
    scope, key, limit = "test:concurrent", "__global__", 2
    stores = [DbWindowStore(_uow_factory(engine)) for _ in range(4)]
    start = Barrier(4)

    def hit(store: DbWindowStore) -> bool:
        start.wait(timeout=5)
        return _check(store, scope=scope, key=key, limit=limit)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(hit, stores))

    assert sum(1 for allowed in results if allowed) == limit
    assert sum(1 for allowed in results if not allowed) == 4 - limit
