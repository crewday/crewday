"""In-memory rate-limiter for the magic-link surface.

**Temporary home — see cd-7huk.** This module exists because
:mod:`app.auth.magic_link` needs a rate-limit gate today and the
shared abuse-throttle module (``app/abuse/throttle.py``) has not yet
landed. cd-7huk absorbs these checks into the deployment-wide
throttle; until then the magic-link service keeps them private.

Two scoped buckets per caller:

* **Request rate** — per-IP and per-email fixed window, 5 hits / 60 s
  on ``/auth/magic/request`` (§15 "Rate limiting and abuse controls":
  "5/min per IP for magic-link send").
* **Consume failure lockout** — per-IP sliding counter, 3 failed
  attempts / 60 s → 10-minute lockout on ``/auth/magic/consume``
  (§15: "3 failed attempts → 10-minute IP lockout").

Storage: single process memory. crew.day v1 runs one worker per
deployment (§01 "One worker pool per process"), so an in-memory dict
is correct for both semantics and audit trail. Horizontal scaling
(if it ever lands) will move this to a shared Redis-backed bucket
inside the cd-7huk rewrite.

Concurrency: a :class:`threading.Lock` guards every dict mutation.
The lock is process-wide but the critical sections are tiny — a list
append + trim — so contention is a non-issue at the deployment sizes
we care about.

No persistence: a process restart resets every bucket. That's a
feature, not a bug, for a dev-scoped throttle: operators can clear
the counters by bouncing the service.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

__all__ = [
    "ConsumeLockout",
    "RateLimited",
    "Throttle",
]


# Defaults documented in the module docstring and §15. Exposed as
# module-level Finals so tests can monkey-patch them to tight values
# without re-plumbing the service.
_REQUEST_LIMIT: Final[int] = 5
_REQUEST_WINDOW: Final[timedelta] = timedelta(minutes=1)

_CONSUME_FAIL_LIMIT: Final[int] = 3
_CONSUME_FAIL_WINDOW: Final[timedelta] = timedelta(minutes=1)
_CONSUME_LOCKOUT: Final[timedelta] = timedelta(minutes=10)


class RateLimited(Exception):
    """Caller exceeded the per-scope request budget.

    429-equivalent. The HTTP router maps this to ``429 rate_limited``.
    """


class ConsumeLockout(Exception):
    """Caller IP is locked out of consume for the configured window.

    429-equivalent. Distinct from :class:`RateLimited` so the router
    can emit a different error symbol (``consume_locked_out``) and
    the test suite can pin the 3-fail trigger semantics.
    """


@dataclass(frozen=True, slots=True)
class _BucketKey:
    """``(scope, key)`` tuple that identifies a single bucket.

    ``scope`` is one of ``"request:ip"``, ``"request:email"``, or
    ``"consume_fail:ip"``; ``key`` is the IP (or email hash) string.
    Frozen so it hashes under :class:`dict` / :class:`defaultdict`.
    """

    scope: str
    key: str


class Throttle:
    """Per-process counter bucket with tripwires for magic-link flows.

    A single instance is shared by both routes; tests construct their
    own so the suite's state never bleeds across cases. The class is
    threadsafe but deliberately not async-aware — the work is
    microseconds of dict mutation, no I/O.
    """

    __slots__ = ("_fail_locked_until", "_fails", "_hits", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Fixed-window hits: {(scope, key): [hit_dt, ...]}. A deque
        # keeps the append O(1) and the left-trim cheap.
        self._hits: dict[_BucketKey, deque[datetime]] = defaultdict(deque)
        # Per-IP failed-consume counters — same shape as ``_hits``
        # but the reset trigger is different (§15: 3 fails within
        # the window flips the lockout).
        self._fails: dict[str, deque[datetime]] = defaultdict(deque)
        # IPs currently locked out (value is the moment the lockout
        # expires). Not a deque — single expiry per key.
        self._fail_locked_until: dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Request (/auth/magic/request) budget
    # ------------------------------------------------------------------

    def check_request(self, *, ip: str, email_hash: str, now: datetime) -> None:
        """Raise :class:`RateLimited` if either IP or email is over budget.

        Hits against the per-IP and per-email buckets count separately;
        a single call advances both. Exceeding either raises — the
        router maps the exception to ``429 rate_limited``. Below the
        gate, the enumeration guard still applies: a matched email and
        a missing email both produce an identical ``202`` response, so
        a caller who stays under the budget learns nothing about
        whether their email exists.
        """
        with self._lock:
            if self._over_limit(_BucketKey("request:ip", ip), now):
                raise RateLimited(f"per-IP request budget exceeded for {ip!r}")
            if self._over_limit(_BucketKey("request:email", email_hash), now):
                raise RateLimited("per-email request budget exceeded")
            # Under budget — record both hits so future calls see them.
            self._record_hit(_BucketKey("request:ip", ip), now)
            self._record_hit(_BucketKey("request:email", email_hash), now)

    # ------------------------------------------------------------------
    # Consume (/auth/magic/consume) lockout
    # ------------------------------------------------------------------

    def check_consume_allowed(self, *, ip: str, now: datetime) -> None:
        """Raise :class:`ConsumeLockout` if ``ip`` is inside its lockout.

        The router calls this **before** trying to consume the token
        so a locked-out IP never even touches the nonce row. Clears a
        lapsed lockout in passing.
        """
        with self._lock:
            self._evict_expired_lockout(ip, now)
            if ip in self._fail_locked_until:
                raise ConsumeLockout(f"consume locked out for {ip!r}")

    def record_consume_failure(self, *, ip: str, now: datetime) -> None:
        """Increment the per-IP failure counter; flip lockout on the Nth fail.

        The router calls this after a consume raises (bad signature,
        unknown nonce, expired, already-consumed, purpose mismatch) —
        anything observable as "the caller asked us to redeem a token
        that didn't redeem". Success does **not** call this.
        """
        with self._lock:
            bucket = self._fails[ip]
            self._evict_expired(bucket, now, _CONSUME_FAIL_WINDOW)
            bucket.append(now)
            if len(bucket) >= _CONSUME_FAIL_LIMIT:
                self._fail_locked_until[ip] = now + _CONSUME_LOCKOUT
                # Clear the rolling window so the IP has to earn the
                # next lockout from scratch once this one expires.
                bucket.clear()

    def record_consume_success(self, *, ip: str) -> None:
        """Reset the per-IP failure counter on a successful consume.

        A consume that returned a fresh ``MagicLinkOutcome`` means the
        user finally got through — we don't want one bad attempt an
        hour ago to still count against their next legitimate try.
        """
        with self._lock:
            self._fails.pop(ip, None)
            self._fail_locked_until.pop(ip, None)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _over_limit(self, key: _BucketKey, now: datetime) -> bool:
        bucket = self._hits[key]
        self._evict_expired(bucket, now, _REQUEST_WINDOW)
        return len(bucket) >= _REQUEST_LIMIT

    def _record_hit(self, key: _BucketKey, now: datetime) -> None:
        self._hits[key].append(now)

    @staticmethod
    def _evict_expired(
        bucket: deque[datetime], now: datetime, window: timedelta
    ) -> None:
        """Drop hits older than ``now - window`` from the left of ``bucket``."""
        cutoff = now - window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def _evict_expired_lockout(self, ip: str, now: datetime) -> None:
        """Clear ``ip`` from the lockout table if the ban has elapsed."""
        expires_at = self._fail_locked_until.get(ip)
        if expires_at is not None and expires_at <= now:
            del self._fail_locked_until[ip]
