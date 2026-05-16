"""Unit tests for :mod:`app.api.admin.agent`."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.capabilities.models import DeploymentSetting
from app.adapters.db.llm.models import ApprovalRequest, LlmModel
from app.adapters.db.ops.models import AdminAgentAction, AdminAgentChatMessage
from app.adapters.llm.fake import FakeLLMClient
from app.adapters.llm.openrouter import OPENROUTER_API_KEY_SETTING
from app.adapters.llm.ports import (
    ChatMessage as LlmChatMessage,
)
from app.adapters.llm.ports import (
    LLMResponse,
    LLMUsage,
)
from app.adapters.llm.ports import (
    Tool as LlmTool,
)
from app.adapters.llm.ports import (
    ToolCall as LlmToolCall,
)
from app.api.admin.agent import AdminAgentActionProposal, AdminAgentTextReply
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
from app.domain.llm.router import invalidate_cache
from app.fixtures.llm import seed_default_registry
from app.tenancy import tenant_agnostic
from app.util.redact import ConsentSet
from app.util.ulid import new_ulid
from tests.unit.api.admin._helpers import (
    PINNED,
    TEST_ACCEPT_LANGUAGE,
    TEST_UA,
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

    def activity_label_for(self, call: ToolCall) -> str:
        return call.name.replace(".", " ").capitalize()


class _FakeActionProducer:
    def __init__(
        self,
        proposal: AdminAgentActionProposal
        | AdminAgentTextReply
        | None
        | object = _DEFAULT_PROPOSAL,
    ) -> None:
        self.calls: list[tuple[str, str, str]] = []
        if proposal is _DEFAULT_PROPOSAL:
            self.proposal: AdminAgentActionProposal | AdminAgentTextReply | None
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
        request_headers: Mapping[str, str],
        ctx: object,
        session: Session,
    ) -> AdminAgentActionProposal | AdminAgentTextReply | None:
        _ = request_headers, session
        user_id = ctx.user_id
        assert isinstance(user_id, str)
        self.calls.append((message, page_context, user_id))
        return self.proposal


class _FailingActionProducer:
    def produce_action(
        self,
        *,
        message: str,
        page_context: str,
        request_headers: Mapping[str, str],
        ctx: object,
        session: Session,
    ) -> AdminAgentActionProposal:
        _ = message, page_context, request_headers, ctx, session
        raise RuntimeError("producer failed")


class _SequenceLLMClient:
    def __init__(self, responses: Sequence[LLMResponse]) -> None:
        self.responses = list(responses)
        self.messages: list[Sequence[LlmChatMessage]] = []
        self.tools: list[Sequence[LlmTool] | None] = []

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[LlmChatMessage],
        tools: Sequence[LlmTool] | None = None,
        consents: ConsentSet | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        thinking_level: object = "disabled",
        thinking_strategy: object = "none",
    ) -> LLMResponse:
        _ = consents, max_tokens, temperature, thinking_level, thinking_strategy
        self.messages.append(messages)
        self.tools.append(tools)
        if self.responses:
            return self.responses.pop(0)
        return _llm_response("Done.", model_id=model_id)


def _llm_response(
    text: str = "",
    *,
    model_id: str = "model_admin",
    tool_calls: tuple[LlmToolCall, ...] = (),
) -> LLMResponse:
    return LLMResponse(
        text=text,
        usage=LLMUsage(
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
        ),
        model_id=model_id,
        finish_reason="stop",
        tool_calls=tool_calls,
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


def _assert_admin_text_reply_write(
    session_factory: sessionmaker[Session],
    *,
    user_body: str,
    agent_body: str,
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
    assert (messages[1].kind, messages[1].body_md) == ("agent", agent_body)
    assert "Error ID:" not in messages[1].body_md
    assert actions == []
    assert approvals == []


def _seed_admin_model_default(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session, tenant_agnostic():
        seed_default_registry(session)
        session.commit()
    invalidate_cache()


def _admin_replay_headers(client: TestClient) -> dict[str, str]:
    cookie = client.cookies.get(SESSION_COOKIE_NAME)
    assert cookie is not None
    return {
        "Cookie": f"{SESSION_COOKIE_NAME}={cookie}",
        "User-Agent": TEST_UA,
        "Accept-Language": TEST_ACCEPT_LANGUAGE,
    }


def _install_workspace_openapi_route(client: TestClient) -> None:
    def _workspace_items(slug: str) -> dict[str, str]:
        return {"slug": slug}

    client.app.add_api_route(
        "/w/{slug}/api/v1/inventory/items",
        _workspace_items,
        methods=["GET"],
        operation_id="inventory.items.list",
    )
    client.app.openapi_schema = None


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

    def test_message_with_text_reply_records_user_and_agent_reply(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_id = install_admin_cookie(client, session_factory, settings)
        client.app.state.admin_agent_action_producer = _FakeActionProducer(
            AdminAgentTextReply("Hey. I can help with deployment admin tasks.")
        )
        published: list[dict[str, object]] = []
        monkeypatch.setattr(
            admin_sse,
            "publish_admin_event",
            lambda **kwargs: published.append(kwargs),
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            headers={"X-Agent-Page": "route=/admin/dashboard"},
            json={"body": "ello whats up"},
        )

        assert resp.status_code == 201
        assert resp.json()["kind"] == "user"
        assert resp.json()["body"] == "ello whats up"
        _assert_admin_text_reply_write(
            session_factory,
            user_body="ello whats up",
            agent_body="Hey. I can help with deployment admin tasks.",
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
        reply_payload = published[4]["payload"]
        assert isinstance(reply_payload, dict)
        reply_message = reply_payload["message"]
        assert isinstance(reply_message, dict)
        assert reply_message["kind"] == "agent"
        assert reply_message["body"] == "Hey. I can help with deployment admin tasks."
        finished_payload = published[5]["payload"]
        assert isinstance(finished_payload, dict)
        assert finished_payload["outcome"] == "replied"
        assert "error" not in finished_payload

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

    def test_runtime_producer_plain_text_records_agent_reply(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _seed_admin_model_default(session_factory)
        client.app.state.admin_agent_action_producer = AdminAgentRuntimeActionProducer(
            llm=FakeLLMClient(chat_text="Hello. What admin task should we look at?")
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            json={"body": "ello whats up"},
        )

        assert resp.status_code == 201
        assert resp.json()["kind"] == "user"
        assert resp.json()["body"] == "ello whats up"
        _assert_admin_text_reply_write(
            session_factory,
            user_body="ello whats up",
            agent_body="Hello. What admin task should we look at?",
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

    def test_runtime_producer_multiple_text_tool_calls_records_fallback(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _seed_admin_model_default(session_factory)
        with session_factory() as session:
            workspace_id = seed_workspace(session, slug="runtime-many-text-tools")
            session.commit()
        tool_text = (
            "<tool_call "
            f'name="admin.workspaces.trust" input=\'{{"id":"{workspace_id}"}}\'/>'
            "<tool_call "
            f'name="admin.workspaces.archive" input=\'{{"id":"{workspace_id}"}}\'/>'
        )
        client.app.state.admin_agent_action_producer = AdminAgentRuntimeActionProducer(
            llm=FakeLLMClient(chat_text=tool_text)
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            json={"body": "trust and archive that workspace"},
        )

        assert resp.status_code == 201
        assert resp.json()["kind"] == "user"
        assert resp.json()["body"] == "trust and archive that workspace"
        _assert_admin_runtime_fallback_write(
            session_factory,
            user_body="trust and archive that workspace",
        )

    def test_runtime_producer_rejects_out_of_catalog_dispatcher_tool(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _seed_admin_model_default(session_factory)
        _install_workspace_openapi_route(client)
        fallback = _FakeDispatcher()
        dispatcher = DeploymentAdminToolDispatcher(fallback=fallback, app=client.app)
        client.app.state.admin_agent_action_producer = AdminAgentRuntimeActionProducer(
            llm=FakeLLMClient(
                tool_calls=(
                    LlmToolCall(
                        id="call_workspace",
                        name="inventory.items.list",
                        arguments={},
                    ),
                )
            ),
            dispatcher=dispatcher,
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            json={"body": "list inventory items"},
        )

        assert resp.status_code == 201
        assert resp.json()["kind"] == "user"
        assert fallback.calls == []
        _assert_admin_runtime_fallback_write(
            session_factory,
            user_body="list inventory items",
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

    def test_deployment_admin_dispatcher_catalog_uses_admin_openapi_surface(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        dispatcher = DeploymentAdminToolDispatcher(app=client.app)

        tool_names = {tool["name"] for tool in dispatcher.tools}

        assert "admin.llm.models.create" in tool_names
        assert "admin.llm.provider_models.create" in tool_names
        assert "admin.llm.assignments.create" in tool_names
        assert "admin.llm.graph" in tool_names
        assert "admin.agent.message.create" not in tool_names
        assert "admin.agent.action.approve" not in tool_names
        assert "admin.agent.action.deny" not in tool_names
        assert "admin.llm.providers.key.set" not in tool_names
        assert "admin.llm.providers.key.clear" not in tool_names

    def test_runtime_producer_read_tool_loops_to_agent_reply(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _seed_admin_model_default(session_factory)
        llm = _SequenceLLMClient(
            (
                _llm_response(
                    tool_calls=(
                        LlmToolCall(
                            id="call_graph",
                            name="admin.llm.graph",
                            arguments={},
                        ),
                    )
                ),
                _llm_response("The LLM registry is available."),
            )
        )
        dispatcher = DeploymentAdminToolDispatcher(app=client.app)
        client.app.state.admin_agent_action_producer = AdminAgentRuntimeActionProducer(
            llm=llm, dispatcher=dispatcher
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            headers={"X-Agent-Page": "route=/admin/llm"},
            json={"body": "show me the llm graph"},
        )

        assert resp.status_code == 201
        _assert_admin_text_reply_write(
            session_factory,
            user_body="show me the llm graph",
            agent_body="The LLM registry is available.",
        )
        with session_factory() as session, tenant_agnostic():
            assert session.scalars(select(AdminAgentAction)).all() == []
        assert len(llm.messages) == 2
        tool_names = {tool["name"] for tool in llm.tools[0] or ()}
        assert "admin.llm.graph" in tool_names
        assert "admin.llm.models.create" in tool_names
        result_content = llm.messages[1][-1]["content"]
        assert isinstance(result_content, str)
        assert '"name": "admin.llm.graph"' in result_content
        assert '"status": 200' in result_content

    def test_runtime_producer_generic_llm_model_create_creates_pending_action(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id = install_admin_cookie(client, session_factory, settings)
        _seed_admin_model_default(session_factory)
        dispatcher = DeploymentAdminToolDispatcher(app=client.app)
        payload = {
            "canonical_name": "agent/test-model",
            "display_name": "Agent Test Model",
            "capabilities": ["chat"],
        }
        client.app.state.admin_agent_action_producer = AdminAgentRuntimeActionProducer(
            llm=FakeLLMClient(
                tool_calls=(
                    LlmToolCall(
                        id="call_model_create",
                        name="admin.llm.models.create",
                        arguments=payload,
                    ),
                )
            ),
            dispatcher=dispatcher,
        )

        resp = client.post(
            "/admin/api/v1/agent/message",
            headers={"X-Agent-Page": "route=/admin/llm"},
            json={"body": "create model agent/test-model"},
        )

        assert resp.status_code == 201
        with session_factory() as session, tenant_agnostic():
            rows = session.scalars(select(AdminAgentAction)).all()
            created = session.scalar(
                select(LlmModel).where(LlmModel.canonical_name == "agent/test-model")
            )
        assert created is None
        assert len(rows) == 1
        row = rows[0]
        assert row.for_user_id == user_id
        assert row.action == "admin.llm.models.create"
        assert row.resolved_payload_json == {
            "tool_call_id": "call_model_create",
            "tool_input": payload,
        }
        assert row.card_summary == "admin.llm.models.create requires confirmation."
        assert row.card_risk == "medium"

    def test_deployment_admin_dispatcher_read_route_uses_fastapi_deps(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        dispatcher = DeploymentAdminToolDispatcher(app=client.app)

        result = dispatcher.dispatch(
            ToolCall(id="call_graph", name="admin.llm.graph", input={}),
            token=DelegatedToken(plaintext="mip_test", token_id="tok_test"),
            headers=_admin_replay_headers(client),
        )

        assert result.status_code == 200, result.body
        assert result.mutated is False
        assert isinstance(result.body, dict)
        assert "providers" in result.body
        assert "models" in result.body

    def test_deployment_admin_dispatcher_write_route_uses_fastapi_deps(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id, cookie = seed_admin(
            session_factory,
            settings=settings,
            email="dispatcher-owner@example.com",
            display_name="Dispatcher Owner",
            owner=True,
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        dispatcher = DeploymentAdminToolDispatcher(app=client.app)

        result = dispatcher.dispatch(
            ToolCall(
                id="call_setting",
                name="admin.settings.update",
                input={"key": "signup_enabled", "value": False},
            ),
            token=DelegatedToken(plaintext="mip_test", token_id="tok_test"),
            headers=_admin_replay_headers(client),
        )

        assert result.status_code == 200, result.body
        assert result.mutated is True
        assert isinstance(result.body, dict)
        assert result.body["key"] == "signup_enabled"
        assert result.body["value"] is False
        with session_factory() as session, tenant_agnostic():
            setting = session.get(DeploymentSetting, "signup_enabled")
        assert setting is not None
        assert setting.value is False
        assert setting.updated_by == user_id

    def test_deployment_admin_dispatcher_forbidden_admin_tools_do_not_mutate(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        dispatcher = DeploymentAdminToolDispatcher(app=client.app)

        self_recursive = dispatcher.dispatch(
            ToolCall(id="call_agent", name="admin.agent.message.create", input={}),
            token=DelegatedToken(plaintext="mip_test", token_id="tok_test"),
            headers=_admin_replay_headers(client),
        )
        interactive = dispatcher.dispatch(
            ToolCall(
                id="call_key",
                name="admin.llm.providers.key.set",
                input={"provider_id": "provider_1", "api_key": "sk-secret"},
            ),
            token=DelegatedToken(plaintext="mip_test", token_id="tok_test"),
            headers=_admin_replay_headers(client),
        )
        unknown = dispatcher.dispatch(
            ToolCall(id="call_unknown", name="admin.nope", input={}),
            token=DelegatedToken(plaintext="mip_test", token_id="tok_test"),
            headers=_admin_replay_headers(client),
        )

        assert self_recursive.status_code == 403
        assert self_recursive.mutated is False
        assert interactive.status_code == 403
        assert interactive.mutated is False
        assert unknown.status_code == 404
        assert unknown.mutated is False
        _assert_no_agent_writes(session_factory)

    def test_deployment_admin_dispatcher_fails_closed_for_admin_non_admin_tool(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _install_workspace_openapi_route(client)
        fallback = _FakeDispatcher()
        dispatcher = DeploymentAdminToolDispatcher(fallback=fallback, app=client.app)
        headers = {
            **_admin_replay_headers(client),
            "X-Agent-Channel": "web_admin_sidebar",
        }

        result = dispatcher.dispatch(
            ToolCall(id="call_workspace", name="inventory.items.list", input={}),
            token=DelegatedToken(plaintext="mip_test", token_id="tok_test"),
            headers=headers,
        )

        assert result.status_code == 403
        assert result.mutated is False
        assert fallback.calls == []
        _assert_no_agent_writes(session_factory)

    def test_deployment_admin_dispatcher_preserves_fallback_for_non_admin_replay(
        self,
        client: TestClient,
    ) -> None:
        _install_workspace_openapi_route(client)
        fallback = _FakeDispatcher()
        dispatcher = DeploymentAdminToolDispatcher(fallback=fallback, app=client.app)

        result = dispatcher.dispatch(
            ToolCall(id="call_workspace", name="inventory.items.list", input={}),
            token=DelegatedToken(plaintext="mip_test", token_id="tok_test"),
            headers={"X-Agent-Channel": "approval-replay"},
        )

        assert result.status_code == 200
        assert result.body == {"ok": True, "tool": "inventory.items.list"}
        assert result.mutated is True
        assert [call.name for call, _headers in fallback.calls] == [
            "inventory.items.list"
        ]
        assert (
            dispatcher.activity_label_for(
                ToolCall(id="call_workspace", name="inventory.items.list", input={})
            )
            == "Inventory items list"
        )

    def test_deployment_admin_dispatcher_forbids_secret_routes_after_metadata_drift(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        schema = deepcopy(client.app.openapi())
        operation = schema["paths"]["/admin/api/v1/llm/providers/{provider_id}/key"][
            "put"
        ]
        operation.pop("x-interactive-only", None)
        client.app.openapi_schema = schema
        dispatcher = DeploymentAdminToolDispatcher(app=client.app)

        tool_names = {tool["name"] for tool in dispatcher.tools}
        result = dispatcher.dispatch(
            ToolCall(
                id="call_key",
                name="admin.llm.providers.key.set",
                input={"provider_id": "provider_1", "api_key": "sk-secret"},
            ),
            token=DelegatedToken(plaintext="mip_test", token_id="tok_test"),
            headers=_admin_replay_headers(client),
        )

        assert "admin.llm.providers.key.set" not in tool_names
        assert result.status_code == 403
        assert result.mutated is False
        _assert_no_agent_writes(session_factory)

    def test_deployment_admin_dispatcher_rejects_invalid_cap_before_mutation(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        dispatcher = DeploymentAdminToolDispatcher(app=client.app)

        result = dispatcher.dispatch(
            ToolCall(
                id="call_cap",
                name="admin.usage.workspaces.cap",
                input={"id": "ws_123", "cap_cents_30d": -1},
            ),
            token=DelegatedToken(plaintext="mip_test", token_id="tok_test"),
            headers=_admin_replay_headers(client),
        )

        assert result.status_code == 422
        assert result.mutated is False

    def test_deployment_admin_dispatcher_rejects_secret_setting_replay(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id, cookie = seed_admin(
            session_factory,
            settings=settings,
            email="secret-setting-owner@example.com",
            display_name="Secret Setting Owner",
            owner=True,
        )
        _ = user_id
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
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
            headers=_admin_replay_headers(client),
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
        settings: Settings,
        key: str,
        value: object,
    ) -> None:
        user_id, cookie = seed_admin(
            session_factory,
            settings=settings,
            email=f"invalid-{key.replace('_', '-')}@example.com",
            display_name="Invalid Setting Owner",
            owner=True,
        )
        _ = user_id
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        dispatcher = DeploymentAdminToolDispatcher(app=client.app)

        result = dispatcher.dispatch(
            ToolCall(
                id="call_invalid_setting",
                name="admin.settings.update",
                input={"key": key, "value": value},
            ),
            token=DelegatedToken(plaintext="mip_test", token_id="tok_test"),
            headers=_admin_replay_headers(client),
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
