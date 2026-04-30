"""Tests for SMTP DB-then-env configuration resolution."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.secrets.models  # noqa: F401
from app.adapters.db.base import Base
from app.adapters.db.capabilities.models import DeploymentSetting
from app.adapters.db.secrets.repositories import SqlAlchemySecretEnvelopeRepository
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.mail.smtp_config import (
    SMTP_BOUNCE_DOMAIN_SETTING,
    SMTP_FROM_SETTING,
    SMTP_HOST_SETTING,
    SMTP_PASSWORD_PURPOSE,
    SMTP_PASSWORD_SETTING,
    SMTP_PORT_SETTING,
    SMTP_TIMEOUT_SETTING,
    SMTP_USE_TLS_SETTING,
    SMTP_USER_SETTING,
    DeploymentSmtpConfigSource,
    SmtpConfig,
    SmtpConfigError,
    smtp_envelope_id_from_pointer,
)
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.adapters.storage.ports import EnvelopeOwner
from app.tenancy import tenant_agnostic

_ROOT = SecretStr("unit-smtp-root-key")
_PINNED = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def _uow_factory(
    session_factory: sessionmaker[Session],
) -> Callable[[], UnitOfWorkImpl]:
    return lambda: UnitOfWorkImpl(session_factory)


def _env_config(*, password: SecretStr | None = None) -> SmtpConfig:
    return SmtpConfig(
        host="smtp.env.example",
        port=587,
        from_addr="crew.day <env@example.com>",
        user="env-user",
        password=password or SecretStr("env-pass"),
        use_tls=True,
        timeout=10,
        bounce_domain="env-bounce.example",
    )


def _write_setting(
    session_factory: sessionmaker[Session],
    *,
    key: str,
    value: object,
) -> None:
    with session_factory() as session, tenant_agnostic():
        row = session.get(DeploymentSetting, key)
        if row is None:
            session.add(
                DeploymentSetting(
                    key=key,
                    value=value,
                    updated_at=_PINNED,
                    updated_by="test",
                )
            )
        else:
            row.value = value
        session.commit()


def _write_db_password(
    session_factory: sessionmaker[Session],
    *,
    root_key: SecretStr = _ROOT,
    plaintext: str,
) -> str:
    with session_factory() as session, tenant_agnostic():
        envelope = Aes256GcmEnvelope(
            root_key,
            repository=SqlAlchemySecretEnvelopeRepository(session),
        )
        pointer = envelope.encrypt(
            plaintext.encode("utf-8"),
            purpose=SMTP_PASSWORD_PURPOSE,
            owner=EnvelopeOwner(kind="deployment_setting", id=SMTP_PASSWORD_SETTING),
        )
        envelope_id = smtp_envelope_id_from_pointer(pointer)
        row = session.get(DeploymentSetting, SMTP_PASSWORD_SETTING)
        if row is None:
            session.add(
                DeploymentSetting(
                    key=SMTP_PASSWORD_SETTING,
                    value=envelope_id,
                    updated_at=_PINNED,
                    updated_by="test",
                )
            )
        else:
            row.value = envelope_id
        session.commit()
        return envelope_id


def test_env_config_is_used_when_db_rows_are_absent(
    session_factory: sessionmaker[Session],
) -> None:
    source = DeploymentSmtpConfigSource(
        env=_env_config(),
        root_key=_ROOT,
        uow_factory=_uow_factory(session_factory),
    )

    resolved = source.config()

    assert resolved.host == "smtp.env.example"
    assert resolved.port == 587
    assert resolved.user == "env-user"
    assert resolved.password is not None
    assert resolved.password.get_secret_value() == "env-pass"
    assert resolved.from_addr == "crew.day <env@example.com>"
    assert resolved.use_tls is True
    assert resolved.timeout == 10
    assert resolved.bounce_domain == "env-bounce.example"


def test_db_rows_take_precedence_over_env(
    session_factory: sessionmaker[Session],
) -> None:
    _write_setting(session_factory, key=SMTP_HOST_SETTING, value="smtp.db.example")
    _write_setting(session_factory, key=SMTP_PORT_SETTING, value=2525)
    _write_setting(session_factory, key=SMTP_USER_SETTING, value="db-user")
    _write_db_password(session_factory, plaintext="db-pass")
    _write_setting(session_factory, key=SMTP_FROM_SETTING, value="db@example.com")
    _write_setting(session_factory, key=SMTP_USE_TLS_SETTING, value=False)
    _write_setting(session_factory, key=SMTP_TIMEOUT_SETTING, value=20)
    _write_setting(
        session_factory, key=SMTP_BOUNCE_DOMAIN_SETTING, value="db-bounce.example"
    )
    source = DeploymentSmtpConfigSource(
        env=_env_config(),
        root_key=_ROOT,
        uow_factory=_uow_factory(session_factory),
    )

    resolved = source.config()

    assert resolved.host == "smtp.db.example"
    assert resolved.port == 2525
    assert resolved.user == "db-user"
    assert resolved.password is not None
    assert resolved.password.get_secret_value() == "db-pass"
    assert resolved.from_addr == "db@example.com"
    assert resolved.use_tls is False
    assert resolved.timeout == 20
    assert resolved.bounce_domain == "db-bounce.example"


def test_db_password_update_is_resolved_on_next_call_without_rebuild(
    session_factory: sessionmaker[Session],
) -> None:
    _write_db_password(session_factory, plaintext="first-pass")
    source = DeploymentSmtpConfigSource(
        env=_env_config(password=SecretStr("env-pass")),
        root_key=_ROOT,
        uow_factory=_uow_factory(session_factory),
    )

    first = source.config().password
    assert first is not None
    assert first.get_secret_value() == "first-pass"
    _write_db_password(session_factory, plaintext="second-pass")

    resolved = source.config()

    assert resolved.password is not None
    assert resolved.password.get_secret_value() == "second-pass"


def test_malformed_password_row_fails_closed_without_env_fallback(
    session_factory: sessionmaker[Session],
) -> None:
    _write_setting(
        session_factory,
        key=SMTP_PASSWORD_SETTING,
        value={"envelope_id": "not-the-storage-shape"},
    )
    source = DeploymentSmtpConfigSource(
        env=_env_config(password=SecretStr("env-pass")),
        root_key=_ROOT,
        uow_factory=_uow_factory(session_factory),
    )

    with pytest.raises(SmtpConfigError, match="malformed"):
        source.config()


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        (SMTP_PORT_SETTING, 0, "TCP port"),
        (SMTP_PORT_SETTING, 65536, "TCP port"),
        (SMTP_TIMEOUT_SETTING, 0, "positive"),
    ],
)
def test_invalid_numeric_db_rows_fail_closed(
    session_factory: sessionmaker[Session],
    key: str,
    value: int,
    match: str,
) -> None:
    _write_setting(session_factory, key=key, value=value)
    source = DeploymentSmtpConfigSource(
        env=_env_config(),
        root_key=_ROOT,
        uow_factory=_uow_factory(session_factory),
    )

    with pytest.raises(SmtpConfigError, match=match):
        source.config()


def test_db_load_failure_is_reported_as_config_error() -> None:
    class _BrokenUow:
        def __enter__(self) -> Session:
            raise SQLAlchemyError("database is down")

        def __exit__(self, *exc: object) -> None:
            return None

    source = DeploymentSmtpConfigSource(
        env=_env_config(),
        root_key=_ROOT,
        uow_factory=_BrokenUow,
    )

    with pytest.raises(SmtpConfigError, match="could not be loaded"):
        source.config()
