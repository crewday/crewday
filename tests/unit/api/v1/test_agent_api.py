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
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import JSONResponse

from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.llm.models import (
    BudgetLedger,
    LlmAssignment,
    LlmModel,
    LlmProvider,
    LlmProviderModel,
)
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.adapters.llm.ports import LLMResponse, LLMUsage
from app.api.deps import current_workspace_context, db_session
from app.api.deps import get_llm as get_llm_dep
from app.api.errors import _handle_domain_error
from app.api.factory import create_app
from app.api.v1.agent import build_agent_router, get_agent_token_factory
from app.config import Settings
from app.domain.agent.runtime import DelegatedToken
from app.domain.errors import DomainError
from app.events.bus import EventBus
from app.events.types import AgentMessageAppended, ChatMessageSent
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


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
        priority=0,
        is_enabled=True,
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    model = LlmModel(
        id=new_ulid(),
        canonical_name=f"fake/{capability}",
        display_name=f"fake/{capability}",
        vendor="other",
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
            workspace_id=workspace_id,
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
        return _ReplyLLM()

    def _override_token_factory() -> _UnitTokenFactory:
        return _UnitTokenFactory()

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db
    app.dependency_overrides[get_llm_dep] = _override_llm
    app.dependency_overrides[get_agent_token_factory] = _override_token_factory
    return TestClient(app, raise_server_exceptions=False)


class _ReplyLLM:
    def complete(self, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def chat(self, **kwargs):  # type: ignore[no-untyped-def]
        return LLMResponse(
            text="I can help.",
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

    def revoke_minted(self, ctx: WorkspaceContext) -> None:
        del ctx


def _settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-agent-root-key"),
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
    assert {message["at"] for message in messages} == {"2026-04-29T12:00:00Z"}


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
