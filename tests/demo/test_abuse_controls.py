"""Demo-mode abuse controls."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.factory import create_app
from app.config import Settings


@dataclass(frozen=True, slots=True)
class _Seeded:
    workspace_id: str = "01HWA00000000000000000WSP"
    workspace_slug: str = "demo-rental-manager-test"
    persona_user_id: str = "01HWA00000000000000000USR"


def test_mint_throttle_returns_429_on_eleventh_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.api.factory.load_scenario_fixture",
        lambda _scenario: {"default_start": "/tasks"},
    )
    monkeypatch.setattr("app.api.factory.normalise_start_path", lambda *_args: "/tasks")
    monkeypatch.setattr(
        "app.api.factory.load_bound_demo_workspace",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr("app.api.factory.seed_workspace", lambda *_a, **_k: _Seeded())
    monkeypatch.setattr("app.api.factory.mint_demo_cookie", lambda *_a, **_k: "cookie")
    monkeypatch.setattr("app.api.factory.make_uow", lambda: _fake_uow())

    client = TestClient(
        create_app(settings=_settings()),
        base_url="https://demo.crew.day",
    )

    for _ in range(10):
        assert (
            client.get(
                "/app?scenario=rental-manager", follow_redirects=False
            ).status_code
            == 303
        )

    response = client.get("/app?scenario=rental-manager", follow_redirects=False)

    assert response.status_code == 429
    assert response.json()["error"] == "rate_limited"


def test_payload_cap_rejects_large_demo_text_request() -> None:
    client = TestClient(
        create_app(settings=_settings()), base_url="https://demo.crew.day"
    )

    response = client.post(
        "/w/demo/api/v1/tasks/nl/preview",
        content=b"x" * (32 * 1024 + 1),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413


def test_upload_ip_daily_cap_returns_429() -> None:
    client = TestClient(
        create_app(
            settings=_settings(
                demo_max_upload_bytes=100,
                demo_upload_bytes_per_ip_per_day=15,
            )
        ),
        base_url="https://demo.crew.day",
    )
    headers = {"content-type": "multipart/form-data; boundary=x"}

    first = client.post(
        "/w/demo/api/v1/upload-test", content=b"x" * 10, headers=headers
    )
    second = client.post(
        "/w/demo/api/v1/upload-test", content=b"x" * 10, headers=headers
    )

    assert first.status_code == 404
    assert second.status_code == 429
    assert second.json()["type"].endswith("demo_upload_bytes_rate_limited")


def test_upload_workspace_count_cap_returns_429() -> None:
    client = TestClient(
        create_app(
            settings=_settings(
                demo_max_upload_bytes=100,
                demo_upload_bytes_per_ip_per_day=1_000,
                demo_uploads_per_workspace_lifetime=1,
            )
        ),
        base_url="https://demo.crew.day",
    )
    headers = {"content-type": "multipart/form-data; boundary=x"}

    first = client.post("/w/demo/api/v1/upload-test", content=b"x", headers=headers)
    second = client.post("/w/demo/api/v1/upload-test", content=b"x", headers=headers)

    assert first.status_code == 404
    assert second.status_code == 429
    assert second.json()["type"].endswith("demo_upload_count_rate_limited")


@contextmanager
def _fake_uow() -> Iterator[object]:
    yield object()


def _settings(**overrides: object) -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("demo-root-key"),
        demo_cookie_key=SecretStr("demo-cookie-key"),
        demo_mode=True,
        public_url="https://demo.crew.day",
        bind_host="127.0.0.1",
        worker="external",
        profile="prod",
        **overrides,
    )
