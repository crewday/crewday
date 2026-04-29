"""HTTP-boundary tests for workspace-scoped agent chat endpoints."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import current_workspace_context, db_session
from app.api.factory import create_app
from app.api.v1.agent import build_agent_router
from app.config import Settings
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
        s.commit()
    return workspace_id, user_id


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

    def _override_ctx() -> WorkspaceContext:
        return ctx

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db
    return TestClient(app, raise_server_exceptions=False)


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
    assert len(appended) == 1
    assert appended[0].workspace_id == workspace_id
    assert appended[0].actor_user_id == user_id
    assert appended[0].scope == scope
    assert appended[0].message.kind == "user"
    assert appended[0].message.body == "Can you help?"
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
    assert listed.json() == [
        {
            "at": "2026-04-29T12:00:00Z",
            "kind": "user",
            "body": "Show my next shift",
            "channel_kind": None,
        }
    ]
