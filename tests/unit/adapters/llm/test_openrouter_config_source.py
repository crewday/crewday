"""Tests for OpenRouter DB-then-env key resolution."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.secrets.models  # noqa: F401
from app.adapters.db.base import Base
from app.adapters.db.capabilities.models import DeploymentSetting
from app.adapters.db.secrets.repositories import SqlAlchemySecretEnvelopeRepository
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.llm.openrouter import (
    OPENROUTER_API_KEY_PURPOSE,
    OPENROUTER_API_KEY_SETTING,
    DeploymentOpenRouterConfigSource,
    LlmTransportError,
    openrouter_envelope_id_from_pointer,
)
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.adapters.storage.ports import EnvelopeOwner
from app.tenancy import tenant_agnostic

_ROOT = SecretStr("unit-openrouter-root-key")
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


def _write_db_key(
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
            purpose=OPENROUTER_API_KEY_PURPOSE,
            owner=EnvelopeOwner(
                kind="deployment_setting",
                id=OPENROUTER_API_KEY_SETTING,
            ),
        )
        envelope_id = openrouter_envelope_id_from_pointer(pointer)
        row = session.get(DeploymentSetting, OPENROUTER_API_KEY_SETTING)
        if row is None:
            session.add(
                DeploymentSetting(
                    key=OPENROUTER_API_KEY_SETTING,
                    value=envelope_id,
                    updated_at=_PINNED,
                    updated_by="test",
                )
            )
        else:
            row.value = envelope_id
        session.commit()
        return envelope_id


def test_env_key_is_used_when_db_row_is_absent(
    session_factory: sessionmaker[Session],
) -> None:
    source = DeploymentOpenRouterConfigSource(
        env_api_key=SecretStr("sk-env"),
        root_key=_ROOT,
        uow_factory=_uow_factory(session_factory),
    )

    assert source.api_key() is not None
    assert source.api_key().get_secret_value() == "sk-env"


def test_db_key_takes_precedence_over_env(
    session_factory: sessionmaker[Session],
) -> None:
    _write_db_key(session_factory, plaintext="sk-db")
    source = DeploymentOpenRouterConfigSource(
        env_api_key=SecretStr("sk-env"),
        root_key=_ROOT,
        uow_factory=_uow_factory(session_factory),
    )

    resolved = source.api_key()

    assert resolved is not None
    assert resolved.get_secret_value() == "sk-db"


def test_db_update_is_resolved_on_next_call_without_rebuild(
    session_factory: sessionmaker[Session],
) -> None:
    _write_db_key(session_factory, plaintext="sk-first")
    source = DeploymentOpenRouterConfigSource(
        env_api_key=SecretStr("sk-env"),
        root_key=_ROOT,
        uow_factory=_uow_factory(session_factory),
    )

    assert source.api_key() is not None
    assert source.api_key().get_secret_value() == "sk-first"
    _write_db_key(session_factory, plaintext="sk-second")

    resolved = source.api_key()

    assert resolved is not None
    assert resolved.get_secret_value() == "sk-second"


def test_missing_db_and_env_returns_none(
    session_factory: sessionmaker[Session],
) -> None:
    source = DeploymentOpenRouterConfigSource(
        env_api_key=None,
        root_key=_ROOT,
        uow_factory=_uow_factory(session_factory),
    )

    assert source.api_key() is None


def test_malformed_db_row_fails_closed_without_env_fallback(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, tenant_agnostic():
        session.add(
            DeploymentSetting(
                key=OPENROUTER_API_KEY_SETTING,
                value={"envelope_id": "not-the-storage-shape"},
                updated_at=_PINNED,
                updated_by="test",
            )
        )
        session.commit()
    source = DeploymentOpenRouterConfigSource(
        env_api_key=SecretStr("sk-env"),
        root_key=_ROOT,
        uow_factory=_uow_factory(session_factory),
    )

    with pytest.raises(LlmTransportError, match="malformed"):
        source.api_key()


def test_missing_envelope_fails_closed_without_env_fallback(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, tenant_agnostic():
        session.add(
            DeploymentSetting(
                key=OPENROUTER_API_KEY_SETTING,
                value="01HWA00000000000000000MISS",
                updated_at=_PINNED,
                updated_by="test",
            )
        )
        session.commit()
    source = DeploymentOpenRouterConfigSource(
        env_api_key=SecretStr("sk-env"),
        root_key=_ROOT,
        uow_factory=_uow_factory(session_factory),
    )

    with pytest.raises(LlmTransportError, match="could not be decrypted"):
        source.api_key()
