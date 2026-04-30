"""Integration coverage for the OpenRouter deployment secret setting."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.capabilities.models import DeploymentSetting
from app.adapters.db.secrets.models import SecretEnvelope
from app.adapters.db.session import UnitOfWorkImpl
from app.adapters.llm.openrouter import (
    OPENROUTER_API_KEY_PURPOSE,
    OPENROUTER_API_KEY_SETTING,
    DeploymentOpenRouterConfigSource,
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
    return settings_fixture("openrouter-settings")


@pytest.fixture
def admin_engine() -> Iterator[Engine]:
    yield from engine_fixture()


@pytest.fixture
def session_factory(admin_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=admin_engine, expire_on_commit=False, class_=Session)


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
    owner: bool = True,
) -> str:
    with session_factory() as session:
        user_id = seed_user(session, email="or@example.com", display_name="OR")
        grant_deployment_admin(session, user_id=user_id)
        if owner:
            grant_deployment_owner(session, user_id=user_id)
        session.commit()
    return issue_session(session_factory, user_id=user_id, settings=settings)


def _uow_factory(
    session_factory: sessionmaker[Session],
) -> Callable[[], UnitOfWorkImpl]:
    return lambda: UnitOfWorkImpl(session_factory)


def test_owner_write_stores_envelope_and_returns_display_stub_only(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    plaintext = "sk-or-secret-test-value"
    client.cookies.set(
        SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings, owner=True)
    )

    resp = client.put(
        f"/admin/api/v1/settings/{OPENROUTER_API_KEY_SETTING}",
        json={"value": plaintext},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["key"] == OPENROUTER_API_KEY_SETTING
    assert body["kind"] == "secret"
    assert body["value"] == {"display_stub": "********"}
    assert plaintext not in resp.text

    with session_factory() as session, tenant_agnostic():
        setting = session.get(DeploymentSetting, OPENROUTER_API_KEY_SETTING)
        envelopes = session.scalars(select(SecretEnvelope)).all()
        audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "deployment_setting.updated")
        ).all()

    assert setting is not None
    assert isinstance(setting.value, str)
    assert setting.value != plaintext
    assert len(envelopes) == 1
    assert envelopes[0].id == setting.value
    assert envelopes[0].purpose == OPENROUTER_API_KEY_PURPOSE
    assert envelopes[0].owner_entity_kind == "deployment_setting"
    assert envelopes[0].owner_entity_id == OPENROUTER_API_KEY_SETTING
    assert len(audits) == 1
    assert plaintext not in str(audits[0].diff)


def test_get_returns_display_stub_only_after_write(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    plaintext = "sk-or-secret-for-read"
    client.cookies.set(
        SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings, owner=True)
    )
    write_resp = client.put(
        f"/admin/api/v1/settings/{OPENROUTER_API_KEY_SETTING}",
        json={"value": plaintext},
    )
    assert write_resp.status_code == 200, write_resp.text

    resp = client.get("/admin/api/v1/settings")

    assert resp.status_code == 200, resp.text
    target = next(
        row
        for row in resp.json()["settings"]
        if row["key"] == OPENROUTER_API_KEY_SETTING
    )
    assert target["value"] == {"display_stub": "********"}
    assert set(target["value"]) == {"display_stub"}
    assert plaintext not in resp.text


def test_config_source_resolves_admin_written_key_over_env(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    plaintext = "sk-or-db-wins"
    client.cookies.set(
        SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings, owner=True)
    )
    resp = client.put(
        f"/admin/api/v1/settings/{OPENROUTER_API_KEY_SETTING}",
        json={"value": plaintext},
    )
    assert resp.status_code == 200, resp.text

    source = DeploymentOpenRouterConfigSource(
        env_api_key=SecretStr("sk-or-env-loses"),
        root_key=settings.root_key,
        uow_factory=_uow_factory(session_factory),
    )
    resolved = source.api_key()

    assert resolved is not None
    assert resolved.get_secret_value() == plaintext
