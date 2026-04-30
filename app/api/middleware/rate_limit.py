"""API rate-limit middleware.

The middleware applies one token bucket to every API-shaped route:

* bearer-token callers key by token id;
* session and anonymous callers fall back to a peppered source-IP hash;
* personal access tokens whose scopes are all ``me.*`` get the larger
  self-service quota from §03.

Static pages, SPA fallbacks, docs, and health probes are deliberately
outside this middleware. Webhooks are included because they are
machine-facing HTTP APIs even though they do not live under
``/api/v1``.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Final, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session as DbSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.adapters.db.ops.models import RateLimitBucket
from app.adapters.db.ports import DbSession as DbSessionPort
from app.adapters.db.session import make_uow
from app.api.errors import problem_response
from app.auth._hashing import hash_with_pepper
from app.auth.keys import KeyDerivationError, derive_subkey
from app.config import Settings, get_settings
from app.tenancy.current import tenant_agnostic
from app.tenancy.middleware import (
    ACTOR_STATE_ATTR,
    WORKSPACE_REJECTION_STATE_ATTR,
    ActorIdentity,
)

__all__ = [
    "RATE_LIMIT_REMAINING_HEADER",
    "RATE_LIMIT_RESET_HEADER",
    "MemoryRateLimitBackend",
    "PersistentRateLimitBackend",
    "RateLimitClock",
    "RateLimitDecision",
    "RateLimitMiddleware",
    "SystemRateLimitClock",
    "api_route_class",
    "build_bucket_key",
    "build_rate_limit_backend",
    "is_personal_me_token",
]


RATE_LIMIT_REMAINING_HEADER: Final[str] = "X-RateLimit-Remaining"
RATE_LIMIT_RESET_HEADER: Final[str] = "X-RateLimit-Reset"
_IP_HASH_PURPOSE: Final[str] = "rate-limit-ip"
_DEFAULT_CLIENT_HOST: Final[str] = "0.0.0.0"


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Outcome of a single bucket consume attempt."""

    allowed: bool
    remaining: int
    reset_epoch_seconds: int
    retry_after_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class _BucketMath:
    allowed: bool
    remaining: int
    reset_after_seconds: float
    retry_after_seconds: int | None
    stored_tokens: float


@dataclass(frozen=True, slots=True)
class _BucketIdentity:
    key: str
    limit_per_minute: int


@dataclass(slots=True)
class _MemoryBucket:
    tokens: float
    updated_at: float


class RateLimitClock(Protocol):
    """Clock seam for rate-limit math."""

    def monotonic(self) -> float: ...

    def time(self) -> float: ...


class SystemRateLimitClock:
    """Production clock: monotonic for local math, wall time for headers."""

    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()


class _RateLimitBackend(Protocol):
    def consume(
        self,
        *,
        bucket_key: str,
        limit_per_minute: int,
        clock: RateLimitClock,
    ) -> RateLimitDecision: ...


def api_route_class(path: str) -> str | None:
    """Return the bounded API route class for ``path`` or ``None``.

    Route class is intentionally coarse so the bucket key has bounded
    cardinality. We include webhooks because they are machine-facing API
    endpoints; health probes, docs, static assets, and SPA pages do not
    match any branch here.
    """
    if path == "/api/openapi.json":
        return None
    if path == "/api" or path.startswith("/api/"):
        return "bare-api"
    if path == "/admin/api" or path.startswith("/admin/api/"):
        return "admin-api"
    if path == "/webhooks" or path.startswith("/webhooks/"):
        return "webhook"

    segments = path.split("/")
    if len(segments) >= 5 and segments[1] == "w" and segments[3] == "api":
        return "workspace-api"
    return None


def is_personal_me_token(actor: ActorIdentity | None) -> bool:
    """Return ``True`` for PATs limited to the ``me.*`` scope family."""
    if actor is None or actor.principal_kind != "token":
        return False
    if actor.token_kind != "personal":
        return False
    scopes = actor.token_scopes
    return bool(scopes) and all(scope.startswith("me.") for scope in scopes)


def _client_host(request: Request) -> str:
    if request.client is None or not request.client.host:
        return _DEFAULT_CLIENT_HOST
    return request.client.host


