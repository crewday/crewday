"""Unit tests for :mod:`app.api.admin.agent`."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.ops.models import AdminAgentChatMessage
from app.api.transport import admin_sse
from app.auth.session import SESSION_COOKIE_NAME
from app.config import Settings
from app.tenancy import tenant_agnostic
from tests.unit.api.admin._helpers import (
    build_client,
    engine_fixture,
    install_admin_cookie,
    seed_admin,
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

    def test_message_persists_and_returns_sidebar_compatible_shape(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id = install_admin_cookie(client, session_factory, settings)
        published: list[dict[str, object]] = []
        monkeypatch.setattr(
            admin_sse,
            "publish_admin_event",
            lambda **kwargs: published.append(kwargs),
        )

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

        reload_resp = client.get("/admin/api/v1/agent/log")
        assert reload_resp.status_code == 200
        assert reload_resp.json() == [body]

        with session_factory() as session, tenant_agnostic():
            row = session.scalar(
                select(AdminAgentChatMessage).where(
                    AdminAgentChatMessage.admin_user_id == user_id
                )
            )
            assert row is not None
            assert row.body_md == "show owners"
            assert row.page_context == "route=/admin/admins"
            audit = session.scalar(select(AuditLog).where(AuditLog.entity_id == row.id))
            assert audit is not None
            assert audit.scope_kind == "deployment"
            assert audit.action == "admin_agent.message.sent"
            assert audit.diff["capability"] == "chat.admin"
            assert audit.diff["page"] == "route=/admin/admins"

        assert [event["kind"] for event in published] == [
            "admin.audit.appended",
            "agent.turn.started",
            "agent.message.appended",
            "agent.turn.finished",
        ]
        assert published[1]["user_scope"] == user_id
        assert published[2]["user_scope"] == user_id
        assert published[3]["user_scope"] == user_id
        message_payload = published[2]["payload"]
        assert isinstance(message_payload, dict)
        assert message_payload["scope"] == "admin"
        assert message_payload["message"] == body

    def test_log_is_oldest_first_and_scoped_to_current_admin(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        first = client.post("/admin/api/v1/agent/message", json={"body": "first"})
        second = client.post("/admin/api/v1/agent/message", json={"body": "second"})

        assert first.status_code == 201
        assert second.status_code == 201
        log_resp = client.get("/admin/api/v1/agent/log")

        assert log_resp.status_code == 200
        assert [item["body"] for item in log_resp.json()] == ["first", "second"]

        _, other_cookie = seed_admin(
            session_factory,
            settings=settings,
            email="grace@example.com",
            display_name="Grace",
        )
        client.cookies.set(SESSION_COOKIE_NAME, other_cookie)

        other_log_resp = client.get("/admin/api/v1/agent/log")

        assert other_log_resp.status_code == 200
        assert other_log_resp.json() == []

    def test_actions_are_empty_until_deployment_replay_model_exists(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/agent/actions")

        assert resp.status_code == 200
        assert resp.json() == []

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
