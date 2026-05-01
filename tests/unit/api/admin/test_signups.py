"""Unit tests for :mod:`app.api.admin.signups` (cd-1h7k).

Signup abuse surfacing is deployment-scoped. Pre-workspace signals
such as burst-rate trips have no workspace to attach to, so the
placeholder lives under ``/admin/api/v1/signups`` and uses the same
surface-invisible 404 wall as the rest of the deployment admin API.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from tests.unit.api.admin._helpers import (
    build_client,
    engine_fixture,
    install_admin_cookie,
    settings_fixture,
)


@pytest.fixture
def settings() -> Settings:
    return settings_fixture("signups")


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


class TestAdminSignups:
    def test_admin_gets_200_empty_envelope(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/signups")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"data": [], "next_cursor": None, "has_more": False}

    def test_non_admin_gets_surface_invisible_404(self, client: TestClient) -> None:
        resp = client.get("/admin/api/v1/signups")

        assert resp.status_code == 404, resp.text
        assert resp.json().get("error") == "not_found"

    def test_accepts_kind_cursor_and_limit(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.get(
            "/admin/api/v1/signups?kind=burst_rate&cursor=abc&limit=25"
        )

        assert resp.status_code == 200, resp.text

    def test_rejects_unknown_kind(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/signups?kind=nope")

        assert resp.status_code == 422

    def test_rejects_limit_above_ceiling(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/signups?limit=501")

        assert resp.status_code == 422

    def test_rejects_non_positive_limit(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/signups?limit=0")

        assert resp.status_code == 422


class TestAdminSignupsOpenApi:
    def test_operation_shape(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        schema = client.get("/openapi.json").json()
        op = schema["paths"]["/admin/api/v1/signups"]["get"]

        assert op["operationId"] == "admin.signups.list"
        assert "admin" in op.get("tags", [])
        assert op.get("x-cli", {}).get("group") == "admin"
        assert op["x-cli"].get("verb") == "signups-list"
        assert op["x-cli"].get("mutates") is False
        assert op["parameters"][0]["name"] == "kind"
        assert op["parameters"][0]["schema"]["anyOf"][0]["enum"] == [
            "burst_rate",
            "distinct_emails_one_ip",
            "repeat_email",
            "quota_near_breach",
        ]
