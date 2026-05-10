"""Unit tests for :mod:`app.api.admin.agent`."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.capabilities.models import DeploymentSetting
from app.adapters.db.llm.models import ApprovalRequest, LlmProvider
from app.adapters.db.ops.models import AdminAgentAction, AdminAgentChatMessage
from app.adapters.llm.fake import FakeLLMClient
from app.adapters.llm.openrouter import OPENROUTER_API_KEY_SETTING
from app.adapters.llm.ports import ToolCall as LlmToolCall
from app.api.admin.agent import AdminAgentActionProposal
from app.api.admin.agent_runtime import (
    AdminAgentRuntimeActionProducer,
    DeploymentAdminToolDispatcher,
)
from app.api.transport import admin_sse
from app.auth.session import SESSION_COOKIE_NAME
from app.capabilities import probe as probe_capabilities
from app.config import Settings
from app.domain.agent.runtime import (
    DelegatedToken,
    GateDecision,
    Tool,
    ToolCall,
    ToolResult,
)
from app.fixtures.llm import DEFAULT_PROVIDER_NAME, seed_default_registry
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.unit.api.admin._helpers import (
    PINNED,
    build_client,
    engine_fixture,
    install_admin_cookie,
    seed_admin,
    seed_workspace,
    settings_fixture,
)

_DEFAULT_PROPOSAL = object()


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


class _FakeActionProducer:
    def __init__(
        self,
        proposal: AdminAgentActionProposal | None | object = _DEFAULT_PROPOSAL,
    ) -> None:
        self.calls: list[tuple[str, str, str]] = []
        if proposal is _DEFAULT_PROPOSAL:
            self.proposal = AdminAgentActionProposal(
                tool_call=ToolCall(
                    id="call_deployment_settings_edit",
                    name="deployment.settings.edit",
                    input={"setting": "root_llm_budget", "value": "200"},
                ),
                card_summary="Update root LLM budget",
                card_fields=(("Setting", "root_llm_budget"), ("Value", "200")),
                card_risk="high",
                gate_source="workspace_always",
                requested_by_token_id="tok_live_admin_agent",
            )
        else:
            self.proposal = proposal

    def produce_action(
        self,
        *,
        message: str,
        page_context: str,
        ctx: object,
        session: Session,
    ) -> AdminAgentActionProposal | None:
        _ = session
        user_id = ctx.user_id
        assert isinstance(user_id, str)
        self.calls.append((message, page_context, user_id))
        assert self.proposal is None or isinstance(
            self.proposal,
            AdminAgentActionProposal,
        )
        return self.proposal


class _FailingActionProducer:
    def produce_action(
        self,
        *,
        message: str,
        page_context: str,
        ctx: object,
        session: Session,
    ) -> AdminAgentActionProposal:
        _ = message, page_context, ctx, session
        raise RuntimeError("producer failed")


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


def _assert_no_agent_writes(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session, tenant_agnostic():
        assert session.scalars(select(AdminAgentChatMessage)).all() == []
        assert session.scalars(select(AdminAgentAction)).all() == []
        assert session.scalars(select(ApprovalRequest)).all() == []


def _assert_admin_runtime_fallback_write(
    session_factory: sessionmaker[Session],
    *,
    user_body: str,
) -> None:
    with session_factory() as session, tenant_agnostic():
        messages = session.scalars(
            select(AdminAgentChatMessage).order_by(
                AdminAgentChatMessage.created_at.asc(),
                AdminAgentChatMessage.id.asc(),
            )
        ).all()
        actions = session.scalars(select(AdminAgentAction)).all()
        approvals = session.scalars(select(ApprovalRequest)).all()
    assert len(messages) == 2
    assert (messages[0].kind, messages[0].body_md) == ("user", user_body)
    assert messages[1].kind == "agent"
    assert (
        "The admin agent cannot propose an action right now because its chat "
        "runtime is not configured or did not return a supported action."
        in messages[1].body_md
    )
    assert "Error ID:" in messages[1].body_md
    assert actions == []
    assert approvals == []


def _seed_admin_model_default(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session, tenant_agnostic():
        provider_model = seed_default_registry(session)
        provider = session.scalar(
            select(LlmProvider).where(LlmProvider.name == DEFAULT_PROVIDER_NAME)
        )
        assert provider is not None
        provider.default_model = provider_model.id
        session.commit()


@contextmanager
def _session_uow(
    session_factory: sessionmaker[Session],
) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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
        producer = _FakeActionProducer()
        client.app.state.admin_agent_action_producer = producer
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
        assert producer.calls == [
            ("show owners", "route=/admin/admins", user_id),
        ]

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
            "agent.turn.started",
            "admin.audit.appended",
            "agent.message.appended",
            "admin.audit.appended",
            "agent.action.pending",
            "agent.turn.finished",
        ]
        assert published[0]["user_scope"] == user_id
        assert published[2]["user_scope"] == user_id
        assert published[4]["user_scope"] == user_id
        assert published[5]["user_scope"] == user_id
        message_payload = published[2]["payload"]
        assert isinstance(message_payload, dict)
        assert message_payload["scope"] == "admin"
        assert message_payload["message"] == body
        pending_payload = published[4]["payload"]
        assert isinstance(pending_payload, dict)
        assert pending_payload["scope"] == "admin"
        assert pending_payload["actor_user_id"] == user_id
        assert pending_payload["thread_id"] is None
        finished_payload = published[5]["payload"]
        assert isinstance(finished_payload, dict)
        assert finished_payload["outcome"] == "action"

    def test_log_is_oldest_first_and_scoped_to_current_admin(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        client.app.state.admin_agent_action_producer = _FakeActionProducer()

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

    def test_message_without_action_producer_records_user_and_fallback(
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
            headers={"X-Agent-Page": "route=/admin/settings"},
            json={"body": "change the budget"},
        )

        assert resp.status_code == 201
        assert resp.json()["kind"] == "user"
        assert resp.json()["body"] == "change the budget"
        _assert_admin_runtime_fallback_write(
            session_factory,
            user_body="change the budget",
        )
        assert [event["kind"] for event in published] == [
            "agent.turn.started",
            "admin.audit.appended",
            "agent.message.appended",
            "admin.audit.appended",
            "agent.message.appended",
            "agent.turn.finished",
        ]
        assert published[0]["user_scope"] == user_id
        assert published[2]["user_scope"] == user_id
        assert published[4]["user_scope"] == user_id
        assert published[5]["user_scope"] == user_id
        fallback_payload = published[4]["payload"]
        assert isinstance(fallback_payload, dict)
        fallback_message = fallback_payload["message"]
        assert isinstance(fallback_message, dict)
        assert fallback_message["kind"] == "agent"
        assert (
            "admin agent cannot propose an action right now" in fallback_message["body"]
        )
        finished_payload = published[5]["payload"]
        assert isinstance(finished_payload, dict)
        assert finished_payload["outcome"] == "error"
        assert finished_payload["error"] == "admin_agent_runtime_unwired"

    def test_message_without_action_proposal_records_user_and_fallback(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        client.app.state.admin_agent_action_producer = _FakeActionProducer(None)
        published: list[dict[str, object]] = []
        monkeypatch.setattr(
            admin_sse,
            "publish_admin_event",
            lambda **kwargs: published.append(kwargs),
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            headers={"X-Agent-Page": "route=/admin/settings"},
            json={"body": "say something that has no tool call"},
        )

        assert resp.status_code == 201
        assert resp.json()["kind"] == "user"
        assert resp.json()["body"] == "say something that has no tool call"
        _assert_admin_runtime_fallback_write(
            session_factory,
            user_body="say something that has no tool call",
        )
        assert [event["kind"] for event in published] == [
            "agent.turn.started",
            "admin.audit.appended",
            "agent.message.appended",
            "admin.audit.appended",
            "agent.message.appended",
            "agent.turn.finished",
        ]
        finished_payload = published[5]["payload"]
        assert isinstance(finished_payload, dict)
        assert finished_payload["outcome"] == "error"
        assert finished_payload["error"] == "admin_agent_no_action_proposal"

    def test_message_with_invalid_action_proposal_fails_closed_without_writes(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        client.app.state.admin_agent_action_producer = _FakeActionProducer(
            AdminAgentActionProposal(
                tool_call=ToolCall(
                    id="call_invalid",
                    name="deployment.settings.edit",
                    input={"setting": {"not_jsonable"}},
                ),
                card_summary="Update root LLM budget",
                card_fields=(("Setting", "root_llm_budget"),),
                card_risk="high",
                gate_source="workspace_always",
            )
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            json={"body": "raise root llm budget"},
        )

        assert resp.status_code == 503
        assert resp.json()["error"] == "admin_agent_action_proposal_invalid"
        _assert_no_agent_writes(session_factory)

    def test_message_when_action_producer_errors_records_user_and_fallback(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        client.app.state.admin_agent_action_producer = _FailingActionProducer()

        resp = client.post(
            "/admin/api/v1/agent/message",
            json={"body": "raise root llm budget"},
        )

        assert resp.status_code == 201
        assert resp.json()["kind"] == "user"
        assert resp.json()["body"] == "raise root llm budget"
        _assert_admin_runtime_fallback_write(
            session_factory,
            user_body="raise root llm budget",
        )

    def test_runtime_producer_model_unavailable_records_user_and_fallback(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        client.app.state.admin_agent_action_producer = AdminAgentRuntimeActionProducer(
            llm=FakeLLMClient(
                tool_calls=(
                    LlmToolCall(
                        id="call_cap",
                        name="admin.usage.workspaces.cap",
                        arguments={"id": "ws_missing", "cap_cents_30d": 2000},
                    ),
                )
            )
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            json={"body": "raise that workspace cap"},
        )

        assert resp.status_code == 201
        assert resp.json()["kind"] == "user"
        assert resp.json()["body"] == "raise that workspace cap"
        _assert_admin_runtime_fallback_write(
            session_factory,
            user_body="raise that workspace cap",
        )

    def test_runtime_producer_supported_tool_creates_pending_admin_action(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id = install_admin_cookie(client, session_factory, settings)
        _seed_admin_model_default(session_factory)
        with session_factory() as session:
            workspace_id = seed_workspace(
                session,
                slug="runtime-admin",
                name="Runtime Admin",
            )
            session.commit()
        client.app.state.admin_agent_action_producer = AdminAgentRuntimeActionProducer(
            llm=FakeLLMClient(
                tool_calls=(
                    LlmToolCall(
                        id="call_cap",
                        name="admin.usage.workspaces.cap",
                        arguments={"id": workspace_id, "cap_cents_30d": 2000},
                    ),
                )
            )
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            headers={"X-Agent-Page": "route=/admin/usage; params=ws=runtime-admin"},
            json={"body": "raise that workspace cap"},
        )

        assert resp.status_code == 201
        with session_factory() as session, tenant_agnostic():
            rows = session.scalars(select(AdminAgentAction)).all()
            approvals = session.scalars(select(ApprovalRequest)).all()
        assert len(rows) == 1
        assert approvals == []
        row = rows[0]
        assert row.for_user_id == user_id
        assert row.action == "admin.usage.workspaces.cap"
        assert row.resolved_payload_json == {
            "tool_call_id": "call_cap",
            "tool_input": {"id": workspace_id, "cap_cents_30d": 2000},
        }
        assert row.card_summary == "Update workspace LLM budget cap"
        assert row.card_fields_json == [
            ["Workspace", workspace_id],
            ["Cap", "2000"],
        ]
        assert row.card_risk == "high"

    def test_runtime_producer_missing_workspace_records_user_and_fallback(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _seed_admin_model_default(session_factory)
        client.app.state.admin_agent_action_producer = AdminAgentRuntimeActionProducer(
            llm=FakeLLMClient(
                tool_calls=(
                    LlmToolCall(
                        id="call_cap",
                        name="admin.usage.workspaces.cap",
                        arguments={"id": "ws_missing", "cap_cents_30d": 2000},
                    ),
                )
            )
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            json={"body": "raise that workspace cap"},
        )

        assert resp.status_code == 201
        assert resp.json()["kind"] == "user"
        assert resp.json()["body"] == "raise that workspace cap"
        _assert_admin_runtime_fallback_write(
            session_factory,
            user_body="raise that workspace cap",
        )

    def test_runtime_producer_negative_cap_records_user_and_fallback(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _seed_admin_model_default(session_factory)
        with session_factory() as session:
            workspace_id = seed_workspace(session, slug="runtime-negative-cap")
            session.commit()
        client.app.state.admin_agent_action_producer = AdminAgentRuntimeActionProducer(
            llm=FakeLLMClient(
                tool_calls=(
                    LlmToolCall(
                        id="call_cap",
                        name="admin.usage.workspaces.cap",
                        arguments={"id": workspace_id, "cap_cents_30d": -1},
                    ),
                )
            )
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            json={"body": "set that workspace cap below zero"},
        )

        assert resp.status_code == 201
        assert resp.json()["kind"] == "user"
        assert resp.json()["body"] == "set that workspace cap below zero"
        _assert_admin_runtime_fallback_write(
            session_factory,
            user_body="set that workspace cap below zero",
        )

    def test_runtime_producer_unsupported_tool_records_user_and_fallback(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _seed_admin_model_default(session_factory)
        client.app.state.admin_agent_action_producer = AdminAgentRuntimeActionProducer(
            llm=FakeLLMClient(
                tool_calls=(
                    LlmToolCall(
                        id="call_read",
                        name="admin.usage.summary",
                        arguments={},
                    ),
                )
            )
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            json={"body": "show usage"},
        )

        assert resp.status_code == 201
        assert resp.json()["kind"] == "user"
        assert resp.json()["body"] == "show usage"
        _assert_admin_runtime_fallback_write(session_factory, user_body="show usage")

    def test_runtime_producer_supported_non_secret_setting_creates_action(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _seed_admin_model_default(session_factory)
        client.app.state.admin_agent_action_producer = AdminAgentRuntimeActionProducer(
            llm=FakeLLMClient(
                tool_calls=(
                    LlmToolCall(
                        id="call_settings",
                        name="admin.settings.update",
                        arguments={"key": "signup_enabled", "value": True},
                    ),
                )
            )
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            json={"body": "turn signup on"},
        )

        assert resp.status_code == 201
        with session_factory() as session, tenant_agnostic():
            rows = session.scalars(select(AdminAgentAction)).all()
            approvals = session.scalars(select(ApprovalRequest)).all()
        assert len(rows) == 1
        assert approvals == []
        row = rows[0]
        assert row.action == "admin.settings.update"
        assert row.resolved_payload_json == {
            "tool_call_id": "call_settings",
            "tool_input": {"key": "signup_enabled", "value": True},
        }
        assert row.card_summary == "Update deployment setting"
        assert row.card_fields_json == [
            ["Setting", "signup_enabled"],
            ["Value", "true"],
        ]
        assert row.card_risk == "medium"

    def test_runtime_producer_secret_setting_records_user_and_no_action_payload(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _seed_admin_model_default(session_factory)
        client.app.state.admin_agent_action_producer = AdminAgentRuntimeActionProducer(
            llm=FakeLLMClient(
                tool_calls=(
                    LlmToolCall(
                        id="call_secret",
                        name="admin.settings.update",
                        arguments={
                            "key": OPENROUTER_API_KEY_SETTING,
                            "value": "sk-plaintext-must-not-persist",
                        },
                    ),
                )
            )
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            json={"body": "set the OpenRouter key"},
        )

        assert resp.status_code == 201
        assert resp.json()["kind"] == "user"
        assert resp.json()["body"] == "set the OpenRouter key"
        _assert_admin_runtime_fallback_write(
            session_factory,
            user_body="set the OpenRouter key",
        )

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            (OPENROUTER_API_KEY_SETTING, "sk-plaintext-must-not-persist"),
            ("trusted_interfaces", ["lo"]),
            ("signup_enabled", "definitely-not-a-bool"),
        ],
    )
    def test_invalid_settings_action_proposal_fails_before_persisting_payload(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        key: str,
        value: object,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        client.app.state.admin_agent_action_producer = _FakeActionProducer(
            AdminAgentActionProposal(
                tool_call=ToolCall(
                    id="call_bad_setting",
                    name="admin.settings.update",
                    input={"key": key, "value": value},
                ),
                card_summary="Update deployment setting",
                card_fields=(("Setting", key), ("Value", str(value))),
                card_risk="medium",
                gate_source="workspace_always",
                requested_by_token_id="tok_live_admin_agent",
            )
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            json={"body": "change deployment setting"},
        )

        assert resp.status_code == 503
        assert resp.json()["error"] == "admin_agent_action_proposal_invalid"
        _assert_no_agent_writes(session_factory)

    def test_runtime_producer_multiple_tool_calls_records_user_and_fallback(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _seed_admin_model_default(session_factory)
        with session_factory() as session:
            workspace_id = seed_workspace(session, slug="runtime-many-tools")
            session.commit()
        client.app.state.admin_agent_action_producer = AdminAgentRuntimeActionProducer(
            llm=FakeLLMClient(
                tool_calls=(
                    LlmToolCall(
                        id="call_cap",
                        name="admin.usage.workspaces.cap",
                        arguments={"id": workspace_id, "cap_cents_30d": 2000},
                    ),
                    LlmToolCall(
                        id="call_trust",
                        name="admin.workspaces.trust",
                        arguments={"id": workspace_id},
                    ),
                )
            )
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            json={"body": "raise the cap and trust it"},
        )

        assert resp.status_code == 201
        assert resp.json()["kind"] == "user"
        assert resp.json()["body"] == "raise the cap and trust it"
        _assert_admin_runtime_fallback_write(
            session_factory,
            user_body="raise the cap and trust it",
        )

    def test_runtime_producer_without_tools_fails_closed_without_writes(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _seed_admin_model_default(session_factory)
        client.app.state.admin_agent_action_producer = AdminAgentRuntimeActionProducer(
            llm=FakeLLMClient(),
            tools=(),
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            json={"body": "trust that workspace"},
        )

        assert resp.status_code == 503
        assert resp.json()["error"] == "dispatcher_not_configured"
        _assert_no_agent_writes(session_factory)

    def test_deployment_admin_dispatcher_rejects_invalid_cap_before_mutation(
        self,
    ) -> None:
        dispatcher = DeploymentAdminToolDispatcher()

        result = dispatcher.dispatch(
            ToolCall(
                id="call_cap",
                name="admin.usage.workspaces.cap",
                input={"id": "ws_123", "cap_cents_30d": -1},
            ),
            token=DelegatedToken(plaintext="mip_test", token_id="tok_test"),
            headers={"X-Crewday-Replay-Actor-Id": "user_admin"},
        )

        assert result.status_code == 422
        assert result.body == {"error": "invalid_cap"}
        assert result.mutated is False

    def test_deployment_admin_dispatcher_rejects_secret_setting_replay(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        dispatcher = DeploymentAdminToolDispatcher(app=client.app)

        result = dispatcher.dispatch(
            ToolCall(
                id="call_secret_setting",
                name="admin.settings.update",
                input={
                    "key": OPENROUTER_API_KEY_SETTING,
                    "value": "sk-plaintext-must-not-replay",
                },
            ),
            token=DelegatedToken(plaintext="mip_test", token_id="tok_test"),
            headers={"X-Crewday-Replay-Actor-Id": "user_admin"},
        )

        assert result.status_code == 422
        assert result.body == {"error": "invalid_setting_value"}
        assert result.mutated is False
        with session_factory() as session, tenant_agnostic():
            assert session.get(DeploymentSetting, OPENROUTER_API_KEY_SETTING) is None

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("trusted_interfaces", ["lo"]),
            ("signup_enabled", "definitely-not-a-bool"),
        ],
    )
    def test_deployment_admin_dispatcher_rejects_root_and_invalid_setting_replay(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        key: str,
        value: object,
    ) -> None:
        dispatcher = DeploymentAdminToolDispatcher(app=client.app)

        result = dispatcher.dispatch(
            ToolCall(
                id="call_invalid_setting",
                name="admin.settings.update",
                input={"key": key, "value": value},
            ),
            token=DelegatedToken(plaintext="mip_test", token_id="tok_test"),
            headers={"X-Crewday-Replay-Actor-Id": "user_admin"},
        )

        assert result.status_code == 422
        assert result.body == {"error": "invalid_setting_value"}
        assert result.mutated is False
        with session_factory() as session, tenant_agnostic():
            assert session.scalars(select(DeploymentSetting)).all() == []

    def test_live_message_produces_one_scoped_pending_admin_action(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id = install_admin_cookie(client, session_factory, settings)
        client.app.state.admin_agent_action_producer = _FakeActionProducer()

        first = client.post(
            "/admin/api/v1/agent/message",
            headers={"X-Agent-Page": "route=/admin/settings; params=tab=llm"},
            json={"body": "raise root llm budget"},
        )
        retry = client.post(
            "/admin/api/v1/agent/message",
            headers={"X-Agent-Page": "route=/admin/settings; params=tab=llm"},
            json={"body": "raise root llm budget"},
        )

        assert first.status_code == 201
        assert retry.status_code == 201
        with session_factory() as session, tenant_agnostic():
            rows = session.scalars(select(AdminAgentAction)).all()
            approvals = session.scalars(select(ApprovalRequest)).all()
        assert len(rows) == 1
        assert approvals == []
        row = rows[0]
        assert row.for_user_id == user_id
        assert row.inline_channel == "web_admin_sidebar"
        assert row.page_context == "route=/admin/settings; params=tab=llm"
        assert row.idempotency_key.startswith("admin-agent:")
        assert row.action == "deployment.settings.edit"
        assert row.resolved_payload_json == {
            "tool_call_id": "call_deployment_settings_edit",
            "tool_input": {"setting": "root_llm_budget", "value": "200"},
        }
        assert row.card_summary == "Update root LLM budget"
        assert row.card_fields_json == [
            ["Setting", "root_llm_budget"],
            ["Value", "200"],
        ]
        assert row.card_risk == "high"
        assert row.gate_source == "workspace_always"
        assert row.requested_by_token_id == "tok_live_admin_agent"

        actions_resp = client.get("/admin/api/v1/agent/actions")
        assert actions_resp.status_code == 200
        assert [item["id"] for item in actions_resp.json()] == [row.id]

        _, other_cookie = seed_admin(
            session_factory,
            settings=settings,
            email="other-produced-action@example.com",
            display_name="Other Produced",
        )
        client.cookies.set(SESSION_COOKIE_NAME, other_cookie)
        other_resp = client.get("/admin/api/v1/agent/actions")
        assert other_resp.status_code == 200
        assert other_resp.json() == []

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

    def test_approve_replays_action_produced_by_live_message_once(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id = install_admin_cookie(client, session_factory, settings)
        client.app.state.admin_agent_action_producer = _FakeActionProducer()
        dispatcher = _FakeDispatcher()
        client.app.state.tool_dispatcher = dispatcher

        produced = client.post(
            "/admin/api/v1/agent/message",
            headers={"X-Agent-Page": "route=/admin/settings"},
            json={"body": "raise root llm budget"},
        )
        assert produced.status_code == 201
        with session_factory() as session, tenant_agnostic():
            action_id = session.scalar(select(AdminAgentAction.id))
            assert action_id is not None

        first = client.post(f"/admin/api/v1/agent/action/{action_id}/approve")
        retry = client.post(f"/admin/api/v1/agent/action/{action_id}/approve")

        assert first.status_code == 200
        assert retry.status_code == 200
        assert len(dispatcher.calls) == 1
        call, headers = dispatcher.calls[0]
        assert call.name == "deployment.settings.edit"
        assert call.id == "call_deployment_settings_edit"
        assert call.input == {"setting": "root_llm_budget", "value": "200"}
        assert headers["X-Agent-Page"] == "route=/admin/settings"
        with session_factory() as session, tenant_agnostic():
            row = session.get(AdminAgentAction, action_id)
            assert row is not None
            assert row.state == "executed"
            assert row.decided_by_user_id == user_id

    def test_approve_settings_action_writes_once_refreshes_and_publishes(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id, cookie = seed_admin(
            session_factory,
            settings=settings,
            email="settings-agent-owner@example.com",
            display_name="Settings Owner",
            owner=True,
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        client.app.state.admin_agent_action_producer = _FakeActionProducer(
            AdminAgentActionProposal(
                tool_call=ToolCall(
                    id="call_signup_enabled",
                    name="admin.settings.update",
                    input={"key": "signup_enabled", "value": False},
                ),
                card_summary="Update deployment setting",
                card_fields=(("Setting", "signup_enabled"), ("Value", "false")),
                card_risk="medium",
                gate_source="workspace_always",
                requested_by_token_id="tok_live_admin_agent",
            )
        )
        client.app.state.capabilities = probe_capabilities(settings)
        client.app.state.tool_dispatcher = DeploymentAdminToolDispatcher(app=client.app)
        monkeypatch.setattr(
            "app.adapters.db.session.make_uow",
            lambda: _session_uow(session_factory),
        )
        published: list[dict[str, object]] = []
        monkeypatch.setattr(
            admin_sse.default_admin_fanout,
            "publish",
            lambda **kwargs: published.append(kwargs),
        )

        produced = client.post(
            "/admin/api/v1/agent/message",
            headers={"X-Agent-Page": "route=/admin/settings"},
            json={"body": "turn signup off"},
        )
        assert produced.status_code == 201
        with session_factory() as session, tenant_agnostic():
            action_id = session.scalar(select(AdminAgentAction.id))
            assert action_id is not None
        published.clear()

        first = client.post(f"/admin/api/v1/agent/action/{action_id}/approve")
        retry = client.post(f"/admin/api/v1/agent/action/{action_id}/approve")

        assert first.status_code == 200, first.text
        assert retry.status_code == 200, retry.text
        with session_factory() as session, tenant_agnostic():
            setting = session.get(DeploymentSetting, "signup_enabled")
            setting_audits = session.scalars(
                select(AuditLog).where(AuditLog.action == "deployment_setting.updated")
            ).all()
            approvals = session.scalars(select(ApprovalRequest)).all()
        assert setting is not None
        assert setting.value is False
        assert setting.updated_by == user_id
        assert len(setting_audits) == 1
        assert setting_audits[0].actor_id == user_id
        assert setting_audits[0].actor_was_owner_member is True
        assert approvals == []
        assert client.app.state.capabilities.settings.signup_enabled is False
        assert [event["kind"] for event in published] == [
            "admin.audit.appended",
            "admin.settings.updated",
            "admin.audit.appended",
        ]
        assert published[1]["payload"]["key"] == "signup_enabled"

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

    def test_deny_produced_action_never_dispatches(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id = install_admin_cookie(client, session_factory, settings)
        client.app.state.admin_agent_action_producer = _FakeActionProducer()
        dispatcher = _FakeDispatcher()
        client.app.state.tool_dispatcher = dispatcher

        produced = client.post(
            "/admin/api/v1/agent/message",
            headers={"X-Agent-Page": "route=/admin/settings"},
            json={"body": "raise root llm budget"},
        )
        assert produced.status_code == 201
        with session_factory() as session, tenant_agnostic():
            action_id = session.scalar(select(AdminAgentAction.id))
            assert action_id is not None

        resp = client.post(f"/admin/api/v1/agent/action/{action_id}/deny")

        assert resp.status_code == 200
        assert dispatcher.calls == []
        with session_factory() as session, tenant_agnostic():
            row = session.get(AdminAgentAction, action_id)
            assert row is not None
            assert row.state == "rejected"
            assert row.decided_by_user_id == user_id
            assert row.executed_at is None

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
