"""Sliding-window hit store seam for deployment-wide abuse throttles.

The abuse throttles (:class:`app.abuse.throttle.ShieldStore` and
:class:`app.auth._throttle.Throttle`) count hits inside a rolling window
per ``(scope, key)`` bucket — per-IP, per-email, and deployment-global
keys. This module owns *where* those hit lists live so a single seam can
serve both single-worker self-host and multi-worker deployments:

* :class:`MemoryWindowStore` — the default. A process-local
  :class:`dict` of :class:`~collections.deque` timestamps guarded by a
  :class:`threading.Lock`. Dependency-free, no DB round-trip on the hot
  path. Correct for single-worker self-host (§01 "One worker pool per
  process"), where the process *is* the deployment.

* :class:`DbWindowStore` — the shared backend. Buckets live in the
  deployment-wide ``throttle_window`` table so every worker/replica
  counts against the *same* window. Without it, ``N`` workers each keep
  their own window and the spec §15 per-deployment caps (e.g. ≤ 200
  signup starts / deployment / hour) are effectively multiplied by
  ``N``. Selected when ``settings.rate_limit_backend == "postgres"`` —
  the same flag that already routes the §12 API rate limiter onto its
  shared ``rate_limit_bucket`` table.

**Race-safety.** :class:`DbWindowStore` mirrors
:class:`app.api.middleware.rate_limit.PersistentRateLimitBackend`: a
``pg_advisory_xact_lock`` serialises every check-then-record for a bucket
key across concurrent Postgres workers, so two workers can never both
observe "under the cap" and both record. On SQLite the advisory lock is a
no-op — SQLite is the single-worker self-host backend, so there is no
cross-process race to guard, and the store is only exercised on SQLite by
tests, which drive it sequentially.

**Semantics are identical to the in-memory deque.** A bucket is a rolling
window: a hit at ``t`` counts until ``t + window``; the check evicts hits
older than ``now - window`` before counting; a refused hit is *not*
recorded (a refusal must not push the window boundary forward). When a
call spans several buckets (signup evaluates global + per-IP + per-email
together), the whole batch is all-or-nothing: the first bucket over its
cap raises with nothing recorded, otherwise every bucket records one hit.

See ``docs/specs/15-security-privacy.md`` §"Self-serve abuse mitigations"
and §"Self-service lost-device & email-change abuse mitigations".
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session as SaSession

from app.adapters.db.ops.models import ThrottleWindow
from app.adapters.db.ports import DbSession as DbSessionPort
from app.adapters.db.session import make_uow
from app.config import Settings
from app.tenancy.current import tenant_agnostic

__all__ = [
    "DbWindowStore",
    "MemoryWindowStore",
    "WindowCheck",
    "WindowRejection",
    "WindowStore",
    "build_window_store",
]


@dataclass(frozen=True, slots=True)
class WindowCheck:
    """One ``(scope, key)`` bucket to evaluate against ``limit`` per ``window``.

    A single throttle decision may carry several checks (signup weighs
    global + per-IP + per-email); the store evaluates them in the order
    given so the caller learns which cap tripped first.
    """

    scope: str
    key: str
    limit: int
    window: timedelta


@dataclass(frozen=True, slots=True)
class WindowRejection:
    """The first over-cap bucket in a batch, with its oldest live hit.

    ``index`` is the position in the ``checks`` sequence that tripped —
    callers map it back to a scope name. ``oldest_in_window`` is the
    oldest hit still inside that bucket's window, so a caller can derive
    an exact ``Retry-After`` (``oldest + window - now``).
    """

    index: int
    oldest_in_window: datetime


class WindowStore(Protocol):
    """Backend that counts and records rolling-window hits per bucket."""

    def check_and_record_all(
        self, checks: Sequence[WindowCheck], *, now: datetime
    ) -> WindowRejection | None:
        """Record one hit in every bucket, or refuse the whole batch.

        Returns ``None`` when every check is under its cap (a hit is
        recorded in each). Returns the first :class:`WindowRejection`
        when any bucket is at or over its cap — in which case **nothing**
        is recorded.
        """
        ...

    def clear(self) -> None:
        """Drop every bucket. Test-only reset; production never calls it."""
        ...


class MemoryWindowStore:
    """Process-local rolling-window buckets for single-worker self-host.

    A :class:`threading.Lock` guards every mutation; critical sections
    are a deque append plus a left-side trim — microseconds, no I/O — so
    the lock is uncontended at the deployment sizes this backend serves.
    """

    __slots__ = ("_hits", "_lock")

    def __init__(self) -> None:
        self._lock = Lock()
        self._hits: dict[tuple[str, str], deque[datetime]] = defaultdict(deque)

    def check_and_record_all(
        self, checks: Sequence[WindowCheck], *, now: datetime
    ) -> WindowRejection | None:
        with self._lock:
            # Evict each touched bucket first so the count reflects the
            # live window before any refusal decision.
            for check in checks:
                bucket = self._hits[(check.scope, check.key)]
                cutoff = now - check.window
                while bucket and bucket[0] < cutoff:
                    bucket.popleft()
            for index, check in enumerate(checks):
                bucket = self._hits[(check.scope, check.key)]
                if len(bucket) >= check.limit:
                    return WindowRejection(index=index, oldest_in_window=bucket[0])
            for check in checks:
                self._hits[(check.scope, check.key)].append(now)
            return None

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


class DbWindowStore:
    """Deployment-wide rolling-window buckets shared through ``throttle_window``.

    Every check-then-record runs inside one Unit of Work: the bucket keys
    are advisory-locked (Postgres) so concurrent workers serialise, the
    live hit lists are read, the batch is decided, and — only when every
    bucket is under its cap — each bucket's row is upserted with the new
    hit appended. Refusals write nothing.
    """

    __slots__ = ("_uow_factory",)

    def __init__(
        self,
        uow_factory: Callable[[], AbstractContextManager[DbSessionPort]] = make_uow,
    ) -> None:
        self._uow_factory = uow_factory

    def check_and_record_all(
        self, checks: Sequence[WindowCheck], *, now: datetime
    ) -> WindowRejection | None:
        if not checks:
            return None
        now_epoch = now.timestamp()
        with self._uow_factory() as db_session:
            assert isinstance(db_session, SaSession)
            # justification: throttle_window is deployment-wide (no
            # workspace_id column); buckets are keyed by (scope,
            # bucket_key), not tenant.
            with tenant_agnostic():
                # Lock distinct keys in a stable order so two batches that
                # overlap on some keys can never deadlock.
                for scope, key in sorted({(c.scope, c.key) for c in checks}):
                    _lock_bucket(db_session, scope=scope, key=key)

                rows: dict[tuple[str, str], ThrottleWindow | None] = {}
                live: dict[tuple[str, str], list[float]] = {}
                for check in checks:
                    sk = (check.scope, check.key)
                    if sk in live:
                        continue
                    row = db_session.get(ThrottleWindow, sk)
                    rows[sk] = row
                    cutoff = (now - check.window).timestamp()
                    existing: list[float] = (
                        list(row.hits_json) if row is not None else []
                    )
                    live[sk] = [hit for hit in existing if hit >= cutoff]

                for index, check in enumerate(checks):
                    bucket = live[(check.scope, check.key)]
                    if len(bucket) >= check.limit:
                        oldest = datetime.fromtimestamp(min(bucket), tz=UTC)
                        return WindowRejection(index=index, oldest_in_window=oldest)

                for check in checks:
                    sk = (check.scope, check.key)
                    bucket = live[sk]
                    bucket.append(now_epoch)
                    row = rows[sk]
                    if row is None:
                        db_session.add(
                            ThrottleWindow(
                                scope=sk[0],
                                bucket_key=sk[1],
                                hits_json=bucket,
                                updated_at_epoch=now_epoch,
                            )
                        )
                    else:
                        row.hits_json = bucket
                        row.updated_at_epoch = now_epoch
                    db_session.flush()
        return None

    def clear(self) -> None:
        with self._uow_factory() as db_session:
            assert isinstance(db_session, SaSession)
            # justification: throttle_window is deployment-wide; test-only
            # reset with no tenant scoping to honour.
            with tenant_agnostic():
                db_session.query(ThrottleWindow).delete()


def _lock_bucket(db_session: SaSession, *, scope: str, key: str) -> None:
    """Take a Postgres transaction advisory lock for one bucket key.

    No-op on SQLite: that backend is single-worker self-host, so there is
    no cross-process race to serialise. Mirrors
    :func:`app.api.middleware.rate_limit._lock_bucket`.
    """
    if db_session.bind is None or db_session.bind.dialect.name != "postgresql":
        return
    # ``\x1f`` (unit separator) joins the two parts into one lock key.
    # A hash collision only serialises two unrelated buckets — coarser
    # than needed, never incorrect — but NUL (``\x00``) is rejected by
    # Postgres text, so the separator must be a non-NUL byte.
    db_session.execute(
        text("SELECT pg_advisory_xact_lock(CAST(hashtext(:k) AS bigint))"),
        {"k": f"{scope}\x1f{key}"},
    )


def build_window_store(settings: Settings) -> WindowStore:
    """Return the shared DB store when multi-worker, else the in-memory store.

    Driven by ``settings.rate_limit_backend`` — the same flag that routes
    the §12 API rate limiter. ``"postgres"`` shares buckets across workers
    via ``throttle_window``; ``"memory"`` (the self-host default) keeps
    every bucket in process memory with no DB round-trip.
    """
    if settings.rate_limit_backend == "postgres":
        return DbWindowStore()
    return MemoryWindowStore()
