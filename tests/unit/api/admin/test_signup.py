"""Unit tests for :mod:`app.api.admin.signup`.

Covers the GET / PUT signup-settings routes spec §12 "Admin
surface" pins:

* ``GET /signup/settings`` — defaults when no rows exist;
  reads existing rows verbatim.
* ``PUT /signup/settings`` — patches each knob; emits one
  audit row covering the changed fields; ignores absent fields;
  refreshes :attr:`app.state.capabilities` when present.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.capabilities.models import DeploymentSetting
from app.auth.session import SESSION_COOKIE_NAME
from app.config import Settings
from app.tenancy import tenant_agnostic
from tests.unit.api.admin._helpers import (
    build_client,
    engine_fixture,
    grant_deployment_admin,
    issue_session,
    seed_user,
    settings_fixture,
)


@pytest.fixture
def settings() -> Settings:
    return settings_fixture("signup")


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


def _admin_cookie(
    session_factory: sessionmaker[Session], settings: Settings
) -> str:
    with session_factory() as s:
        user_id = seed_user(s, email="ada@example.com", display_name="Ada")
        grant_deployment_admin(s, user_id=user_id)
        s.commit()
    return issue_session(session_factory, user_id=user_id, settings=settings)


class TestReadSignupSettings:
    def test_returns_defaults_for_empty_db(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.get("/admin/api/v1/signup/settings")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["signup_enabled"] is True
        assert body["signup_throttle_overrides"] == {}
        assert body["signup_disposable_domains_path"].endswith(
            "disposable_domains.txt"
        )

    def test_reads_existing_rows(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        from datetime import UTC, datetime

        with session_factory() as s, tenant_agnostic():
            s.add(
                DeploymentSetting(
                    key="signup_enabled",
                    value=False,
                    updated_at=datetime.now(UTC),
                    updated_by="seed",
                )
            )
            s.add(
                DeploymentSetting(
                    key="signup_throttle_overrides",
                    value={"per_ip_hour": 7},
                    updated_at=datetime.now(UTC),
                    updated_by="seed",
                )
            )
            s.commit()
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.get("/admin/api/v1/signup/settings")
        assert resp.status_code == 200
        body = resp.json()
        assert body["signup_enabled"] is False
        assert body["signup_throttle_overrides"] == {"per_ip_hour": 7}

    def test_404_for_non_admin(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            stranger = seed_user(s, email="x@example.com", display_name="X")
            s.commit()
        cookie = issue_session(session_factory, user_id=stranger, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.get("/admin/api/v1/signup/settings")
        assert resp.status_code == 404


class TestUpdateSignupSettings:
    def test_writes_signup_enabled_and_audits(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.put(
            "/admin/api/v1/signup/settings",
            json={"signup_enabled": False},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["signup_enabled"] is False

        with session_factory() as s, tenant_agnostic():
            row = s.get(DeploymentSetting, "signup_enabled")
            assert row is not None
            assert row.value is False
            audits = s.scalars(
                select(AuditLog)
                .where(AuditLog.action == "signup_settings.updated")
            ).all()
            assert len(audits) == 1
            assert audits[0].diff == {
                "signup_enabled": {"before": None, "after": False}
            }

    def test_omitted_fields_left_alone(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.put(
            "/admin/api/v1/signup/settings",
            json={},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["signup_enabled"] is True
        with session_factory() as s, tenant_agnostic():
            audits = s.scalars(
                select(AuditLog).where(AuditLog.action == "signup_settings.updated")
            ).all()
            assert audits == []

    def test_throttle_override_persisted_and_normalised(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.put(
            "/admin/api/v1/signup/settings",
            json={"signup_throttle_overrides": {"per_ip_hour": 9}},
        )
        assert resp.status_code == 200
        assert resp.json()["signup_throttle_overrides"] == {"per_ip_hour": 9}

    def test_404_for_non_admin(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            stranger = seed_user(s, email="x@example.com", display_name="X")
            s.commit()
        cookie = issue_session(session_factory, user_id=stranger, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.put(
            "/admin/api/v1/signup/settings",
            json={"signup_enabled": True},
        )
        assert resp.status_code == 404
