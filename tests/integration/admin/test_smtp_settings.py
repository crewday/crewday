"""Integration coverage for SMTP deployment settings."""

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
from app.adapters.mail.smtp_config import (
    SMTP_FROM_SETTING,
    SMTP_HOST_SETTING,
    SMTP_PASSWORD_DISPLAY_STUB,
    SMTP_PASSWORD_PURPOSE,
    SMTP_PASSWORD_SETTING,
    SMTP_PORT_SETTING,
    SMTP_TIMEOUT_SETTING,
    DeploymentSmtpConfigSource,
    SmtpConfig,
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
    return settings_fixture("smtp-settings")


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
        user_id = seed_user(session, email="smtp@example.com", display_name="SMTP")
        grant_deployment_admin(session, user_id=user_id)
        if owner:
            grant_deployment_owner(session, user_id=user_id)
        session.commit()
    return issue_session(session_factory, user_id=user_id, settings=settings)


def _uow_factory(
    session_factory: sessionmaker[Session],
) -> Callable[[], UnitOfWorkImpl]:
    return lambda: UnitOfWorkImpl(session_factory)


def test_owner_write_stores_password_envelope_and_returns_display_stub_only(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    plaintext = "smtp-secret-test-value"
    client.cookies.set(
        SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings, owner=True)
    )

    resp = client.put(
        f"/admin/api/v1/settings/{SMTP_PASSWORD_SETTING}",
        json={"value": plaintext},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["key"] == SMTP_PASSWORD_SETTING
    assert body["kind"] == "secret"
    assert body["value"] == {"display_stub": SMTP_PASSWORD_DISPLAY_STUB}
    assert plaintext not in resp.text

    with session_factory() as session, tenant_agnostic():
        setting = session.get(DeploymentSetting, SMTP_PASSWORD_SETTING)
        envelopes = session.scalars(select(SecretEnvelope)).all()
        audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "deployment_setting.updated")
        ).all()

    assert setting is not None
    assert isinstance(setting.value, str)
    assert setting.value != plaintext
    assert len(envelopes) == 1
    assert envelopes[0].id == setting.value
    assert envelopes[0].purpose == SMTP_PASSWORD_PURPOSE
    assert envelopes[0].owner_entity_kind == "deployment_setting"
    assert envelopes[0].owner_entity_id == SMTP_PASSWORD_SETTING
    assert len(audits) == 1
    assert plaintext not in str(audits[0].diff)


def test_get_returns_smtp_password_display_stub_only_after_write(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    plaintext = "smtp-secret-for-read"
    client.cookies.set(
        SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings, owner=True)
    )
    write_resp = client.put(
        f"/admin/api/v1/settings/{SMTP_PASSWORD_SETTING}",
        json={"value": plaintext},
    )
    assert write_resp.status_code == 200, write_resp.text

    resp = client.get("/admin/api/v1/settings")

    assert resp.status_code == 200, resp.text
    target = next(
        row for row in resp.json()["settings"] if row["key"] == SMTP_PASSWORD_SETTING
    )
    assert target["value"] == {"display_stub": SMTP_PASSWORD_DISPLAY_STUB}
    assert set(target["value"]) == {"display_stub"}
    assert plaintext not in resp.text


def test_nonsecret_smtp_settings_round_trip_as_plain_values(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    client.cookies.set(
        SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings, owner=True)
    )

    host_resp = client.put(
        f"/admin/api/v1/settings/{SMTP_HOST_SETTING}",
        json={"value": "smtp.db.example"},
    )
    from_resp = client.put(
        f"/admin/api/v1/settings/{SMTP_FROM_SETTING}",
        json={"value": "crew.day <db@example.com>"},
    )
    list_resp = client.get("/admin/api/v1/settings")

    assert host_resp.status_code == 200, host_resp.text
    assert from_resp.status_code == 200, from_resp.text
    rows = {row["key"]: row["value"] for row in list_resp.json()["settings"]}
    assert rows[SMTP_HOST_SETTING] == "smtp.db.example"
    assert rows[SMTP_FROM_SETTING] == "crew.day <db@example.com>"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (SMTP_PORT_SETTING, 0),
        (SMTP_PORT_SETTING, 65536),
        (SMTP_TIMEOUT_SETTING, 0),
    ],
)
def test_invalid_numeric_smtp_settings_refuse_before_write(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
    key: str,
    value: int,
) -> None:
    client.cookies.set(
        SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings, owner=True)
    )

    resp = client.put(
        f"/admin/api/v1/settings/{key}",
        json={"value": value},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "invalid_setting_value"
    with session_factory() as session, tenant_agnostic():
        assert session.get(DeploymentSetting, key) is None


def test_config_source_resolves_admin_written_password_over_env(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    plaintext = "smtp-db-wins"
    client.cookies.set(
        SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings, owner=True)
    )
    resp = client.put(
        f"/admin/api/v1/settings/{SMTP_PASSWORD_SETTING}",
        json={"value": plaintext},
    )
    assert resp.status_code == 200, resp.text

    source = DeploymentSmtpConfigSource(
        env=SmtpConfig(
            host="smtp.env.example",
            port=587,
            from_addr="env@example.com",
            user="env-user",
            password=SecretStr("smtp-env-loses"),
            use_tls=True,
            timeout=10,
            bounce_domain=None,
        ),
        root_key=settings.root_key,
        uow_factory=_uow_factory(session_factory),
    )
    resolved = source.config()

    assert resolved.password is not None
    assert resolved.password.get_secret_value() == plaintext
