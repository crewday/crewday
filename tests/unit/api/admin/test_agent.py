"""Unit tests for :mod:`app.api.admin.agent`."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from tests.unit.api.admin._helpers import (
    build_client,
    engine_fixture,
    install_admin_cookie,
    settings_fixture,
)


@pytest.fixture
def settings() -> Settings:
    return settings_fixture("agent")


@pytest.fixture
def engine() -> Iterator[Engine]:
    yield from engine_fixture()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    yield from build_client(settings, session_factory, monkeypatch)


class TestAdminAgent:
    def test_log_and_actions_are_mounted_for_deployment_admin(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        log_resp = client.get("/admin/api/v1/agent/log")
        actions_resp = client.get("/admin/api/v1/agent/actions")

        assert log_resp.status_code == 200
        assert log_resp.json() == []
        assert actions_resp.status_code == 200
        assert actions_resp.json() == []

    def test_admin_agent_surface_keeps_admin_wall_for_non_admin(
        self,
        client: TestClient,
    ) -> None:
        resp = client.get("/admin/api/v1/agent/log")

        assert resp.status_code == 404

    def test_message_returns_sidebar_compatible_shape(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.post(
            "/admin/api/v1/agent/message",
            headers={"X-Agent-Page": "route=/admin/admins"},
            json={"body": "  show owners  "},
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["kind"] == "user"
        assert body["body"] == "show owners"
        assert body["channel_kind"] is None
        assert isinstance(body["at"], str)

    def test_unknown_action_decision_uses_not_found_shape(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        approve_resp = client.post("/admin/api/v1/agent/action/missing/approve")
        deny_resp = client.post("/admin/api/v1/agent/action/missing/deny")

        assert approve_resp.status_code == 404
        assert deny_resp.status_code == 404
