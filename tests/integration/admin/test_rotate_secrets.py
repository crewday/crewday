"""Integration coverage for host-only secret rotation helpers."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.capabilities.models import DeploymentSetting
from app.adapters.db.secrets.models import SecretEnvelope
from app.admin.rotate_secrets import (
    DEFAULT_HMAC_PURPOSES,
    SmtpRotationCredentials,
    parse_smtp_credentials,
    rotate_hmac_signing_key,
    rotate_openrouter_key,
    rotate_session_secret,
    rotate_smtp_credentials,
    secret_bytes_from_input,
)
from app.config import Settings
from app.security.hmac_signer import HMAC_KEY_BYTES
from app.tenancy import tenant_agnostic
from app.util.clock import FrozenClock
from tests.unit.api.admin._helpers import engine_fixture, settings_fixture

PINNED = datetime(2026, 4, 30, 13, 0, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return settings_fixture("rotate-secrets")


@pytest.fixture
def admin_engine() -> Iterator[Engine]:
    yield from engine_fixture()


@pytest.fixture
def session_factory(admin_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=admin_engine, expire_on_commit=False, class_=Session)


def test_rotate_smtp_credentials_writes_settings_envelope_and_audit(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    probes: list[SmtpRotationCredentials] = []
    credentials = parse_smtp_credentials(
        b'{"host":"smtp.example","port":587,"from":"Crew <ops@example.com>",'
        b'"user":"crew","password":"smtp-secret","use_tls":true}'
    )

    with session_factory() as session:
        result = rotate_smtp_credentials(
            session,
            settings=settings,
            credentials=credentials,
            probe=probes.append,
            clock=FrozenClock(PINNED),
        )
        session.commit()

    assert result.action == "secrets.smtp.rotated"
    assert probes == [credentials]
    with session_factory() as session, tenant_agnostic():
        settings_rows = session.scalars(select(DeploymentSetting)).all()
        envelopes = session.scalars(select(SecretEnvelope)).all()
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "secrets.smtp.rotated")
        ).one()
    rows = {row.key: row.value for row in settings_rows}
    assert rows["smtp.host"] == "smtp.example"
    assert rows["smtp.password_envelope_id"] == envelopes[0].id
    assert envelopes[0].purpose == "smtp.password"
    assert audit.via == "cli"
    assert "smtp-secret" not in str(audit.diff)


def test_rotate_smtp_probe_failure_leaves_no_rows(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    credentials = parse_smtp_credentials(
        b'{"host":"smtp.example","from":"ops@example.com","password":"smtp-secret"}'
    )

    def fail_probe(_: SmtpRotationCredentials) -> None:
        raise RuntimeError("probe failed")

    with session_factory() as session:
        with pytest.raises(RuntimeError, match="probe failed"):
            rotate_smtp_credentials(
                session,
                settings=settings,
                credentials=credentials,
                probe=fail_probe,
                clock=FrozenClock(PINNED),
            )
        session.rollback()

    with session_factory() as session, tenant_agnostic():
        assert session.scalars(select(DeploymentSetting)).all() == []
        assert session.scalars(select(SecretEnvelope)).all() == []


def test_rotate_openrouter_key_writes_envelope_and_audit(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    probes: list[tuple[str, str]] = []

    def probe(api_key: str, *, base_url: str) -> None:
        probes.append((api_key, base_url))

    with session_factory() as session:
        result = rotate_openrouter_key(
            session,
            settings=settings,
            api_key=bytearray(b"sk-or-new"),
            probe=probe,
            clock=FrozenClock(PINNED),
        )
        session.commit()

    assert result.action == "secrets.openrouter.rotated"
    assert probes == [("sk-or-new", "https://openrouter.ai/api/v1")]
    with session_factory() as session, tenant_agnostic():
        setting = session.get(DeploymentSetting, "openrouter.api_key_envelope_id")
        envelope = session.scalars(select(SecretEnvelope)).one()
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "secrets.openrouter.rotated")
        ).one()
    assert setting is not None
    assert setting.value == envelope.id
    assert envelope.purpose == "openrouter.api_key"
    assert audit.via == "cli"
    assert "sk-or-new" not in str(audit.diff)


def test_rotate_hmac_signing_key_rotates_default_purposes_and_audits(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    with session_factory() as session:
        result = rotate_hmac_signing_key(
            session,
            settings=settings,
            new_key=bytearray(b"h" * HMAC_KEY_BYTES),
            clock=FrozenClock(PINNED),
        )
        session.commit()

    assert result.rotated == DEFAULT_HMAC_PURPOSES
    assert result.legacy_until == PINNED + timedelta(hours=72)
    with session_factory() as session, tenant_agnostic():
        envelopes = session.scalars(select(SecretEnvelope)).all()
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "secrets.hmac.rotated")
        ).one()
    assert {row.owner_entity_id for row in envelopes} == set(DEFAULT_HMAC_PURPOSES)
    assert audit.via == "cli"


def test_rotate_session_secret_writes_envelope_and_audits(
    session_factory: sessionmaker[Session], settings: Settings
) -> None:
    with session_factory() as session:
        result = rotate_session_secret(
            session,
            settings=settings,
            new_key=bytearray(b"s" * 32),
            clock=FrozenClock(PINNED),
        )
        session.commit()

    assert result.action == "secrets.session.rotated"
    with session_factory() as session, tenant_agnostic():
        envelope = session.scalars(select(SecretEnvelope)).one()
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "secrets.session.rotated")
        ).one()
    assert envelope.owner_entity_id == "session.signing_key"
    assert audit.via == "cli"


def test_exact_width_secret_input_accepts_raw_bytes() -> None:
    raw = bytes(range(32))

    assert secret_bytes_from_input(raw, exact_len=32) == bytearray(raw)
