"""Unit tests for :mod:`app.api.admin.agent_docs`."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.llm.models import AgentDoc
from app.auth.session import SESSION_COOKIE_NAME
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.unit.api.admin._helpers import (
    PINNED,
    build_client,
    engine_fixture,
    install_admin_cookie,
    issue_session,
    seed_user,
    settings_fixture,
)


@pytest.fixture
def settings() -> Settings:
    return settings_fixture("agent-docs")


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


class TestAdminAgentDocs:
    def test_lists_seeded_docs_for_deployment_admin(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/agent_docs")

        assert resp.status_code == 200
        body = resp.json()
        assert {row["slug"] for row in body} >= {
            "admin_boundaries",
            "crewday_overview",
        }
        overview = next(row for row in body if row["slug"] == "crewday_overview")
        assert overview["title"] == "Crewday operating model"
        assert overview["roles"] == ["manager", "employee", "admin"]
        assert overview["version"] == 1
        assert overview["is_customised"] is False
        assert len(overview["default_hash"]) == 16

    def test_fetches_doc_body_after_seed(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/agent_docs/admin_boundaries")

        assert resp.status_code == 200
        body = resp.json()
        assert body["slug"] == "admin_boundaries"
        assert body["capabilities"] == ["chat.admin"]
        assert "Deployment admins manage the installation" in body["body_md"]

    def test_marks_operator_customised_rows(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        with session_factory() as s:
            with tenant_agnostic():
                s.add(
                    AgentDoc(
                        id=new_ulid(),
                        slug="custom",
                        title="Custom",
                        summary="Operator edit",
                        body_md="custom body\n",
                        roles=["admin"],
                        capabilities=["chat.admin"],
                        version=2,
                        is_active=True,
                        default_hash="0" * 16,
                        notes=None,
                        created_at=PINNED,
                        updated_at=PINNED,
                    )
                )
            s.commit()

        resp = client.get("/admin/api/v1/agent_docs/custom")

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_customised"] is True
        assert body["version"] == 2

    def test_hidden_from_non_admins(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            user_id = seed_user(s, email="worker@example.com", display_name="Worker")
            s.commit()
        cookie = issue_session(session_factory, user_id=user_id, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        resp = client.get("/admin/api/v1/agent_docs")

        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"

    def test_unknown_doc_returns_not_found(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/agent_docs/missing")

        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"
