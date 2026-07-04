"""Shared per-scope sliding-window throttle + decorator.

Two public surfaces:

* :class:`ShieldStore` — sliding-window hit counter over a pluggable
  :class:`~app.abuse.window_store.WindowStore` backend (in-memory for
  single-worker self-host, DB-backed for multi-worker deployments).
  Keyed by ``(scope, bucket_key)`` tuples so one shared store can serve
  any number of scopes without state bleed.
* :func:`throttle` — decorator that consults a :class:`ShieldStore`
  before the wrapped function runs. Derives the bucket key from the
  request via a caller-supplied ``key_fn``, refuses the 11th (or
  N+1th) hit inside ``window_s`` with
  ``HTTPException(429, {"error": "rate_limited"})``, otherwise
  records the hit and calls through.

**Why this exists alongside :class:`~app.auth._throttle.Throttle`.**
:class:`Throttle` is a class with per-feature named buckets
(``check_signup_start``, ``check_passkey_login_allowed``, etc.) —
each bucket carries its own semantics (3-fail lockouts, retry-after
hints, per-scope priority ordering) that don't compose into a
generic decorator shape. The passkey-login *begin* endpoint, by
contrast, is a pure N/window rate limit with no lockout or retry-
after — the natural fit for a scope-agnostic decorator. This module
introduces that decorator and migrates the login-begin route onto
it (cd-7huk). The rest of the per-feature buckets stay where they
are until a full migration is budgeted.

**Storage.** Delegated to :mod:`app.abuse.window_store`. Single-worker
self-host keeps an in-memory dict of deques (dependency-free, no DB
round-trip); a multi-worker deployment
(``settings.rate_limit_backend == "postgres"``) shares the
``throttle_window`` table so a per-deployment cap holds across every
worker instead of being multiplied by the worker count. The backend is
chosen with :func:`app.abuse.window_store.build_window_store`.

**No persistence (in-memory backend).** A process restart resets every
bucket. That is a feature, not a bug, for a dev-scoped throttle:
operators can clear the counters by bouncing the service. The shared
DB backend outlives a restart, matching multi-worker expectations.

See ``docs/specs/15-security-privacy.md`` §"Rate limiting and abuse
controls" for the spec intent (10/min/IP login begin, 5/min/IP
magic-link send, etc.).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import TypeVar

from fastapi import HTTPException, status

from app.abuse.window_store import MemoryWindowStore, WindowCheck, WindowStore
from app.util.clock import Clock, SystemClock

__all__ = ["ShieldStore", "throttle"]


class ShieldStore:
    """Scope-agnostic sliding-window counter over a pluggable backend.

    Callers hand in the scope + bucket key and the window, and the store
    decides whether a new hit fits inside the rolling budget. The actual
    hit lists live in an injected :class:`~app.abuse.window_store.WindowStore`:
    :class:`~app.abuse.window_store.MemoryWindowStore` by default (the
    dependency-free single-worker self-host path), or
    :class:`~app.abuse.window_store.DbWindowStore` for a multi-worker
    deployment where the counter must be shared across every worker.
    Construct with ``ShieldStore(store=build_window_store(settings))`` to
    pick the backend from config.

    The public surface (``check_and_record`` returning a ``bool``,
    ``clear``) is unchanged from the original in-memory store, so the
    ``@throttle`` decorator and every existing caller keep working.
    """

    __slots__ = ("_store",)

    def __init__(self, store: WindowStore | None = None) -> None:
        self._store = store if store is not None else MemoryWindowStore()

    def check_and_record(
        self,
        *,
        scope: str,
        key: str,
        limit: int,
        window: timedelta,
        now: datetime,
    ) -> bool:
        """Return ``True`` if the hit fits inside the budget, ``False`` if not.

        When the bucket is **under** ``limit``, the hit is recorded and
        the method returns ``True``. When the bucket is **at or over**
        ``limit``, the hit is **not** recorded (refusing a refusal
        bumps the window unfairly) and the method returns ``False``.

        The "record only on accept" rule matches the magic-link /
        signup / recovery flows in :class:`app.auth._throttle.Throttle`:
        a refused attempt should not extend the lockout beyond what
        the original burst earned. An attacker who hits the cap and
        keeps pounding doesn't push the window forward.
        """
        rejection = self._store.check_and_record_all(
            [WindowCheck(scope=scope, key=key, limit=limit, window=window)],
            now=now,
        )
        return rejection is None

    def clear(self) -> None:
        """Drop every bucket; used by tests that share the module store.

        Production code never calls this — the process restart (or a
        window roll on the shared backend) is the reset semantics. Tests
        that share :data:`_DEFAULT_STORE` across cases (rare: most pass
        ``store=`` explicitly) can drop the state between cases without
        re-importing the module.
        """
        self._store.clear()


# Default store for decorator call sites that don't pass an explicit
# ``store=``. In-memory: single-worker self-host and unit tests. The
# multi-worker path injects a shared-backed :class:`ShieldStore` at the
# call site (see :func:`app.api.v1.auth.passkey.build_login_router`).
# Tests construct their own :class:`ShieldStore` via the decorator's
# ``store=`` keyword so per-test state never bleeds across cases.
_DEFAULT_STORE = ShieldStore()


# Generic return type for the wrapped handler. The decorator is shape-
# preserving — the handler's signature is unchanged from the caller's
# perspective, so ``wraps`` carries the annotations through.
_R = TypeVar("_R")


def throttle(
    *,
    scope: str,
    key_fn: Callable[..., str],
    limit: int,
    window_s: int,
    store: ShieldStore | None = None,
    clock: Clock | None = None,
) -> Callable[[Callable[..., _R]], Callable[..., _R]]:
    """Return a decorator that rate-limits the wrapped callable.

    Parameters
    ----------
    scope:
        Per-surface identifier (e.g. ``"passkey.login.begin"``). Used
        as the first component of the bucket key so two surfaces
        sharing the same :class:`ShieldStore` never collide. Two
        callers that pick the *same* scope but hand in different
        ``key_fn``\\s will collide whenever both derivations yield
        equal strings — so in practice, scope is per-surface and
        key_fn shape is per-scope.
    key_fn:
        Called with the same positional + keyword arguments as the
        wrapped handler. Returns the string that identifies the
        bucket inside ``scope`` — typically a client-IP string or a
        hash thereof. The empty string is accepted (it pins every
        unresolved-client request to a single bucket), so a FastAPI
        test client that omits ``host`` still gets deterministic
        throttle behaviour instead of a crash.
    limit:
        Maximum number of hits permitted inside ``window_s`` for one
        bucket. The ``limit + 1``th hit inside the window returns
        429. Must be positive — callers that want "no rate limit"
        simply don't apply the decorator.
    window_s:
        Sliding-window width, in seconds. A hit recorded at ``t0``
        still counts against the bucket until ``t0 + window_s``.
    store:
        Optional explicit :class:`ShieldStore` — tests pass their own
        so per-test state never bleeds across sibling cases.
        Production call sites default to the module singleton.
    clock:
        Optional :class:`Clock` override — tests pass a
        :class:`~app.util.clock.FrozenClock` so the window boundaries
        are deterministic. Production call sites default to
        :class:`~app.util.clock.SystemClock`.

    Returns
    -------
    A decorator that wraps a callable of any signature. The wrapped
    callable sees its own arguments unchanged; when the bucket is
    over budget, it is never called — the decorator raises
    :class:`fastapi.HTTPException(429)` with ``{"error":
    "rate_limited"}`` before the handler body runs.

    Notes
    -----
    The decorator is **sync-only** by design (FastAPI runs sync
    handlers in a thread pool anyway; the passkey-login-begin handler
    is sync). An async variant would need an ``inspect.iscoroutinefunction``
    branch; that's a small diff the next caller can make if they land
    an async endpoint. Keeping the shape narrow here means the
    current caller surface has one obvious behaviour.

    Do **not** wrap a recursive function: each recursive call
    consumes one unit of budget, so a deeply recursive handler will
    429 itself once the call stack exceeds the cap. The decorator is
    meant for the request boundary, not internal hot loops.
    """
    if limit <= 0:
        raise ValueError(f"throttle limit must be positive; got {limit}")
    if window_s <= 0:
        raise ValueError(f"throttle window_s must be positive; got {window_s}")

    resolved_store = store if store is not None else _DEFAULT_STORE
    resolved_clock: Clock = clock if clock is not None else SystemClock()
    window = timedelta(seconds=window_s)

    def decorator(fn: Callable[..., _R]) -> Callable[..., _R]:
        @wraps(fn)
        def wrapper(*args: object, **kwargs: object) -> _R:
            # Derive the bucket key first; a key_fn that blows up is a
            # programming error and should surface as a 500 (the
            # caller's key_fn invariants are part of their contract).
            bucket_key = key_fn(*args, **kwargs)
            now = _now_aware_utc(resolved_clock)
            if not resolved_store.check_and_record(
                scope=scope,
                key=bucket_key,
                limit=limit,
                window=window,
                now=now,
            ):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={"error": "rate_limited"},
                )
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def _now_aware_utc(clock: Clock) -> datetime:
    """Return an aware-UTC ``datetime`` from ``clock``.

    :class:`SystemClock` already returns aware UTC; a test
    :class:`FrozenClock` enforces it on construction. The fallback
    normalisation here means a bespoke :class:`Clock` that somehow
    returns a naive datetime still lands a deterministic UTC moment
    rather than a comparison error when the deque is evicted.
    """
    moment = clock.now()
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment
