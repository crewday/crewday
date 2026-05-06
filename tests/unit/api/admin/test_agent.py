"""Unit tests for :mod:`app.api.admin.agent`."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.ops.models import AdminAgentAction, AdminAgentChatMessage
from app.api.transport import admin_sse
from app.auth.session import SESSION_COOKIE_NAME
from app.config import Settings
from app.domain.agent.runtime import (
    DelegatedToken,
    GateDecision,
    Tool,
    ToolCall,
    ToolResult,
)
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.unit.api.admin._helpers import (
    PINNED,
    build_client,
    engine_fixture,
    install_admin_cookie,
    seed_admin,
    settings_fixture,
)


class _FakeDispatcher:
    def __init__(self, *, result_status_code: int = 200) -> None:
        self.calls: list[tuple[ToolCall, Mapping[str, str]]] = []
        self.result_status_code = result_status_code

    @property
    def tools(self) -> Sequence[Tool]:
        return ()

    def is_gated(self, call: ToolCall) -> GateDecision:
        return GateDecision(gated=False)

    def dispatch(
        self,
        call: ToolCall,
        *,
        token: DelegatedToken,
        headers: Mapping[str, str],
    ) -> ToolResult:
        _ = token
        self.calls.append((call, headers))
        return ToolResult(
            call_id=call.id,
            status_code=self.result_status_code,
            body={"ok": True, "tool": call.name},
            mutated=self.result_status_code < 400,
        )


def _seed_action(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    action_id: str | None = None,
    state: str = "pending",
    inline_channel: str = "web_admin_sidebar",
    page_context: str = "route=/admin/settings",
) -> str:
    row_id = action_id or new_ulid()
    with session_factory() as session, tenant_agnostic():
        session.add(
            AdminAgentAction(
                id=row_id,
                requested_at=PINNED,
                requested_by_token_id="tok_agent",
                for_user_id=user_id,
                action="deployment.settings.edit",
                resolved_payload_json={"tool_input": {"setting": "budget"}},
                idempotency_key=f"idem_{row_id}",
                state=state,
                gate_source="workspace_always",
                card_summary="Update deployment budget",
                card_risk="high",
                card_fields_json={"Setting": "budget"},
                inline_channel=inline_channel,
                page_context=page_context,
            )
        )
        session.commit()
    return row_id


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

    def test_actions_returns_only_current_admin_pending_sidebar_cards(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id = install_admin_cookie(client, session_factory, settings)
        shown_id = _seed_action(session_factory, user_id=user_id)
        _seed_action(session_factory, user_id=user_id, state="rejected")
        _seed_action(session_factory, user_id=user_id, inline_channel="desk_only")
        other_id, _ = seed_admin(
            session_factory,
            settings=settings,
            email="other-actions@example.com",
            display_name="Other Actions",
        )
        _seed_action(session_factory, user_id=other_id)

        resp = client.get("/admin/api/v1/agent/actions")

        assert resp.status_code == 200
        assert resp.json() == [
            {
                "id": shown_id,
                "title": "Update deployment budget",
                "detail": "Update deployment budget",
                "risk": "high",
                "card_summary": "Update deployment budget",
                "card_fields": [["Setting", "budget"]],
                "gate_source": "workspace_always",
                "inline_channel": "web_admin_sidebar",
            }
        ]

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

    def test_approve_replays_action_then_persists_audit_and_is_retry_safe(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id = install_admin_cookie(client, session_factory, settings)
        action_id = _seed_action(session_factory, user_id=user_id)
        dispatcher = _FakeDispatcher()
        client.app.state.tool_dispatcher = dispatcher

        first = client.post(
            f"/admin/api/v1/agent/action/{action_id}/approve",
            headers={"X-Agent-Page": "route=/admin/usage"},
        )
        retry = client.post(f"/admin/api/v1/agent/action/{action_id}/approve")

        assert first.status_code == 200
        assert first.json() == {"ok": True}
        assert retry.status_code == 200
        assert retry.json() == {"ok": True}
        assert len(dispatcher.calls) == 1
        call, headers = dispatcher.calls[0]
        assert call.name == "deployment.settings.edit"
        assert call.input == {"setting": "budget"}
        assert headers["X-Agent-Page"] == "route=/admin/settings"
        assert headers["Idempotency-Key"].startswith("idem_")

        with session_factory() as session, tenant_agnostic():
            row = session.get(AdminAgentAction, action_id)
            assert row is not None
            assert row.state == "executed"
            assert row.decided_by_user_id == user_id
            assert row.executed_at is not None
            assert row.result_json == {
                "status_code": 200,
                "mutated": True,
                "body": {"ok": True, "tool": "deployment.settings.edit"},
            }
            audit = session.scalar(
                select(AuditLog).where(AuditLog.entity_id == action_id)
            )
            assert audit is not None
            assert audit.scope_kind == "deployment"
            assert audit.action == "admin_agent.action.approved"
            assert audit.actor_id == user_id
            assert audit.actor_kind == "user"
            assert audit.diff["page"] == "route=/admin/usage"
            assert audit.diff["requested_by_token_id"] == "tok_agent"

    def test_approve_without_dispatcher_fails_and_keeps_action_pending(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id = install_admin_cookie(client, session_factory, settings)
        action_id = _seed_action(session_factory, user_id=user_id)

        resp = client.post(f"/admin/api/v1/agent/action/{action_id}/approve")

        assert resp.status_code == 503
        assert resp.json()["error"] == "dispatcher_not_configured"
        with session_factory() as session, tenant_agnostic():
            row = session.get(AdminAgentAction, action_id)
            assert row is not None
            assert row.state == "pending"
            assert row.result_json is None

    def test_approve_replay_failure_keeps_action_pending(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id = install_admin_cookie(client, session_factory, settings)
        action_id = _seed_action(session_factory, user_id=user_id)
        dispatcher = _FakeDispatcher(result_status_code=404)
        client.app.state.tool_dispatcher = dispatcher

        resp = client.post(f"/admin/api/v1/agent/action/{action_id}/approve")

        assert resp.status_code == 503
        assert resp.json()["error"] == "admin_agent_action_replay_failed"
        assert len(dispatcher.calls) == 1
        with session_factory() as session, tenant_agnostic():
            row = session.get(AdminAgentAction, action_id)
            assert row is not None
            assert row.state == "pending"
            assert row.result_json is None

    def test_deny_records_decision_without_dispatching(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id = install_admin_cookie(client, session_factory, settings)
        action_id = _seed_action(session_factory, user_id=user_id)
        dispatcher = _FakeDispatcher()
        client.app.state.tool_dispatcher = dispatcher

        resp = client.post(
            f"/admin/api/v1/agent/action/{action_id}/deny",
            headers={"X-Agent-Page": "route=/admin/settings"},
        )

        assert resp.status_code == 200
        assert dispatcher.calls == []
        with session_factory() as session, tenant_agnostic():
            row = session.get(AdminAgentAction, action_id)
            assert row is not None
            assert row.state == "rejected"
            assert row.decided_by_user_id == user_id
            assert row.executed_at is None
            audit = session.scalar(
                select(AuditLog).where(AuditLog.entity_id == action_id)
            )
            assert audit is not None
            assert audit.action == "admin_agent.action.denied"
            assert audit.actor_id == user_id
            assert audit.actor_kind == "user"
            assert audit.diff["page"] == "route=/admin/settings"

    def test_cross_user_and_already_decided_actions_fail_safely(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        other_id, _ = seed_admin(
            session_factory,
            settings=settings,
            email="cross-user@example.com",
            display_name="Cross User",
        )
        cross_id = _seed_action(session_factory, user_id=other_id)
        decider_id, decider_cookie = seed_admin(
            session_factory,
            settings=settings,
            email="decider@example.com",
            display_name="Decider",
        )
        client.cookies.set(SESSION_COOKIE_NAME, decider_cookie)
        own_rejected = _seed_action(
            session_factory,
            user_id=decider_id,
            state="rejected",
        )
        own_executed = _seed_action(
            session_factory,
            user_id=decider_id,
            state="executed",
        )

        cross_resp = client.post(f"/admin/api/v1/agent/action/{cross_id}/deny")
        decided_resp = client.post(f"/admin/api/v1/agent/action/{own_rejected}/approve")
        executed_deny_resp = client.post(
            f"/admin/api/v1/agent/action/{own_executed}/deny"
        )

        assert cross_resp.status_code == 404
        assert cross_resp.json()["error"] == "admin_agent_action_not_found"
        assert decided_resp.status_code == 409
        assert decided_resp.json()["error"] == "admin_agent_action_not_pending"
        assert executed_deny_resp.status_code == 409
        assert executed_deny_resp.json()["error"] == "admin_agent_action_not_pending"
