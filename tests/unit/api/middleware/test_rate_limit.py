"""Unit tests for :mod:`app.api.middleware.rate_limit`."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.api.errors import CONTENT_TYPE_PROBLEM_JSON
from app.api.middleware.rate_limit import (
    RATE_LIMIT_REMAINING_HEADER,
    RATE_LIMIT_RESET_HEADER,
    MemoryRateLimitBackend,
    RateLimitMiddleware,
    api_route_class,
    build_bucket_key,
    is_personal_me_token,
)
from app.config import Settings
from app.tenancy.middleware import (
    ACTOR_STATE_ATTR,
    WORKSPACE_REJECTION_STATE_ATTR,
    ActorIdentity,
)


class FrozenRateLimitClock:
    """Mutable clock for deterministic token-bucket assertions."""

    def __init__(self, *, monotonic: float = 0.0, wall: float = 1_000.0) -> None:
        self._monotonic = monotonic
        self._wall = wall

    def monotonic(self) -> float:
        return self._monotonic

    def time(self) -> float:
        return self._wall

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds
        self._wall += seconds


def _settings(
    *,
    token_limit: int = 2,
    personal_limit: int = 4,
    anonymous_limit: int = 2,
) -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-rate-limit-root-key"),
        rate_limit_backend="memory",
        rate_limit_token_per_minute=token_limit,
        rate_limit_personal_me_per_minute=personal_limit,
        rate_limit_anonymous_per_minute=anonymous_limit,
    )


def _actor(
    token_id: str | None,
    *,
    token_kind: str | None = "scoped",
    scopes: dict[str, object] | None = None,
) -> ActorIdentity:
    return ActorIdentity(
        user_id="01HX00000000000000000USR000",
        kind="user",
        workspace_id="01HX00000000000000000WS0000",
        token_id=token_id,
        session_id=None if token_id is not None else "01HX00000000000000SES000",
        principal_kind="token" if token_id is not None else "session",
        token_kind=token_kind,
        token_scopes=dict(scopes or {}),
    )


async def _handler(_request: Request) -> Response:
    return JSONResponse({"ok": True})


def _app(
    *,
    actor: ActorIdentity | None,
    settings: Settings,
    clock: FrozenRateLimitClock,
    backend: MemoryRateLimitBackend | None = None,
    workspace_rejection_status: int | None = None,
) -> Starlette:
    app = Starlette(
        routes=[
            Route("/api/v1/ping", _handler, methods=["GET", "POST"]),
            Route("/w/demo/api/v1/ping", _handler, methods=["GET"]),
            Route("/webhooks/chat/twilio", _handler, methods=["POST"]),
            Route("/healthz", _handler, methods=["GET"]),
        ]
    )
    app.add_middleware(
        RateLimitMiddleware,
        settings=settings,
        backend=backend if backend is not None else MemoryRateLimitBackend(),
        clock=clock,
    )

    async def stamp_actor(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if actor is not None:
            setattr(request.state, ACTOR_STATE_ATTR, actor)
        if workspace_rejection_status is not None:
            setattr(
                request.state,
                WORKSPACE_REJECTION_STATE_ATTR,
                JSONResponse(
                    {"error": "not_found", "detail": None},
                    status_code=workspace_rejection_status,
                ),
            )
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=stamp_actor)
    return app


class TestRouteClassification:
    def test_api_paths_are_classified(self) -> None:
        assert api_route_class("/api/v1/tasks") == "bare-api"
        assert api_route_class("/admin/api/v1/workspaces") == "admin-api"
        assert api_route_class("/w/villa/api/v1/tasks") == "workspace-api"
        assert api_route_class("/webhooks/chat/twilio") == "webhook"

    def test_non_api_paths_are_excluded(self) -> None:
        assert api_route_class("/healthz") is None
        assert api_route_class("/docs") is None
        assert api_route_class("/api/openapi.json") is None
        assert api_route_class("/w/villa/today") is None
        assert api_route_class("/w/villa/events") is None
        assert api_route_class("/assets/app.js") is None


class TestTokenTier:
    def test_personal_me_token_detected(self) -> None:
        actor = _actor(
            "01HXTOK00000000000000TOK00",
            token_kind="personal",
            scopes={"me.tasks:read": True, "me.profile:write": True},
        )
        assert is_personal_me_token(actor)

    def test_mixed_or_non_personal_token_not_personal_tier(self) -> None:
        mixed = _actor(
            "01HXTOK00000000000000TOK00",
            token_kind="personal",
            scopes={"me.tasks:read": True, "tasks:read": True},
        )
        scoped = _actor(
            "01HXTOK00000000000000TOK01",
            token_kind="scoped",
            scopes={"me.tasks:read": True},
        )
        assert not is_personal_me_token(mixed)
        assert not is_personal_me_token(scoped)


class TestMiddleware:
    def test_success_headers_reflect_post_accept_remaining(self) -> None:
        settings = _settings(token_limit=2)
        clock = FrozenRateLimitClock()
        app = _app(
            actor=_actor("01HXTOK00000000000000TOK00"),
            settings=settings,
            clock=clock,
        )

        with TestClient(app) as client:
            first = client.get("/api/v1/ping")
            second = client.get("/api/v1/ping")

        assert first.status_code == 200
        assert first.headers[RATE_LIMIT_REMAINING_HEADER] == "1"
        assert first.headers[RATE_LIMIT_RESET_HEADER] == "1030"
        assert second.status_code == 200
        assert second.headers[RATE_LIMIT_REMAINING_HEADER] == "0"
        assert second.headers[RATE_LIMIT_RESET_HEADER] == "1060"

    def test_429_uses_problem_json_and_retry_after(self) -> None:
        settings = _settings(token_limit=1)
        clock = FrozenRateLimitClock()
        app = _app(
            actor=_actor("01HXTOK00000000000000TOK00"),
            settings=settings,
            clock=clock,
        )

        with TestClient(app) as client:
            assert client.get("/api/v1/ping").status_code == 200
            limited = client.get("/api/v1/ping")

        assert limited.status_code == 429
        assert limited.headers["content-type"] == CONTENT_TYPE_PROBLEM_JSON
        assert limited.headers["Retry-After"] == "60"
        assert limited.headers[RATE_LIMIT_REMAINING_HEADER] == "0"
        assert limited.json()["type"].endswith("/rate_limited")
        assert limited.json()["retry_after_seconds"] == 60

    def test_refill_allows_later_request(self) -> None:
        settings = _settings(token_limit=1)
        clock = FrozenRateLimitClock()
        app = _app(
            actor=_actor("01HXTOK00000000000000TOK00"),
            settings=settings,
            clock=clock,
        )

        with TestClient(app) as client:
            assert client.get("/api/v1/ping").status_code == 200
            assert client.get("/api/v1/ping").status_code == 429
            clock.advance(60)
            refilled = client.get("/api/v1/ping")

        assert refilled.status_code == 200
        assert refilled.headers[RATE_LIMIT_REMAINING_HEADER] == "0"

    def test_personal_me_token_gets_higher_quota(self) -> None:
        settings = _settings(token_limit=1, personal_limit=2)
        clock = FrozenRateLimitClock()
        app = _app(
            actor=_actor(
                "01HXTOK00000000000000PAT00",
                token_kind="personal",
                scopes={"me.tasks:read": True},
            ),
            settings=settings,
            clock=clock,
        )

        with TestClient(app) as client:
            first = client.get("/api/v1/ping")
            second = client.get("/api/v1/ping")
            third = client.get("/api/v1/ping")

        assert first.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 429

    def test_anonymous_requests_fall_back_to_ip_bucket(self) -> None:
        settings = _settings(anonymous_limit=1)
        clock = FrozenRateLimitClock()
        app = _app(actor=None, settings=settings, clock=clock)

        with TestClient(app) as client:
            first = client.get("/api/v1/ping")
            second = client.get("/api/v1/ping")

        assert first.status_code == 200
        assert second.status_code == 429

    def test_scoped_workspace_rejections_are_ip_limited(self) -> None:
        settings = _settings(anonymous_limit=1)
        clock = FrozenRateLimitClock()
        app = _app(
            actor=None,
            settings=settings,
            clock=clock,
            workspace_rejection_status=404,
        )

        with TestClient(app) as client:
            first = client.get("/w/demo/api/v1/ping")
            second = client.get("/w/demo/api/v1/ping")

        assert first.status_code == 404
        assert first.headers[RATE_LIMIT_REMAINING_HEADER] == "0"
        assert second.status_code == 429

    def test_non_api_path_is_not_limited_or_headered(self) -> None:
        settings = _settings(anonymous_limit=1)
        clock = FrozenRateLimitClock()
        app = _app(actor=None, settings=settings, clock=clock)

        with TestClient(app) as client:
            first = client.get("/healthz")
            second = client.get("/healthz")

        assert first.status_code == 200
        assert second.status_code == 200
        assert RATE_LIMIT_REMAINING_HEADER not in first.headers

    def test_webhook_paths_are_limited(self) -> None:
        settings = _settings(anonymous_limit=1)
        clock = FrozenRateLimitClock()
        app = _app(actor=None, settings=settings, clock=clock)

        with TestClient(app) as client:
            first = client.post("/webhooks/chat/twilio")
            second = client.post("/webhooks/chat/twilio")

        assert first.status_code == 200
        assert second.status_code == 429

    def test_token_and_ip_keys_do_not_collide(self) -> None:
        settings = _settings(token_limit=1, anonymous_limit=1)
        clock = FrozenRateLimitClock()
        backend = MemoryRateLimitBackend()
        token_app = _app(
            actor=_actor("01HXTOK00000000000000TOK00"),
            settings=settings,
            clock=clock,
            backend=backend,
        )
        anonymous_app = _app(
            actor=None,
            settings=settings,
            clock=clock,
            backend=backend,
        )

        with TestClient(token_app) as token_client:
            assert token_client.get("/api/v1/ping").status_code == 200
        with TestClient(anonymous_app) as anonymous_client:
            assert anonymous_client.get("/api/v1/ping").status_code == 200


def test_ip_bucket_key_does_not_contain_raw_ip() -> None:
    settings = _settings()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/ping",
        "headers": [],
        "client": ("203.0.113.9", 1234),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    bucket = build_bucket_key(
        Request(scope),
        route_class="bare-api",
        settings=settings,
    )
    assert "203.0.113.9" not in bucket.key
    assert bucket.key.startswith("ip:")
