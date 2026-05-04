"""HTTP tests for §23 chat-channel binding routes."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Literal

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import JSONResponse

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.messaging.models import (
    ChatChannel,
    ChatChannelBinding,
    ChatGatewayBinding,
)
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.chat_gateway import build_chat_channel_bindings_router
from app.api.deps import current_workspace_context, db_session
from app.api.errors import _handle_domain_error
from app.domain.errors import DomainError
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 5, 1, 6, 0, 0, tzinfo=UTC)


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


def _bootstrap_workspace(s: Session) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"ws-{workspace_id[-6:].lower()}",
            name="Chat Gateway Test",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _bootstrap_user(
    s: Session,
    *,
    workspace_id: str,
    email: str,
    role: Literal["manager", "worker", "client", "guest"],
) -> str:
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=email.split("@", maxsplit=1)[0],
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
    s.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=role,
            scope_kind="workspace",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    s.flush()
    return user_id


def _ctx(
    *,
    workspace_id: str,
    actor_id: str,
    role: Literal["manager", "worker", "client", "guest"],
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="chat-gateway",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _build_app(factory: sessionmaker[Session], ctx: WorkspaceContext) -> FastAPI:
    app = FastAPI()

    async def _on_domain_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DomainError)
        return _handle_domain_error(request, exc)

    app.add_exception_handler(DomainError, _on_domain_error)
    app.include_router(build_chat_channel_bindings_router())

    def _override_ctx() -> WorkspaceContext:
        return ctx

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db
    return app


@pytest.fixture
def seeded(factory: sessionmaker[Session]) -> tuple[str, str, str]:
    with factory() as s:
        workspace_id = _bootstrap_workspace(s)
        manager_id = _bootstrap_user(
            s,
            workspace_id=workspace_id,
            email="manager@example.com",
            role="manager",
        )
        worker_id = _bootstrap_user(
            s,
            workspace_id=workspace_id,
            email="worker@example.com",
            role="worker",
        )
        s.commit()
    return workspace_id, manager_id, worker_id


def test_worker_links_verifies_and_unlinks_own_whatsapp_binding(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, _manager_id, worker_id = seeded
    client = TestClient(
        _build_app(
            factory, _ctx(workspace_id=workspace_id, actor_id=worker_id, role="worker")
        ),
        raise_server_exceptions=False,
    )

    start = client.post(
        "/chat/channels/link/start",
        json={
            "channel_kind": "offapp_whatsapp",
            "address": "+1 555 123 4567",
            "user_id": worker_id,
        },
    )

    assert start.status_code == 201, start.text
    binding_id = start.json()["binding_id"]
    listed = client.get("/chat/channels").json()
    assert [row["id"] for row in listed] == [binding_id]
    assert listed[0]["address"] == "+15551234567"
    assert listed[0]["state"] == "pending"

    verified = client.post(
        "/chat/channels/link/verify",
        json={"binding_id": binding_id, "code": "424242"},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json() == {"binding_id": binding_id, "state": "active"}

    unlinked = client.post(f"/chat/channels/{binding_id}/unlink")
    assert unlinked.status_code == 200, unlinked.text
    assert unlinked.json() == {"binding_id": binding_id, "state": "revoked"}

    with factory() as s:
        row = s.get(ChatChannelBinding, binding_id)
        assert row is not None
        assert row.state == "revoked"
        actions = s.scalars(select(AuditLog.action)).all()
    assert actions == [
        "chat_channel_binding.created",
        "chat_channel_binding.verified",
        "chat_channel_binding.revoked",
    ]


def test_worker_sees_only_own_bindings_but_manager_sees_workspace(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, worker_id = seeded
    worker = TestClient(
        _build_app(
            factory, _ctx(workspace_id=workspace_id, actor_id=worker_id, role="worker")
        ),
        raise_server_exceptions=False,
    )
    manager = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
        ),
        raise_server_exceptions=False,
    )
    start = worker.post(
        "/chat/channels/link/start",
        json={
            "channel_kind": "offapp_whatsapp",
            "address": "+15550000001",
            "user_id": worker_id,
        },
    )
    assert start.status_code == 201, start.text
    manager_start = manager.post(
        "/chat/channels/link/start",
        json={
            "channel_kind": "offapp_whatsapp",
            "address": "+15550000002",
            "user_id": manager_id,
        },
    )
    assert manager_start.status_code == 201, manager_start.text

    worker_rows = worker.get("/chat/channels").json()
    manager_rows = manager.get("/chat/channels").json()

    assert [row["user_id"] for row in worker_rows] == [worker_id]
    assert {row["user_id"] for row in manager_rows} == {manager_id, worker_id}


def test_link_start_cannot_target_another_user(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, worker_id = seeded
    client = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
        ),
        raise_server_exceptions=False,
    )

    resp = client.post(
        "/chat/channels/link/start",
        json={
            "channel_kind": "offapp_whatsapp",
            "address": "+15550000003",
            "user_id": worker_id,
        },
    )

    assert resp.status_code == 403
    assert resp.json()["error"] == "permission_denied"


def test_wrong_verification_code_is_422(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, _manager_id, worker_id = seeded
    client = TestClient(
        _build_app(
            factory, _ctx(workspace_id=workspace_id, actor_id=worker_id, role="worker")
        ),
        raise_server_exceptions=False,
    )
    started = client.post(
        "/chat/channels/link/start",
        json={
            "channel_kind": "offapp_whatsapp",
            "address": "+15550000004",
            "user_id": worker_id,
        },
    ).json()

    resp = client.post(
        "/chat/channels/link/verify",
        json={"binding_id": started["binding_id"], "code": "000000"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"] == "chat_channel_binding_invalid"


def test_provider_status_shape(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, _worker_id = seeded
    client = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
        ),
        raise_server_exceptions=False,
    )

    resp = client.get("/chat/channels/providers")

    assert resp.status_code == 200
    rows = resp.json()
    assert [row["channel_kind"] for row in rows] == [
        "offapp_whatsapp",
        "offapp_telegram",
    ]
    assert rows[0]["templates"] == [
        "chat_channel_link_code",
        "chat_agent_nudge",
        "chat_workspace_pick",
    ]


def test_provider_status_requires_chat_gateway_read(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, _manager_id, worker_id = seeded
    client = TestClient(
        _build_app(
            factory, _ctx(workspace_id=workspace_id, actor_id=worker_id, role="worker")
        ),
        raise_server_exceptions=False,
    )

    resp = client.get("/chat/channels/providers")

    assert resp.status_code == 403
    assert resp.json()["error"] == "permission_denied"
    assert resp.json()["action_key"] == "chat_gateway.read"


def test_provider_status_last_webhook_is_workspace_scoped(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, _worker_id = seeded
    other_webhook_at = datetime(2026, 5, 1, 7, 0, 0, tzinfo=UTC)
    with factory() as s:
        other_workspace_id = _bootstrap_workspace(s)
        _bootstrap_user(
            s,
            workspace_id=other_workspace_id,
            email="other@example.com",
            role="manager",
        )
        for target_workspace_id, last_message_at, contact in (
            (workspace_id, _PINNED, "+15550000998"),
            (other_workspace_id, other_webhook_at, "+15550000999"),
        ):
            channel_id = new_ulid()
            s.add(
                ChatChannel(
                    id=channel_id,
                    workspace_id=target_workspace_id,
                    kind="chat_gateway",
                    source="whatsapp",
                    external_ref=contact,
                    title="WhatsApp",
                    created_at=_PINNED,
                    archived_at=None,
                )
            )
            s.add(
                ChatGatewayBinding(
                    id=new_ulid(),
                    workspace_id=target_workspace_id,
                    provider="meta_whatsapp",
                    external_contact=contact,
                    channel_id=channel_id,
                    display_label="WhatsApp",
                    provider_metadata_json={},
                    created_at=_PINNED,
                    last_message_at=last_message_at,
                )
            )
        s.commit()
    client = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
        ),
        raise_server_exceptions=False,
    )

    resp = client.get("/chat/channels/providers")

    assert resp.status_code == 200
    rows = resp.json()
    assert rows[0]["channel_kind"] == "offapp_whatsapp"
    assert rows[0]["last_webhook_at"] == "2026-05-01T06:00:00Z"
