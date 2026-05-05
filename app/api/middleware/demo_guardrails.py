"""Demo-mode edge guardrails."""

from __future__ import annotations

import ipaddress
import threading
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.abuse.throttle import ShieldStore
from app.api.errors import problem_response
from app.config import Settings, get_settings
from app.demo.guardrails import demo_disabled_response, disabled_integration_for_path
from app.util.clock import Clock, SystemClock

__all__ = ["DemoGuardrailMiddleware"]

_DEFAULT_CLIENT_HOST = "0.0.0.0"


@dataclass(frozen=True)
class _DemoRequestProfile:
    content_length: int | None
    max_payload: int
    workspace_slug: str | None
    client_ip: str
    is_upload: bool


class DemoGuardrailMiddleware(BaseHTTPMiddleware):
    """Apply demo-only deny-list, payload, mutation, and chat-turn limits."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings | None = None,
        store: ShieldStore | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(app)
        self._settings = settings if settings is not None else get_settings()
        self._store = store if store is not None else ShieldStore()
        self._clock = clock if clock is not None else SystemClock()
        self._uploads = _DemoUploadLedger()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # code-health: ignore[ccn nloc] Demo guardrails stay ordered.
        settings = self._settings
        if not settings.demo_mode:
            return await call_next(request)

        if _blocked_ip(request, settings):
            return _demo_problem(
                request,
                status=403,
                type_name="demo_ip_blocked",
                title="Demo request blocked",
                detail="This source IP is blocked from the public demo.",
            )

        disabled = disabled_integration_for_path(request.url.path, request.method)
        if disabled is not None:
            return demo_disabled_response(request, disabled)

        profile = _demo_request_profile(request, settings)
        if _body_requires_content_length(request, profile):
            return _demo_problem(
                request,
                status=411,
                type_name="demo_content_length_required",
                title="Content-Length required",
                detail="Demo requests with a body must declare their size.",
            )

        if (
            profile.content_length is not None
            and profile.content_length > profile.max_payload
        ):
            return _demo_problem(
                request,
                status=413,
                type_name="demo_payload_too_large",
                title="Payload too large",
                detail="Demo request payload is over the configured cap.",
            )

        if profile.is_upload:
            allowed, reason = self._uploads.check_and_record(
                _DemoUploadAttempt(
                    ip=profile.client_ip,
                    workspace_slug=profile.workspace_slug,
                    content_length=profile.content_length or 0,
                    now=self._clock.now(),
                    bytes_per_ip_per_day=settings.demo_upload_bytes_per_ip_per_day,
                    uploads_per_workspace=(
                        settings.demo_uploads_per_workspace_lifetime
                    ),
                )
            )
            if not allowed:
                return _demo_problem(
                    request,
                    status=429,
                    type_name=reason,
                    title="Rate limited",
                    detail="Demo upload quota exceeded.",
                )

        if _mutation_rate_limited(
            request,
            store=self._store,
            workspace_slug=profile.workspace_slug,
            limit=settings.demo_mutations_per_workspace_per_minute,
            now=self._clock.now(),
        ):
            return _demo_problem(
                request,
                status=429,
                type_name="demo_mutation_rate_limited",
                title="Rate limited",
                detail="Too many demo writes for this workspace.",
            )

        if _llm_rate_limited(
            request,
            store=self._store,
            workspace_slug=profile.workspace_slug,
            limit=settings.demo_llm_turns_per_workspace_per_minute,
            now=self._clock.now(),
        ):
            return _demo_problem(
                request,
                status=429,
                type_name="demo_llm_rate_limited",
                title="Rate limited",
                detail="Too many demo agent turns for this workspace.",
            )

        return await call_next(request)


def _demo_request_profile(
    request: Request,
    settings: Settings,
) -> _DemoRequestProfile:
    is_upload = _is_upload_request(request)
    return _DemoRequestProfile(
        content_length=_content_length(request),
        max_payload=(
            settings.demo_max_upload_bytes
            if is_upload
            else settings.demo_max_payload_bytes
        ),
        workspace_slug=_workspace_slug(request.url.path),
        client_ip=_client_host(request),
        is_upload=is_upload,
    )


def _body_requires_content_length(
    request: Request,
    profile: _DemoRequestProfile,
) -> bool:
    return profile.content_length is None and request.method.upper() in {
        "POST",
        "PUT",
        "PATCH",
    }


def _mutation_rate_limited(
    request: Request,
    *,
    store: ShieldStore,
    workspace_slug: str | None,
    limit: int,
    now: datetime,
) -> bool:
    return (
        workspace_slug is not None
        and request.method.upper()
        in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }
        and not store.check_and_record(
            scope="demo.mutation",
            key=workspace_slug,
            limit=limit,
            window=timedelta(minutes=1),
            now=now,
        )
    )


def _llm_rate_limited(
    request: Request,
    *,
    store: ShieldStore,
    workspace_slug: str | None,
    limit: int,
    now: datetime,
) -> bool:
    return (
        workspace_slug is not None
        and _is_llm_turn(request.url.path)
        and not store.check_and_record(
            scope="demo.llm_turn",
            key=workspace_slug,
            limit=limit,
            window=timedelta(minutes=1),
            now=now,
        )
    )


@dataclass(frozen=True)
class _DemoUploadAttempt:
    ip: str
    workspace_slug: str | None
    content_length: int
    now: datetime
    bytes_per_ip_per_day: int
    uploads_per_workspace: int


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def _client_host(request: Request) -> str:
    if request.client is None or not request.client.host:
        return _DEFAULT_CLIENT_HOST
    return request.client.host


def _blocked_ip(request: Request, settings: Settings) -> bool:
    cidrs = settings.demo_block_cidr
    if not cidrs:
        return False
    try:
        ip = ipaddress.ip_address(_client_host(request))
    except ValueError:
        return False
    return any(ip in network for network in cidrs)


def _workspace_slug(path: str) -> str | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "w":
        return parts[1]
    return None


def _is_llm_turn(path: str) -> bool:
    return "/agent/" in path or path.endswith("/agent") or "/tasks/nl" in path


def _is_upload_request(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    return content_type.lower().startswith("multipart/")


class _DemoUploadLedger:
    """In-process demo upload counters for public-demo abuse caps."""

    __slots__ = ("_bytes_by_ip", "_lock", "_uploads_by_workspace")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bytes_by_ip: dict[str, deque[tuple[datetime, int]]] = defaultdict(deque)
        self._uploads_by_workspace: dict[str, int] = defaultdict(int)

    def check_and_record(
        self,
        attempt: _DemoUploadAttempt,
    ) -> tuple[bool, str]:
        with self._lock:
            bucket = self._bytes_by_ip[attempt.ip]
            cutoff = attempt.now - timedelta(days=1)
            while bucket and bucket[0][0] < cutoff:
                bucket.popleft()
            spent = sum(size for _, size in bucket)
            if spent + attempt.content_length > attempt.bytes_per_ip_per_day:
                return False, "demo_upload_bytes_rate_limited"
            if (
                attempt.workspace_slug is not None
                and self._uploads_by_workspace[attempt.workspace_slug]
                >= attempt.uploads_per_workspace
            ):
                return False, "demo_upload_count_rate_limited"
            bucket.append((attempt.now, attempt.content_length))
            if attempt.workspace_slug is not None:
                self._uploads_by_workspace[attempt.workspace_slug] += 1
            return True, ""


def _demo_problem(
    request: Request,
    *,
    status: int,
    type_name: str,
    title: str,
    detail: str,
) -> Response:
    return problem_response(
        request,
        status=status,
        type_name=type_name,
        title=title,
        detail=detail,
    )
