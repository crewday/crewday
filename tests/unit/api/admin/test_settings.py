"""Unit tests for :mod:`app.api.admin.settings`.

Covers ``GET /settings`` + ``PUT /settings/{key}`` per spec §12
"Admin surface":

* GET surfaces every registered key with its resolved value;
  defaults shine through when no row exists.
* PUT 422s ``unknown_setting`` for an unknown key, ``root_only_setting``
  for ``trusted_interfaces``, and ``invalid_setting_value`` for
  a wrong-typed value.
* Non-owner admins still hit the surface-invisible 404 owner gate;
  deployment owners can update mutable settings.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.capabilities.models import DeploymentSetting
from app.api.admin.settings import (
    ERROR_ROOT_ONLY,
    ERROR_UNKNOWN_KEY,
    ERROR_VALUE_TYPE,
)
from app.auth.session import SESSION_COOKIE_NAME
from app.config import Settings
from app.tenancy import tenant_agnostic
from tests.unit.api.admin._helpers import (
    build_client,
    engine_fixture,
    grant_deployment_admin,
    grant_deployment_owner,
    issue_session,
    seed_user,
    settings_fixture,
)


@pytest.fixture
def settings() -> Settings:
    return settings_fixture("settings")


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
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    owner: bool = False,
) -> str:
    with session_factory() as s:
        user_id = seed_user(s, email="ada@example.com", display_name="Ada")
        grant_deployment_admin(s, user_id=user_id)
        if owner:
            grant_deployment_owner(s, user_id=user_id)
        s.commit()
    return issue_session(session_factory, user_id=user_id, settings=settings)


class TestListSettings:
    def test_returns_every_registered_key(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.get("/admin/api/v1/settings")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        keys = {row["key"] for row in body["settings"]}
        # Every :class:`DeploymentSettings` field plus the root-only
        # ``trusted_interfaces`` advertised key.
        assert {
            "signup_enabled",
            "signup_throttle_overrides",
            "require_passkey_attestation",
            "llm_default_budget_cents_30d",
            "captcha_required",
            "trusted_interfaces",
        } <= keys

    def test_root_only_flag_present_for_trusted_interfaces(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        body = client.get("/admin/api/v1/settings").json()
        target = next(
            row for row in body["settings"] if row["key"] == "trusted_interfaces"
        )
        assert target["root_only"] is True

    def test_persisted_row_overrides_default(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s, tenant_agnostic():
            s.add(
                DeploymentSetting(
                    key="captcha_required",
                    value=False,
                    updated_at=datetime.now(UTC),
                    updated_by="seed",
                )
            )
            s.commit()
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        body = client.get("/admin/api/v1/settings").json()
        target = next(
            row for row in body["settings"] if row["key"] == "captcha_required"
        )
        assert target["value"] is False
        assert target["updated_by"] == "seed"


class TestUpdateSetting:
    def test_unknown_key_returns_422(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.put(
            "/admin/api/v1/settings/no_such_key",
            json={"value": True},
        )
        assert resp.status_code == 422
        assert resp.json().get("error") == ERROR_UNKNOWN_KEY

    def test_root_only_key_refused_with_typed_error(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.put(
            "/admin/api/v1/settings/trusted_interfaces",
            json={"value": ["lo"]},
        )
        assert resp.status_code == 422
        assert resp.json().get("error") == ERROR_ROOT_ONLY

    def test_non_owner_admin_404s_on_owner_only_write(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.put(
            "/admin/api/v1/settings/signup_enabled",
            json={"value": False},
        )
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"

    def test_deployment_owner_updates_setting_and_audits(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings, owner=True)
        )
        resp = client.put(
            "/admin/api/v1/settings/signup_enabled",
            json={"value": False},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["value"] is False

        with session_factory() as s, tenant_agnostic():
            row = s.get(DeploymentSetting, "signup_enabled")
            audits = s.scalars(
                select(AuditLog).where(AuditLog.action == "deployment_setting.updated")
            ).all()
        assert row is not None
        assert row.value is False
        assert len(audits) == 1
        assert audits[0].actor_was_owner_member is True

    def test_invalid_type_for_bool_setting(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings, owner=True)
        )
        resp = client.put(
            "/admin/api/v1/settings/signup_enabled",
            json={"value": "definitely-not-a-bool"},
        )
        assert resp.status_code == 422
        assert resp.json().get("error") == ERROR_VALUE_TYPE

    def test_audit_row_not_emitted_when_owner_gate_fails(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        client.put(
            "/admin/api/v1/settings/signup_enabled",
            json={"value": False},
        )
        with session_factory() as s, tenant_agnostic():
            audits = s.scalars(
                select(AuditLog).where(AuditLog.action == "deployment_setting.updated")
            ).all()
            assert audits == []


class TestUpdateSettingErrorContract:
    """Pin the error-code constants to their spec literals."""

    def test_error_constants_are_stable(self) -> None:
        assert ERROR_UNKNOWN_KEY == "unknown_setting"
        assert ERROR_ROOT_ONLY == "root_only_setting"
        assert ERROR_VALUE_TYPE == "invalid_setting_value"