def _hash_ip(ip: str, settings: Settings) -> str:
    try:
        pepper = derive_subkey(settings.root_key, purpose=_IP_HASH_PURPOSE)
    except KeyDerivationError:
        # Some unit-test settings intentionally omit CREWDAY_ROOT_KEY. Do
        # not persist the raw IP; collapse to an unkeyed digest for those
        # local-only configurations.
        return hashlib.sha256(f"crewday-rate-limit:{ip}".encode()).hexdigest()
    return hash_with_pepper(ip, pepper)


def _actor_from_request(request: Request) -> ActorIdentity | None:
    actor = getattr(request.state, ACTOR_STATE_ATTR, None)
    if isinstance(actor, ActorIdentity):
        return actor
    return None


def build_bucket_key(
    request: Request,
    *,
    route_class: str,
    settings: Settings,
) -> _BucketIdentity:
    """Build the privacy-preserving bucket key and quota for ``request``."""
    actor = _actor_from_request(request)
    if actor is not None and actor.principal_kind == "token" and actor.token_id:
        limit = (
            settings.rate_limit_personal_me_per_minute
            if is_personal_me_token(actor)
            else settings.rate_limit_token_per_minute
        )
        return _BucketIdentity(
            key=f"token:{actor.token_id}:{route_class}",
            limit_per_minute=limit,
        )

    ip_hash = _hash_ip(_client_host(request), settings)
    return _BucketIdentity(
        key=f"ip:{ip_hash}:{route_class}",
        limit_per_minute=settings.rate_limit_anonymous_per_minute,
    )


def _evaluate_bucket(
    *,
    tokens: float,
    updated_at: float,
    now: float,
    limit_per_minute: int,
) -> _BucketMath:
    capacity = float(limit_per_minute)
    refill_per_second = capacity / 60.0
    elapsed = max(0.0, now - updated_at)
    available = min(capacity, tokens + (elapsed * refill_per_second))

    if available >= 1.0:
        stored = available - 1.0
        remaining = math.floor(stored)
        reset_after = (capacity - stored) / refill_per_second
        return _BucketMath(
            allowed=True,
            remaining=max(0, min(limit_per_minute, remaining)),
            reset_after_seconds=max(0.0, reset_after),
            retry_after_seconds=None,
            stored_tokens=stored,
        )

    retry_after = max(1, math.ceil((1.0 - available) / refill_per_second))
    return _BucketMath(
        allowed=False,
        remaining=0,
        reset_after_seconds=float(retry_after),
        retry_after_seconds=retry_after,
        stored_tokens=available,
    )


def _decision_from_math(
    math_result: _BucketMath,
    *,
    wall_now: float,
) -> RateLimitDecision:
    return RateLimitDecision(
        allowed=math_result.allowed,
        remaining=math_result.remaining,
        reset_epoch_seconds=math.ceil(wall_now + math_result.reset_after_seconds),
        retry_after_seconds=math_result.retry_after_seconds,
    )


class MemoryRateLimitBackend:
    """In-process token buckets for single-worker dev and unit tests."""

    def __init__(self) -> None:
        self._buckets: dict[str, _MemoryBucket] = {}
        self._lock = threading.Lock()

    def consume(
        self,
        *,
        bucket_key: str,
        limit_per_minute: int,
        clock: RateLimitClock,
    ) -> RateLimitDecision:
        now = clock.monotonic()
        wall_now = clock.time()
        with self._lock:
            bucket = self._buckets.get(bucket_key)
            if bucket is None:
                bucket = _MemoryBucket(tokens=float(limit_per_minute), updated_at=now)
            result = _evaluate_bucket(
                tokens=bucket.tokens,
                updated_at=bucket.updated_at,
                now=now,
                limit_per_minute=limit_per_minute,
            )
            self._buckets[bucket_key] = _MemoryBucket(
                tokens=result.stored_tokens,
                updated_at=now,
            )
        return _decision_from_math(result, wall_now=wall_now)


