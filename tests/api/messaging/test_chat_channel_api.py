"""HTTP-boundary tests for chat-channel CRUD (cd-7ej8)."""

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
from app.adapters.db.messaging.models import ChatChannel
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import current_workspace_context, db_session
from app.api.errors import _handle_domain_error
from app.api.v1.messaging import build_messaging_router
from app.domain.errors import DomainError
from app.tenancy.context import WorkspaceContext
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


def _bootstrap_workspace(s: Session) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"ws-{workspace_id[-6:].lower()}",
            name="Messaging Test",
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
        workspace_slug="messaging",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _build_app(factory: sessionmaker[Session], ctx: WorkspaceContext) -> FastAPI:
    app = FastAPI()

    # Render :class:`DomainError` raised by shared deps as the §12
    # envelope; legacy ``HTTPException(detail=...)`` sites keep their
    # FastAPI default shape until per-context cleanup lands (cd-649m).
    async def _on_domain_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DomainError)
        return _handle_domain_error(request, exc)

    app.add_exception_handler(DomainError, _on_domain_error)
    app.include_router(build_messaging_router())

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


class TestChatChannelApi:
    def test_create_and_list_are_paginated(
        self,
        factory: sessionmaker[Session],
        seeded: tuple[str, str, str],
    ) -> None:
        workspace_id, manager_id, _worker_id = seeded
        ctx = _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager")
        client = TestClient(_build_app(factory, ctx), raise_server_exceptions=False)

        for title in ("Staff", "Managers"):
            resp = client.post(
                "/chat/channels",
                json={"kind": title.lower().removesuffix("s"), "title": title},
            )
            assert resp.status_code == 201

        resp = client.get("/chat/channels", params={"limit": 1})
        body = resp.json()
        assert resp.status_code == 200
        assert len(body["data"]) == 1
        assert body["has_more"] is True
        assert body["next_cursor"] is not None

        resp2 = client.get(
            "/chat/channels",
            params={"limit": 1, "cursor": body["next_cursor"]},
        )
        body2 = resp2.json()
        assert resp2.status_code == 200
        assert len(body2["data"]) == 1
        assert body2["has_more"] is False
        assert body2["next_cursor"] is None

    def test_invalid_cursor_maps_to_422(
        self,
        factory: sessionmaker[Session],
        seeded: tuple[str, str, str],
    ) -> None:
        workspace_id, manager_id, _worker_id = seeded
        ctx = _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager")
        client = TestClient(_build_app(factory, ctx), raise_server_exceptions=False)

        resp = client.get("/chat/channels", params={"cursor": "\xff"})

        assert resp.status_code == 422
        assert resp.json()["type"].endswith("/invalid_cursor")

    def test_worker_never_sees_manager_or_gateway_channels(
        self,
        factory: sessionmaker[Session],
        seeded: tuple[str, str, str],
    ) -> None:
        workspace_id, manager_id, worker_id = seeded
        manager_client = TestClient(
            _build_app(
                factory,
                _ctx(
                    workspace_id=workspace_id,
                    actor_id=manager_id,
                    role="manager",
                ),
            ),
            raise_server_exceptions=False,
        )
        staff = manager_client.post(
            "/chat/channels",
            json={"kind": "staff", "title": "Staff"},
        ).json()
        manager_client.post(
            "/chat/channels",
            json={"kind": "manager", "title": "Managers"},
        )
        manager_client.post(
            "/chat/channels",
            json={
                "kind": "chat_gateway",
                "title": "WhatsApp",
                "source": "whatsapp",
                "external_ref": "wa-1",
            },
        )

        worker_client = TestClient(
            _build_app(
                factory,
                _ctx(
                    workspace_id=workspace_id,
                    actor_id=worker_id,
                    role="worker",
                ),
            ),
            raise_server_exceptions=False,
        )
        resp = worker_client.get("/chat/channels")

        assert resp.status_code == 200
        assert [channel["id"] for channel in resp.json()["data"]] == [staff["id"]]
        assert resp.json()["data"][0]["can_post_messages"] is True

    def test_worker_cannot_create_channel(
        self,
        factory: sessionmaker[Session],
        seeded: tuple[str, str, str],
    ) -> None:
        workspace_id, _manager_id, worker_id = seeded
        client = TestClient(
            _build_app(
                factory,
                _ctx(
                    workspace_id=workspace_id,
                    actor_id=worker_id,
                    role="worker",
                ),
            ),
            raise_server_exceptions=False,
        )

        resp = client.post("/chat/channels", json={"kind": "staff", "title": "Staff"})

        assert resp.status_code == 403
        assert resp.json()["error"] == "permission_denied"

    def test_invalid_create_maps_to_422(
        self,
        factory: sessionmaker[Session],
        seeded: tuple[str, str, str],
    ) -> None:
        workspace_id, manager_id, _worker_id = seeded
        client = TestClient(
            _build_app(
                factory,
                _ctx(
                    workspace_id=workspace_id,
                    actor_id=manager_id,
                    role="manager",
                ),
            ),
            raise_server_exceptions=False,
        )

        resp = client.post(
            "/chat/channels",
            json={"kind": "staff", "title": "Staff", "external_ref": "wa-1"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"] == "chat_channel_invalid"

        gateway = client.post(
            "/chat/channels",
            json={"kind": "chat_gateway", "source": "whatsapp"},
        )
        assert gateway.status_code == 422
        assert gateway.json()["error"] == "chat_channel_invalid"

    def test_patch_hidden_or_missing_channel_maps_to_404(
        self,
        factory: sessionmaker[Session],
        seeded: tuple[str, str, str],
    ) -> None:
        workspace_id, manager_id, worker_id = seeded
        manager_client = TestClient(
            _build_app(
                factory,
                _ctx(
                    workspace_id=workspace_id,
                    actor_id=manager_id,
                    role="manager",
                ),
            ),
            raise_server_exceptions=False,
        )
        manager_channel = manager_client.post(
            "/chat/channels",
            json={"kind": "manager", "title": "Managers"},
        ).json()
        worker_client = TestClient(
            _build_app(
                factory,
                _ctx(
                    workspace_id=workspace_id,
                    actor_id=worker_id,
                    role="worker",
                ),
            ),
            raise_server_exceptions=False,
        )

        hidden = worker_client.patch(
            f"/chat/channels/{manager_channel['id']}",
            json={"title": "Leaked"},
        )
        missing = manager_client.patch(
            "/chat/channels/01HWA000000000000000MISS",
            json={"title": "Missing"},
        )

        assert hidden.status_code == 404
        assert hidden.json()["error"] == "chat_channel_not_found"
        assert missing.status_code == 404
        assert missing.json()["error"] == "chat_channel_not_found"

    def test_patch_renames_and_soft_archives(
        self,
        factory: sessionmaker[Session],
        seeded: tuple[str, str, str],
    ) -> None:
        workspace_id, manager_id, _worker_id = seeded
        client = TestClient(
            _build_app(
                factory,
                _ctx(
                    workspace_id=workspace_id,
                    actor_id=manager_id,
                    role="manager",
                ),
            ),
            raise_server_exceptions=False,
        )
        channel = client.post(
            "/chat/channels",
            json={"kind": "staff", "title": "Staff"},
        ).json()

        resp = client.patch(
            f"/chat/channels/{channel['id']}",
            json={"title": "Team", "archived": True},
        )

        assert resp.status_code == 200
        assert resp.json()["title"] == "Team"
        assert resp.json()["archived_at"] is not None

        assert client.get("/chat/channels").json()["data"] == []
        archived = client.get(
            "/chat/channels",
            params={"include_archived": True},
        ).json()
        assert [channel["id"] for channel in archived["data"]] == [channel["id"]]

        with factory() as s:
            row = s.get(ChatChannel, channel["id"])
            assert row is not None
            assert row.archived_at is not None
            actions = s.scalars(select(AuditLog.action)).all()
            assert actions == [
                "messaging.channel.created",
                "messaging.channel.archived",
            ]

    def test_patch_rejects_noop_and_unarchive_without_mutation(
        self,
        factory: sessionmaker[Session],
        seeded: tuple[str, str, str],
    ) -> None:
        workspace_id, manager_id, _worker_id = seeded
        client = TestClient(
            _build_app(
                factory,
                _ctx(
                    workspace_id=workspace_id,
                    actor_id=manager_id,
                    role="manager",
                ),
            ),
            raise_server_exceptions=False,
        )
        channel = client.post(
            "/chat/channels",
            json={"kind": "staff", "title": "Staff"},
        ).json()

        empty = client.patch(f"/chat/channels/{channel['id']}", json={})
        assert empty.status_code == 422
        assert (
            client.patch(
                f"/chat/channels/{channel['id']}",
                json={"archived": None},
            ).status_code
            == 422
        )
        resp = client.patch(
            f"/chat/channels/{channel['id']}",
            json={"title": "Team", "archived": False},
        )

        assert resp.status_code == 422
        assert resp.json()["error"] == "chat_channel_invalid"
        with factory() as s:
            row = s.get(ChatChannel, channel["id"])
            assert row is not None
            assert row.title == "Staff"
