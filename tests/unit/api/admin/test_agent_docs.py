"""Unit tests for :mod:`app.api.admin.agent_docs`."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.llm.models import AgentDoc, AgentDocRevision
from app.auth.session import SESSION_COOKIE_NAME
from app.config import Settings
from app.domain.agent.system_docs import agent_doc_metadata_hash
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
        assert len(overview["metadata_default_hash"]) == 16
        assert overview["approx_token_count"] > 0

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
        assert body["notes"] is None
        assert body["approx_token_count"] == (len(body["body_md"].strip()) + 3) // 4

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
                        metadata_default_hash=agent_doc_metadata_hash(["admin"]),
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

    def test_updates_doc_and_snapshots_previous_body(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        admin_id = install_admin_cookie(client, session_factory, settings)
        before = client.get("/admin/api/v1/agent_docs/crewday_overview").json()

        resp = client.put(
            "/admin/api/v1/agent_docs/crewday_overview",
            json={
                "body_md": "# Crewday\n\nEdited body.\n",
                "roles": ["admin", "manager"],
                "notes": "Clarified admin boundaries",
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["body_md"] == "# Crewday\n\nEdited body.\n"
        assert body["roles"] == ["manager", "admin"]
        assert body["notes"] == "Clarified admin boundaries"
        assert body["version"] == before["version"] + 1
        assert body["updated_at"] != before["updated_at"]
        assert body["is_customised"] is True
        assert body["approx_token_count"] == 6

        revisions = client.get(
            "/admin/api/v1/agent_docs/crewday_overview/revisions"
        ).json()
        assert len(revisions) == 1
        assert revisions[0]["version"] == before["version"]
        assert revisions[0]["body_md"] == before["body_md"]
        assert revisions[0]["roles"] == before["roles"]
        assert revisions[0]["notes"] is None
        assert revisions[0]["created_by_user_id"] == admin_id
        assert revisions[0]["approx_token_count"] == before["approx_token_count"]

    @pytest.mark.parametrize(
        ("payload", "error_fragment"),
        [
            (
                {"body_md": "# Ok\n", "roles": [], "notes": None},
                "at least 1 item",
            ),
            (
                {"body_md": "# Ok\n", "roles": ["manager", "owner"], "notes": None},
                "unknown agent doc role",
            ),
            (
                {
                    "body_md": "# Ok\n",
                    "roles": ["manager", "manager"],
                    "notes": None,
                },
                "duplicate agent doc role",
            ),
            (
                {"body_md": "   \n\t", "roles": ["manager"], "notes": None},
                "body_md must not be blank",
            ),
            (
                {
                    "body_md": "# Ok\n",
                    "roles": ["manager"],
                    "slug": "crewday_overview",
                },
                "Extra inputs are not permitted",
            ),
        ],
    )
    def test_rejects_invalid_updates(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        payload: dict[str, object],
        error_fragment: str,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.put(
            "/admin/api/v1/agent_docs/crewday_overview",
            json=payload,
        )

        assert resp.status_code == 422
        assert error_fragment in resp.text

    def test_reset_to_default_restores_body_roles_and_clears_notes(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        custom = client.put(
            "/admin/api/v1/agent_docs/crewday_overview",
            json={
                "body_md": "# Crewday\n\nCustom body.\n",
                "roles": ["admin"],
                "notes": "Operator edit",
            },
        ).json()

        resp = client.post(
            "/admin/api/v1/agent_docs/crewday_overview/reset-to-default",
            json={"notes": "Reset after review"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == custom["version"] + 1
        assert body["roles"] == ["manager", "employee", "admin"]
        assert "crew.day helps short-term-rental" in body["body_md"]
        assert body["notes"] is None
        assert body["is_customised"] is False

        revisions = client.get(
            "/admin/api/v1/agent_docs/crewday_overview/revisions"
        ).json()
        assert [row["version"] for row in revisions] == [2, 1]
        assert revisions[0]["body_md"] == custom["body_md"]
        assert revisions[0]["roles"] == ["admin"]
        assert revisions[0]["notes"] == "Reset after review"

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
        assert (
            client.get("/admin/api/v1/agent_docs/crewday_overview").status_code == 404
        )
        assert (
            client.get(
                "/admin/api/v1/agent_docs/crewday_overview/revisions"
            ).status_code
            == 404
        )
        assert (
            client.put(
                "/admin/api/v1/agent_docs/crewday_overview",
                json={"body_md": "# No\n", "roles": ["manager"]},
            ).status_code
            == 404
        )
        assert (
            client.put(
                "/admin/api/v1/agent_docs/crewday_overview",
                json={"body_md": " ", "roles": ["owner"]},
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/admin/api/v1/agent_docs/crewday_overview/reset-to-default"
            ).status_code
            == 404
        )

        with session_factory() as s:
            revisions = s.scalars(select(AgentDocRevision)).all()
        assert revisions == []

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
