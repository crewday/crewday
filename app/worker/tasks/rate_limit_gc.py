"""``sweep_stale_rate_limit_rows`` — GC for the DB-backed rate-limit tables.

The two deployment-wide rate-limit tables accumulate one row per cold
key and never self-clean:

* ``throttle_window`` (:class:`app.abuse.window_store.DbWindowStore`) —
  a sliding-window hit list per ``(scope, bucket_key)``. A *touched*
  key self-evicts expired hits on its next check, but a key that goes
  cold leaves its row behind forever.
* ``rate_limit_bucket``
  (:class:`app.api.middleware.rate_limit.PersistentRateLimitBackend`) —
  a token-bucket balance per key. Same pattern: a cold key's row is
  never revisited.

Both only exist on the multi-worker Postgres backend
(``settings.rate_limit_backend == "postgres"``); single-worker self-host
keeps its buckets in process memory and never writes these tables. The
rows are tiny, so this is a low-urgency hygiene sweep — but unbounded
growth should still be bounded.

**Why a plain DELETE is safe.** Both tables carry ``updated_at_epoch``
(Unix epoch seconds of the last write). A row is only ever safe to
delete once it *cannot* affect a live limit decision:

* ``throttle_window``: every hit in ``hits_json`` was appended at or
  before ``updated_at_epoch``. On the next check, hits older than
  ``now - window`` are evicted. The longest window any caller uses is
  :data:`RATE_LIMIT_GC_MAX_WINDOW_SECONDS` (the §15 signup / recovery
  hour). So once ``updated_at_epoch < now - MAX_WINDOW`` every hit in
  the row would be evicted on the next touch — the row already
  contributes zero to any live count, and deleting it is behaviourally
  identical to leaving an all-evicted (empty) row.
* ``rate_limit_bucket``: the token bucket refills at ``capacity / 60``
  per second, so any bucket fully refills to ``capacity`` after 60 s of
  no traffic regardless of its stored balance. A row with
  ``updated_at_epoch < now - 60`` yields the exact same decision whether
  it is present (evaluated back to full) or absent (recreated fresh at
  full). ``MAX_WINDOW`` (3600 s) dominates that 60 s, so the same cutoff
  is trivially safe here too.

We delete with a safety margin of :data:`RATE_LIMIT_GC_RETENTION_SECONDS`
(``2 * MAX_WINDOW``) so worker clock skew can never let the sweep drop a
row a peer still considers live. Deleting a slightly-too-old row is free;
deleting a live throttle bucket would reset that IP/email's counter and
weaken an abuse control, so the threshold errs strictly on the side of
keeping rows longer.

The sweep is deployment-scope (both tables are tenant-agnostic — no
``workspace_id`` column) and idempotent: a DELETE of already-stale rows
is a no-op on the next tick, and a stale row is never re-touched, so
concurrent workers need no advisory lock.

See ``docs/specs/15-security-privacy.md`` §"Self-serve abuse
mitigations" and ``docs/specs/16-deployment-operations.md`` §"Worker
process".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from sqlalchemy import CursorResult, delete
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Delete

from app.adapters.db.ops.models import RateLimitBucket, ThrottleWindow
from app.adapters.db.session import make_uow
from app.tenancy import tenant_agnostic
from app.util.clock import Clock, SystemClock

__all__ = [
    "RATE_LIMIT_GC_MAX_WINDOW_SECONDS",
    "RATE_LIMIT_GC_RETENTION_SECONDS",
    "RateLimitGcReport",
    "sweep_stale_rate_limit_rows",
]


_log = logging.getLogger(__name__)


# Longest sliding window any caller of either table uses: the §15
# signup / recovery per-deployment cool-off is a 1 h rolling window
# (``app.auth._throttle._SIGNUP_WINDOW`` / ``_RECOVER_WINDOW``). Every
# other throttle window (per-IP request 1 min, passkey-login-begin
# 60 s) and the ``rate_limit_bucket`` 60 s token refill are shorter, so
# this single bound covers both tables. A row untouched for longer than
# this cannot influence a live limit decision.
RATE_LIMIT_GC_MAX_WINDOW_SECONDS: Final[int] = 3600

# Delete rows only once they are twice the longest window old. The extra
# window of margin absorbs any clock skew between workers so the sweep
# can never drop a row a peer still counts as live — deleting a live
# throttle bucket would reset its counter and weaken an abuse control.
RATE_LIMIT_GC_RETENTION_SECONDS: Final[int] = 2 * RATE_LIMIT_GC_MAX_WINDOW_SECONDS


@dataclass(frozen=True, slots=True)
class RateLimitGcReport:
    """Per-table deletion counts for one GC tick."""

    throttle_window_deleted: int
    rate_limit_bucket_deleted: int


def sweep_stale_rate_limit_rows(*, clock: Clock | None = None) -> RateLimitGcReport:
    """Delete stale rows from ``throttle_window`` + ``rate_limit_bucket``.

    Opens a fresh UoW (the worker has no ambient session), computes the
    epoch cutoff from ``clock.now()``, and issues one portable DELETE per
    table for rows whose ``updated_at_epoch`` is older than the cutoff.
    Commits on success; the UoW rolls back on any exception.

    The clock is injectable so tests can drive the cutoff
    deterministically; production passes ``None`` and falls back to
    :class:`SystemClock`. Mirrors the sibling
    :func:`app.worker.tasks.approval_ttl.sweep_expired_approvals`.
    """
    resolved_clock: Clock = clock if clock is not None else SystemClock()
    cutoff_epoch = resolved_clock.now().timestamp() - RATE_LIMIT_GC_RETENTION_SECONDS

    with make_uow() as session:
        # ``DbSession`` is the read-side Protocol; the concrete UoW
        # always yields a real :class:`Session`. Narrow here at the
        # write seam rather than widening the delete helpers.
        assert isinstance(session, Session)
        # justification: both tables are deployment-wide (no
        # workspace_id column); the GC deletes by updated_at_epoch, not
        # by tenant.
        with tenant_agnostic():
            throttle_deleted = _delete_stale(
                session,
                delete(ThrottleWindow).where(
                    ThrottleWindow.updated_at_epoch < cutoff_epoch
                ),
            )
            bucket_deleted = _delete_stale(
                session,
                delete(RateLimitBucket).where(
                    RateLimitBucket.updated_at_epoch < cutoff_epoch
                ),
            )

    report = RateLimitGcReport(
        throttle_window_deleted=throttle_deleted,
        rate_limit_bucket_deleted=bucket_deleted,
    )
    _log.info(
        "rate-limit GC sweep completed",
        extra={
            "event": "rate_limit.gc.sweep",
            "throttle_window_deleted": report.throttle_window_deleted,
            "rate_limit_bucket_deleted": report.rate_limit_bucket_deleted,
        },
    )
    return report


def _delete_stale(session: Session, statement: Delete) -> int:
    """Run one bulk DELETE and return its row count."""
    result = session.execute(statement)
    # ``Session.execute`` is typed ``Result[Any]``; bulk DML returns a
    # CursorResult with a concrete ``rowcount``. The narrow assert is
    # precise, not defensive — a non-cursor result means SQLAlchemy's
    # DELETE seam regressed and we want that failure loud.
    assert isinstance(result, CursorResult)
    return result.rowcount or 0
