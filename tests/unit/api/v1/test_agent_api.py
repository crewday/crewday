"""HTTP-boundary tests for workspace-scoped agent chat endpoints."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, event, select
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import ExecutionContext
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import JSONResponse

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.llm.models import (
    AgentRelayRequest,
    BudgetLedger,
    LlmAssignment,
    LlmModel,
    LlmProvider,
    LlmProviderModel,
)
from app.adapters.db.messaging.models import ChatChannel, ChatMessage, Notification
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import UserWorkspace, WorkEngagement, Workspace
from app.adapters.llm.ports import LLMResponse, LLMUsage
from app.api.deps import current_workspace_context, db_session
from app.api.deps import get_llm as get_llm_dep
from app.api.errors import _handle_domain_error
from app.api.factory import create_app
from app.api.v1.agent import build_agent_router, get_agent_token_factory
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.domain.agent.runtime import DelegatedToken
from app.domain.errors import DomainError
from app.domain.llm.router import invalidate_cache as invalidate_llm_router_cache
from app.events.bus import EventBus
from app.events.types import AgentMessageAppended, ChatMessageSent
from app.tenancy import WorkspaceContext
from app.tenancy.middleware import WorkspaceContextMiddleware
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
_TEST_UA = "pytest-agent-api"
_TEST_ACCEPT_LANGUAGE = "en"


def _load_all_models() -> None:
    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def api_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(api_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=api_engine, expire_on_commit=False, class_=Session)


def _bootstrap(
    factory: sessionmaker[Session],
    *,
    role: Literal["manager", "worker"],
) -> tuple[str, str]:
    with factory() as s:
        workspace_id = new_ulid()
        user_id = new_ulid()
        s.add(
            Workspace(
                id=workspace_id,
                slug="agent-test",
                name="Agent Test",
                plan="free",
                quota_json={},
                settings_json={},
                created_at=_PINNED,
            )
        )
        s.add(
            User(
                id=user_id,
                email=f"{role}@example.com",
                email_lower=canonicalise_email(f"{role}@example.com"),
                display_name=role.title(),
                created_at=_PINNED,
            )
        )
        s.flush()
        s.add(
            UserWorkspace(
                user_id=user_id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=_PINNED,
            )
        )
        _seed_llm_assignment(
            s, workspace_id=workspace_id, capability=f"chat.{role_to_scope(role)}"
        )
        s.add(
            BudgetLedger(
                id=new_ulid(),
                workspace_id=workspace_id,
                period_start=_PINNED - timedelta(days=30),
                period_end=_PINNED + timedelta(seconds=1),
                spent_cents=0,
                cap_cents=10_000,
                updated_at=_PINNED,
            )
        )
        s.commit()
    return workspace_id, user_id


def _grant_role(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
    role: Literal["manager", "worker"],
) -> None:
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=role,
            scope_kind="workspace",
            created_at=_PINNED,
        )
    )


def _seed_worker_target(
    session: Session,
    *,
    workspace_id: str,
    display_name: str = "Maria",
    archived: bool = False,
) -> str:
    user_id = new_ulid()
    session.add(
        User(
            id=user_id,
            email=f"{user_id.lower()}@example.com",
            email_lower=canonicalise_email(f"{user_id.lower()}@example.com"),
            display_name=display_name,
            created_at=_PINNED,
            archived_at=_PINNED if archived else None,
        )
    )
    session.flush()
    session.add(
        UserWorkspace(
            user_id=user_id,
            workspace_id=workspace_id,
            source="workspace_grant",
            added_at=_PINNED,
        )
    )
    _grant_role(session, workspace_id=workspace_id, user_id=user_id, role="worker")
    session.add(
        WorkEngagement(
            id=new_ulid(),
            user_id=user_id,
            workspace_id=workspace_id,
            engagement_kind="payroll",
            started_on=_PINNED.date(),
            archived_on=None,
            created_at=_PINNED,
            updated_at=_PINNED,
        )
    )
    return user_id


def role_to_scope(role: Literal["manager", "worker"]) -> Literal["manager", "employee"]:
    return "manager" if role == "manager" else "employee"


def _seed_llm_assignment(
    session: Session, *, workspace_id: str, capability: str
) -> None:
    provider = LlmProvider(
        id=new_ulid(),
        name=f"unit-provider-{capability}",
        provider_type="fake",
        timeout_s=60,
        requests_per_minute=60,
        is_enabled=True,
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    model = LlmModel(
        id=new_ulid(),
        canonical_name=f"fake/{capability}",
        display_name=f"fake/{capability}",
        capabilities=["chat"],
        is_active=True,
        price_source="",
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add_all([provider, model])
    session.flush()
    provider_model = LlmProviderModel(
        id=new_ulid(),
        provider_id=provider.id,
        model_id=model.id,
        api_model_id=f"fake/{capability}",
        supports_system_prompt=True,
        supports_temperature=True,
        is_enabled=True,
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add(provider_model)
    session.flush()
    session.add(
        LlmAssignment(
            id=new_ulid(),
            workspace_id=None,
            capability=capability,
            model_id=provider_model.id,
            provider="fake",
            priority=0,
            enabled=True,
            max_tokens=None,
            temperature=None,
            extra_api_params={},
            required_capabilities=[],
            created_at=_PINNED,
        )
    )
    invalidate_llm_router_cache(workspace_id=workspace_id)


def _ctx(
    *,
    workspace_id: str,
    actor_id: str,
    role: Literal["manager", "worker"],
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="agent-test",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _client(
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
    *,
    event_bus: EventBus | None = None,
    llm: _ReplyLLM | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_agent_router(clock=FrozenClock(_PINNED), event_bus=event_bus),
        prefix="/w/{slug}/api/v1",
    )

    async def _on_domain_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DomainError)
        return _handle_domain_error(request, exc)

    app.add_exception_handler(DomainError, _on_domain_error)

    def _override_ctx() -> WorkspaceContext:
        return ctx

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    def _override_llm() -> _ReplyLLM:
        return llm or _ReplyLLM()

    def _override_token_factory() -> _UnitTokenFactory:
        return _UnitTokenFactory()

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db
    app.dependency_overrides[get_llm_dep] = _override_llm
    app.dependency_overrides[get_agent_token_factory] = _override_token_factory
    return TestClient(app, raise_server_exceptions=False)


class _ReplyLLM:
    def __init__(self, text: str = "I can help.") -> None:
        self.text = text
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def chat(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(list(kwargs["messages"]))
        return LLMResponse(
            text=self.text,
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model_id="fake/unit",
            finish_reason="stop",
        )

    def ocr(self, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def stream_chat(self, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError


class _UnitTokenFactory:
    def mint_for(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return DelegatedToken(plaintext="mip_FAKEKEY_FAKESECRET", token_id="tok_unit")

    def revoke_minted(
        self, ctx: WorkspaceContext, *, session: Session | None = None
    ) -> None:
        del ctx, session


def _settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-agent-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        smtp_host=None,
        smtp_port=587,
        smtp_from=None,
        smtp_use_tls=False,
        log_level="INFO",
        cors_allow_origins=[],
        profile="prod",
        vite_dev_url="http://127.0.0.1:5173",
    )


def test_agent_routes_are_mounted_by_factory() -> None:
    client = TestClient(create_app(settings=_settings()), raise_server_exceptions=False)

    schema = client.get("/api/openapi.json").json()

    assert "/w/{slug}/api/v1/agent/{scope}/log" in schema["paths"]
    assert "/w/{slug}/api/v1/agent/{scope}/message" in schema["paths"]
    post_op = schema["paths"]["/w/{slug}/api/v1/agent/{scope}/message"]["post"]
    assert post_op["x-interactive-only"] is True


@pytest.mark.parametrize(
    ("scope", "role"),
    [
        ("employee", "worker"),
        ("manager", "manager"),
    ],
)
def test_agent_message_endpoint_mounts_and_returns_agent_message_json(
    factory: sessionmaker[Session],
    scope: Literal["employee", "manager"],
    role: Literal["manager", "worker"],
) -> None:
    workspace_id, user_id = _bootstrap(factory, role=role)
    event_bus = EventBus()
    appended: list[AgentMessageAppended] = []
    broadcast: list[ChatMessageSent] = []
    event_bus.subscribe(AgentMessageAppended)(appended.append)
    event_bus.subscribe(ChatMessageSent)(broadcast.append)
    client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=user_id, role=role),
        event_bus=event_bus,
    )

    response = client.post(
        f"/w/agent-test/api/v1/agent/{scope}/message",
        json={"body": "Can you help?"},
    )

    assert response.status_code == 201
    assert response.json() == {
        "at": "2026-04-29T12:00:00Z",
        "kind": "user",
        "body": "Can you help?",
        "channel_kind": None,
    }
    assert len(appended) == 2
    assert appended[0].workspace_id == workspace_id
    assert appended[0].actor_user_id == user_id
    assert appended[0].scope == scope
    assert appended[0].message.kind == "user"
    assert appended[0].message.body == "Can you help?"
    assert appended[1].message.kind == "agent"
    assert appended[1].message.body == "I can help."
    assert broadcast == []


@pytest.mark.parametrize(
    ("scope", "role"),
    [
        ("employee", "worker"),
        ("manager", "manager"),
    ],
)
def test_agent_log_endpoint_mounts_and_returns_agent_message_list(
    factory: sessionmaker[Session],
    scope: Literal["employee", "manager"],
    role: Literal["manager", "worker"],
) -> None:
    workspace_id, user_id = _bootstrap(factory, role=role)
    client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=user_id, role=role),
    )

    empty = client.get(f"/w/agent-test/api/v1/agent/{scope}/log")
    assert empty.status_code == 200
    assert empty.json() == []

    created = client.post(
        f"/w/agent-test/api/v1/agent/{scope}/message",
        json={"body": "Show my next shift"},
    )
    assert created.status_code == 201

    listed = client.get(f"/w/agent-test/api/v1/agent/{scope}/log")
    assert listed.status_code == 200
    messages = listed.json()
    assert len(messages) == 2
    assert {
        (message["kind"], message["body"], message["channel_kind"])
        for message in messages
    } == {
        ("user", "Show my next shift", None),
        ("agent", "I can help.", None),
    }
    assert [(message["kind"], message["at"]) for message in messages] == [
        ("user", "2026-04-29T12:00:00Z"),
        ("agent", "2026-04-29T12:00:00.000001Z"),
    ]


@pytest.mark.parametrize(
    ("scope", "role"),
    [
        ("employee", "manager"),
        ("manager", "worker"),
    ],
)
def test_agent_scope_mismatch_returns_problem_json(
    factory: sessionmaker[Session],
    scope: Literal["employee", "manager"],
    role: Literal["manager", "worker"],
) -> None:
    workspace_id, user_id = _bootstrap(factory, role=role)
    client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=user_id, role=role),
    )

    response = client.get(f"/w/agent-test/api/v1/agent/{scope}/log")

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error"] == "agent_scope_forbidden"


def test_agent_relay_request_delivers_to_target_agent_thread(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id = _bootstrap(factory, role="manager")
    with factory() as s:
        _grant_role(s, workspace_id=workspace_id, user_id=manager_id, role="manager")
        worker_id = _seed_worker_target(s, workspace_id=workspace_id)
        s.commit()
    bus = EventBus()
    appended: list[AgentMessageAppended] = []
    bus.subscribe(AgentMessageAppended)(appended.append)
    client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
        event_bus=bus,
    )

    response = client.post(
        "/w/agent-test/api/v1/agent/manager/relay/request",
        json={"target_user_id": worker_id, "request": "Can you work Sunday?"},
    )

    assert response.status_code == 201
    assert response.json() == {
        "confirmation": "I asked Maria.",
        "target_user_id": worker_id,
        "target_display_label": "Maria",
    }
    with factory() as s:
        channel = s.scalars(
            select(ChatChannel).where(
                ChatChannel.workspace_id == workspace_id,
                ChatChannel.external_ref == f"agent:employee:{worker_id}",
            )
        ).one()
        message = s.scalars(
            select(ChatMessage).where(ChatMessage.channel_id == channel.id)
        ).one()
        relay = s.scalars(select(AgentRelayRequest)).one()
    assert channel.kind == "staff"
    assert message.author_user_id == worker_id
    assert message.author_label == "agent"
    assert message.body_md == "Manager is asking: Can you work Sunday?"
    assert relay.requester_user_id == manager_id
    assert relay.target_user_id == worker_id
    assert relay.target_thread_ref == f"agent:employee:{worker_id}"
    assert relay.target_message_ref == message.id
    assert relay.request_summary == "Can you work Sunday?"
    assert [event.actor_user_id for event in appended] == [worker_id]
    assert appended[0].message.body == message.body_md
    with factory() as s:
        notification = s.scalars(select(Notification)).one()
        audit = s.scalars(
            select(AuditLog).where(
                AuditLog.entity_kind == "agent_relay_request",
                AuditLog.action == "agent.relay.requested",
            )
        ).one()
    assert notification.recipient_user_id == worker_id
    assert notification.kind == "agent_message"
    assert notification.payload_json["message_id"] == message.id
    assert notification.payload_json["chat_thread_ref"] == channel.id
    assert audit.diff == {
        "requester_user_id": manager_id,
        "target_user_id": worker_id,
        "agent_relay_request_id": relay.id,
        "target_channel_id": channel.id,
        "target_message_id": message.id,
    }


def test_worker_relay_answer_summarizes_to_requester_once(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id = _bootstrap(factory, role="manager")
    with factory() as s:
        _grant_role(s, workspace_id=workspace_id, user_id=manager_id, role="manager")
        worker_id = _seed_worker_target(s, workspace_id=workspace_id)
        _seed_llm_assignment(s, workspace_id=workspace_id, capability="chat.employee")
        s.commit()
    manager_client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
    )
    opened = manager_client.post(
        "/w/agent-test/api/v1/agent/manager/relay/request",
        json={"target_user_id": worker_id, "request": "Can you work Sunday?"},
    )
    assert opened.status_code == 201
    bus = EventBus()
    appended: list[AgentMessageAppended] = []
    bus.subscribe(AgentMessageAppended)(appended.append)
    llm = _ReplyLLM("Thanks, I will pass that along.")
    worker_client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=worker_id, role="worker"),
        event_bus=bus,
        llm=llm,
    )

    response = worker_client.post(
        "/w/agent-test/api/v1/agent/employee/message",
        json={"body": "Yes, but only 2-6pm"},
    )

    assert response.status_code == 201
    with factory() as s:
        relay = s.scalars(select(AgentRelayRequest)).one()
        requester_channel = s.scalars(
            select(ChatChannel).where(
                ChatChannel.workspace_id == workspace_id,
                ChatChannel.external_ref == f"agent:manager:{manager_id}",
            )
        ).one()
        requester_messages = s.scalars(
            select(ChatMessage).where(ChatMessage.channel_id == requester_channel.id)
        ).all()
    assert relay.status == "answered"
    assert relay.response_summary == "Maria responded: Yes, but only 2-6pm"
    assert [message.body_md for message in requester_messages] == [
        "Maria responded: Yes, but only 2-6pm"
    ]
    requester_events = [
        event for event in appended if event.actor_user_id == manager_id
    ]
    assert len(requester_events) == 1
    assert requester_events[0].scope == "manager"
    assert requester_events[0].message.body == "Maria responded: Yes, but only 2-6pm"
    assert llm.calls
    relay_context = [
        message["content"]
        for message in llm.calls[0]
        if message["role"] == "system"
        and "Pending mediated relay" in message["content"]
    ]
    assert relay_context == [
        "Pending mediated relay:\n"
        "Manager asked: Can you work Sunday?\n"
        "If the user's latest message clearly answers this request, "
        "acknowledge briefly. If it does not clearly answer, ask one "
        "short clarifying question."
    ]

    repeated = worker_client.post(
        "/w/agent-test/api/v1/agent/employee/message",
        json={"body": "Yes, but only 2-6pm"},
    )
    assert repeated.status_code == 201
    with factory() as s:
        requester_messages = s.scalars(
            select(ChatMessage).where(ChatMessage.channel_id == requester_channel.id)
        ).all()
        relay = s.scalars(select(AgentRelayRequest)).one()
    assert relay.status == "answered"
    assert [
        message.body_md
        for message in requester_messages
        if message.body_md == "Maria responded: Yes, but only 2-6pm"
    ] == ["Maria responded: Yes, but only 2-6pm"]


def test_worker_ambiguous_relay_reply_keeps_relay_open_without_requester_notice(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id = _bootstrap(factory, role="manager")
    with factory() as s:
        _grant_role(s, workspace_id=workspace_id, user_id=manager_id, role="manager")
        worker_id = _seed_worker_target(s, workspace_id=workspace_id)
        _seed_llm_assignment(s, workspace_id=workspace_id, capability="chat.employee")
        s.commit()
    manager_client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
    )
    opened = manager_client.post(
        "/w/agent-test/api/v1/agent/manager/relay/request",
        json={"target_user_id": worker_id, "request": "Can you work Sunday?"},
    )
    assert opened.status_code == 201
    bus = EventBus()
    appended: list[AgentMessageAppended] = []
    bus.subscribe(AgentMessageAppended)(appended.append)
    worker_client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=worker_id, role="worker"),
        event_bus=bus,
        llm=_ReplyLLM("What hours would work for you?"),
    )

    response = worker_client.post(
        "/w/agent-test/api/v1/agent/employee/message",
        json={"body": "Hmm"},
    )

    assert response.status_code == 201
    with factory() as s:
        relay = s.scalars(select(AgentRelayRequest)).one()
        requester_channel = s.scalars(
            select(ChatChannel).where(
                ChatChannel.workspace_id == workspace_id,
                ChatChannel.external_ref == f"agent:manager:{manager_id}",
            )
        ).first()
    assert relay.status == "open"
    assert relay.response_summary is None
    assert requester_channel is None
    assert all(event.actor_user_id != manager_id for event in appended)


def test_worker_relay_question_reply_keeps_relay_open_without_requester_notice(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id = _bootstrap(factory, role="manager")
    with factory() as s:
        _grant_role(s, workspace_id=workspace_id, user_id=manager_id, role="manager")
        worker_id = _seed_worker_target(s, workspace_id=workspace_id)
        _seed_llm_assignment(s, workspace_id=workspace_id, capability="chat.employee")
        s.commit()
    manager_client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
    )
    opened = manager_client.post(
        "/w/agent-test/api/v1/agent/manager/relay/request",
        json={"target_user_id": worker_id, "request": "Can you work Sunday?"},
    )
    assert opened.status_code == 201
    bus = EventBus()
    appended: list[AgentMessageAppended] = []
    bus.subscribe(AgentMessageAppended)(appended.append)
    worker_client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=worker_id, role="worker"),
        event_bus=bus,
        llm=_ReplyLLM("What hours would work for you?"),
    )

    response = worker_client.post(
        "/w/agent-test/api/v1/agent/employee/message",
        json={"body": "Can you clarify the hours?"},
    )

    assert response.status_code == 201
    with factory() as s:
        relay = s.scalars(select(AgentRelayRequest)).one()
        requester_channel = s.scalars(
            select(ChatChannel).where(
                ChatChannel.workspace_id == workspace_id,
                ChatChannel.external_ref == f"agent:manager:{manager_id}",
            )
        ).first()
    assert relay.status == "open"
    assert relay.response_summary is None
    assert requester_channel is None
    assert all(event.actor_user_id != manager_id for event in appended)


def test_worker_relay_answer_skips_invalid_requester_delivery(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id = _bootstrap(factory, role="manager")
    with factory() as s:
        _grant_role(s, workspace_id=workspace_id, user_id=manager_id, role="manager")
        worker_id = _seed_worker_target(s, workspace_id=workspace_id)
        _seed_llm_assignment(s, workspace_id=workspace_id, capability="chat.employee")
        s.commit()
    manager_client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
    )
    opened = manager_client.post(
        "/w/agent-test/api/v1/agent/manager/relay/request",
        json={"target_user_id": worker_id, "request": "Can you work Sunday?"},
    )
    assert opened.status_code == 201
    with factory() as s:
        grant = s.scalars(
            select(RoleGrant).where(
                RoleGrant.workspace_id == workspace_id,
                RoleGrant.user_id == manager_id,
                RoleGrant.grant_role == "manager",
            )
        ).one()
        grant.revoked_at = _PINNED
        s.commit()
    bus = EventBus()
    appended: list[AgentMessageAppended] = []
    bus.subscribe(AgentMessageAppended)(appended.append)
    worker_client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=worker_id, role="worker"),
        event_bus=bus,
        llm=_ReplyLLM("Thanks, I will pass that along."),
    )

    response = worker_client.post(
        "/w/agent-test/api/v1/agent/employee/message",
        json={"body": "Yes, but only 2-6pm"},
    )

    assert response.status_code == 201
    with factory() as s:
        relay = s.scalars(select(AgentRelayRequest)).one()
        requester_channel = s.scalars(
            select(ChatChannel).where(
                ChatChannel.workspace_id == workspace_id,
                ChatChannel.external_ref == f"agent:manager:{manager_id}",
            )
        ).first()
    assert relay.status == "answered"
    assert relay.response_summary == "Maria responded: Yes, but only 2-6pm"
    assert requester_channel is None
    assert all(event.actor_user_id != manager_id for event in appended)


def test_worker_relay_lookup_ignores_wrong_target_scope(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id = _bootstrap(factory, role="manager")
    with factory() as s:
        _grant_role(s, workspace_id=workspace_id, user_id=manager_id, role="manager")
        worker_id = _seed_worker_target(s, workspace_id=workspace_id)
        _seed_llm_assignment(s, workspace_id=workspace_id, capability="chat.employee")
        s.commit()
    manager_client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
    )
    opened = manager_client.post(
        "/w/agent-test/api/v1/agent/manager/relay/request",
        json={"target_user_id": worker_id, "request": "Can you work Sunday?"},
    )
    assert opened.status_code == 201
    with factory() as s:
        relay = s.scalars(select(AgentRelayRequest)).one()
        relay.target_scope = "manager"
        s.commit()
    llm = _ReplyLLM("I can help.")
    worker_client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=worker_id, role="worker"),
        llm=llm,
    )

    response = worker_client.post(
        "/w/agent-test/api/v1/agent/employee/message",
        json={"body": "Yes, but only 2-6pm"},
    )

    assert response.status_code == 201
    with factory() as s:
        relay = s.scalars(select(AgentRelayRequest)).one()
    assert relay.status == "open"
    assert all(
        "Pending mediated relay" not in message["content"]
        for message in llm.calls[0]
        if message["role"] == "system"
    )


def test_agent_relay_request_resolves_unambiguous_target_name(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id = _bootstrap(factory, role="manager")
    with factory() as s:
        _grant_role(s, workspace_id=workspace_id, user_id=manager_id, role="manager")
        worker_id = _seed_worker_target(s, workspace_id=workspace_id)
        s.commit()
    client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
    )

    response = client.post(
        "/w/agent-test/api/v1/agent/manager/relay/request",
        json={"target_name": "maria", "request": "Can you work Sunday?"},
    )

    assert response.status_code == 201
    assert response.json()["target_user_id"] == worker_id


def test_agent_relay_request_rejects_ambiguous_name_without_message_row(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id = _bootstrap(factory, role="manager")
    with factory() as s:
        _grant_role(s, workspace_id=workspace_id, user_id=manager_id, role="manager")
        _seed_worker_target(s, workspace_id=workspace_id, display_name="Maria")
        _seed_worker_target(s, workspace_id=workspace_id, display_name="Maria")
        s.commit()
    client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
    )

    response = client.post(
        "/w/agent-test/api/v1/agent/manager/relay/request",
        json={"target_name": "Maria", "request": "Can you work Sunday?"},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "agent_relay_target_ambiguous"
    with factory() as s:
        assert s.scalars(select(ChatMessage)).all() == []
        assert s.scalars(select(AgentRelayRequest)).all() == []


def test_agent_relay_request_rejects_archived_target_without_message_row(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id = _bootstrap(factory, role="manager")
    with factory() as s:
        _grant_role(s, workspace_id=workspace_id, user_id=manager_id, role="manager")
        worker_id = _seed_worker_target(
            s, workspace_id=workspace_id, display_name="Maria", archived=True
        )
        s.commit()
    client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
    )

    response = client.post(
        "/w/agent-test/api/v1/agent/manager/relay/request",
        json={"target_user_id": worker_id, "request": "Can you work Sunday?"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "agent_relay_target_forbidden"
    with factory() as s:
        assert s.scalars(select(ChatMessage)).all() == []
        assert s.scalars(select(AgentRelayRequest)).all() == []


def test_agent_relay_request_rejects_cross_workspace_target_without_rows(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id = _bootstrap(factory, role="manager")
    other_workspace_id = new_ulid()
    with factory() as s:
        s.add(
            Workspace(
                id=other_workspace_id,
                slug="agent-test-other",
                name="Other Agent Test",
                plan="free",
                quota_json={},
                settings_json={},
                created_at=_PINNED,
            )
        )
        s.flush()
        _grant_role(s, workspace_id=workspace_id, user_id=manager_id, role="manager")
        worker_id = _seed_worker_target(s, workspace_id=other_workspace_id)
        s.commit()
    client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
    )

    response = client.post(
        "/w/agent-test/api/v1/agent/manager/relay/request",
        json={"target_user_id": worker_id, "request": "Can you work Sunday?"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "agent_relay_target_forbidden"
    with factory() as s:
        assert s.scalars(select(ChatMessage)).all() == []
        assert s.scalars(select(AgentRelayRequest)).all() == []
        assert s.scalars(select(Notification)).all() == []


def test_worker_cannot_open_relay_request_without_rows(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, worker_actor_id = _bootstrap(factory, role="worker")
    with factory() as s:
        _grant_role(
            s, workspace_id=workspace_id, user_id=worker_actor_id, role="worker"
        )
        target_id = _seed_worker_target(s, workspace_id=workspace_id)
        s.commit()
    client = _client(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=worker_actor_id, role="worker"),
    )

    response = client.post(
        "/w/agent-test/api/v1/agent/manager/relay/request",
        json={"target_user_id": target_id, "request": "Can you work Sunday?"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "permission_denied"
    with factory() as s:
        assert s.scalars(select(ChatMessage)).all() == []
        assert s.scalars(select(AgentRelayRequest)).all() == []
        assert s.scalars(select(Notification)).all() == []


def test_manager_log_workspace_resolution_defers_session_touch_autoflush(
    api_engine: Engine,
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, user_id = _bootstrap(factory, role="manager")
    settings = _settings()
    with factory() as s:
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user_id,
                grant_role="manager",
                scope_kind="workspace",
                created_at=_PINNED,
            )
        )
        issued = issue(
            s,
            user_id=user_id,
            has_owner_grant=False,
            ua=_TEST_UA,
            ip="127.0.0.1",
            accept_language=_TEST_ACCEPT_LANGUAGE,
            settings=settings,
            clock=FrozenClock(_PINNED),
        )
        s.commit()

    statements: list[str] = []

    @event.listens_for(api_engine, "before_cursor_execute")
    def _record_sql(
        _conn: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if (
            normalized.startswith("update session")
            or " from workspace " in normalized
            or " from user_workspace " in normalized
            or " from role_grant " in normalized
        ):
            statements.append(normalized)

    monkeypatch.setattr(
        "app.tenancy.middleware.make_uow",
        lambda: UnitOfWorkImpl(session_factory=factory),
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 1),
            "root_path": "",
            "path": "/w/agent-test/api/v1/agent/manager/log",
            "raw_path": b"/w/agent-test/api/v1/agent/manager/log",
            "query_string": b"",
            "headers": [
                (b"cookie", f"{SESSION_COOKIE_NAME}={issued.cookie_value}".encode()),
                (b"user-agent", _TEST_UA.encode()),
                (b"accept-language", _TEST_ACCEPT_LANGUAGE.encode()),
            ],
        }
    )
    middleware = WorkspaceContextMiddleware(app=FastAPI())

    try:
        ctx, actor, outcome = middleware._resolve_context(
            request,
            settings,
            new_ulid(),
        )
    finally:
        event.remove(api_engine, "before_cursor_execute", _record_sql)

    assert ctx is not None
    assert actor is not None
    assert outcome == "resolved"
    assert ctx.workspace_id == workspace_id
    assert ctx.actor_grant_role == "manager"
    first_session_touch = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("update session")
    )
    first_tenancy_read = next(
        index
        for index, statement in enumerate(statements)
        if " from workspace " in statement or " from user_workspace " in statement
    )
    assert first_session_touch > first_tenancy_read
    with factory() as s:
        stored = s.scalar(select(SessionRow).where(SessionRow.id == issued.session_id))
        assert stored is not None
        assert stored.last_seen_at > _PINNED
