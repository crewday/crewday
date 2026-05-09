"""Focused coverage for the dev-profile Vite proxy seam."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.responses import Response

from app.config import Settings
from app.main import create_app


def _settings(
    *,
    profile: Literal["prod", "dev"] = "dev",
    phase0_stub_enabled: bool = False,
) -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-vite-proxy-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        smtp_host=None,
        smtp_port=587,
        smtp_from=None,
        smtp_use_tls=True,
        log_level="INFO",
        cors_allow_origins=[],
        profile=profile,
        vite_dev_url="http://127.0.0.1:5173",
        phase0_stub_enabled=phase0_stub_enabled,
    )


def _dev_app_with_mock(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    phase0_stub_enabled: bool = False,
) -> FastAPI:
    app = create_app(
        settings=_settings(
            profile="dev",
            phase0_stub_enabled=phase0_stub_enabled,
        )
    )
    app.state.vite_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:5173",
        follow_redirects=False,
    )
    return app


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_dev_profile_proxies_workspace_spa_deep_links_to_vite() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<!doctype html><title>crew.day dev</title>",
        )

    client = _client(_dev_app_with_mock(handler))

    for path in ("/w/dev/asset_types", "/w/dev/dashboard", "/w/dev/today"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "crew.day dev" in response.text

    assert seen_paths == ["/w/dev/asset_types", "/w/dev/dashboard", "/w/dev/today"]


def test_dev_profile_workspace_api_paths_are_not_proxied_to_vite() -> None:
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, content=b"should not be called")

    app = _dev_app_with_mock(handler, phase0_stub_enabled=True)
    client = _client(app)

    for path in ("/w/dev/api", "/w/dev/api/v1/does-not-exist"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
    assert called["n"] == 0


def test_dev_profile_workspace_events_path_is_not_proxied_to_vite() -> None:
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, content=b"should not be called")

    app = _dev_app_with_mock(handler, phase0_stub_enabled=True)
    response = _client(app).get("/w/dev/events")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert called["n"] == 0


def test_dev_profile_only_skips_workspace_spa_gets() -> None:
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, content=b"should not be called")

    app = _dev_app_with_mock(handler, phase0_stub_enabled=True)
    response = _client(app).post("/w/dev/asset_types")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert called["n"] == 0


def test_prod_profile_workspace_spa_deep_links_are_not_tenancy_skip_paths() -> None:
    app = create_app(settings=_settings(profile="prod", phase0_stub_enabled=True))

    @app.get("/w/{slug}/asset_types")
    def scoped_route() -> Response:
        return Response("tenant context required", media_type="text/plain")

    response = _client(app).get("/w/dev/asset_types")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