class PersistentRateLimitBackend:
    """Database-backed token buckets for multi-process deployments."""

    def __init__(
        self,
        uow_factory: Callable[[], AbstractContextManager[DbSessionPort]] = make_uow,
    ) -> None:
        self._uow_factory = uow_factory

    def consume(
        self,
        *,
        bucket_key: str,
        limit_per_minute: int,
        clock: RateLimitClock,
    ) -> RateLimitDecision:
        now = clock.time()
        with self._uow_factory() as db_session:
            assert isinstance(db_session, DbSession)
            with tenant_agnostic():
                _lock_bucket(db_session, bucket_key=bucket_key)
                row = db_session.get(RateLimitBucket, bucket_key)
                if row is None:
                    result = _evaluate_bucket(
                        tokens=float(limit_per_minute),
                        updated_at=now,
                        now=now,
                        limit_per_minute=limit_per_minute,
                    )
                    db_session.add(
                        RateLimitBucket(
                            bucket_key=bucket_key,
                            tokens=result.stored_tokens,
                            updated_at_epoch=now,
                        )
                    )
                    db_session.flush()
                else:
                    result = _evaluate_bucket(
                        tokens=row.tokens,
                        updated_at=row.updated_at_epoch,
                        now=now,
                        limit_per_minute=limit_per_minute,
                    )
                    row.tokens = result.stored_tokens
                    row.updated_at_epoch = now
                    db_session.flush()
        return _decision_from_math(result, wall_now=now)


def _lock_bucket(db_session: DbSession, *, bucket_key: str) -> None:
    """Take a Postgres transaction advisory lock for one bucket key."""
    if db_session.bind is None or db_session.bind.dialect.name != "postgresql":
        return
    db_session.execute(
        text("SELECT pg_advisory_xact_lock(CAST(hashtext(:bucket_key) AS bigint))"),
        {"bucket_key": bucket_key},
    )


def build_rate_limit_backend(settings: Settings) -> _RateLimitBackend:
    if settings.rate_limit_backend == "postgres":
        return PersistentRateLimitBackend()
    return MemoryRateLimitBackend()


def _add_headers(response: Response, decision: RateLimitDecision) -> None:
    response.headers[RATE_LIMIT_REMAINING_HEADER] = str(decision.remaining)
    response.headers[RATE_LIMIT_RESET_HEADER] = str(decision.reset_epoch_seconds)


def _pop_workspace_rejection_response(request: Request) -> Response | None:
    response = getattr(request.state, WORKSPACE_REJECTION_STATE_ATTR, None)
    if isinstance(response, Response):
        delattr(request.state, WORKSPACE_REJECTION_STATE_ATTR)
        return response
    return None


def _rate_limited_response(request: Request, decision: RateLimitDecision) -> Response:
    retry_after = decision.retry_after_seconds
    if retry_after is None:
        retry_after = 1
    response = problem_response(
        request,
        status=429,
        type_name="rate_limited",
        title="Rate limited",
        detail="Too many API requests; retry after the indicated delay.",
        extra={"retry_after_seconds": retry_after},
        extra_headers={"Retry-After": str(retry_after)},
    )
    _add_headers(response, decision)
    return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply per-token or per-IP API rate limits."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings | None = None,
        backend: _RateLimitBackend | None = None,
        clock: RateLimitClock | None = None,
    ) -> None:
        super().__init__(app)
        self._settings = settings if settings is not None else get_settings()
        self._backend = (
            backend if backend is not None else build_rate_limit_backend(self._settings)
        )
        self._clock = clock if clock is not None else SystemRateLimitClock()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        route_class = api_route_class(request.url.path)
        if route_class is None:
            return await call_next(request)

        rejection_response = _pop_workspace_rejection_response(request)
        bucket = build_bucket_key(
            request,
            route_class=route_class,
            settings=self._settings,
        )
        decision = self._backend.consume(
            bucket_key=bucket.key,
            limit_per_minute=bucket.limit_per_minute,
            clock=self._clock,
        )
        if not decision.allowed:
            return _rate_limited_response(request, decision)

        if rejection_response is not None:
            _add_headers(rejection_response, decision)
            return rejection_response

        response = await call_next(request)
        _add_headers(response, decision)
        return response
