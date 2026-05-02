"""Unit coverage for the host-only root-key rotation helper."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.secrets.models import RootKeySlot, SecretEnvelope
from app.adapters.db.secrets.repositories import SqlAlchemySecretEnvelopeRepository
from app.adapters.db.session import make_engine
from app.adapters.storage.envelope import Aes256GcmEnvelope, compute_key_fingerprint
from app.adapters.storage.ports import EnvelopeOwner
from app.admin import rotate_root_key
from app.config import Settings
from app.tenancy import tenant_agnostic

OLD_KEY = base64.b64encode(b"o" * 32).decode("ascii")
NEW_KEY = base64.b64encode(b"n" * 32).decode("ascii")
PINNED = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


class _FrozenClock:
    def now(self) -> datetime:
        return PINNED


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


def _settings(root_key: str = OLD_KEY) -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr(root_key),
    )


def test_start_rotation_creates_active_and_retired_slots(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "new.key"
    key_path.write_text(NEW_KEY, encoding="utf-8")
    key_path.chmod(0o600)
    monkeypatch.setenv("CREWDAY_ROOT_KEY", OLD_KEY)

    with session_factory() as session:
        result = rotate_root_key.start_rotation(
            session,
            settings=_settings(),
            new_key=bytearray(NEW_KEY.encode("utf-8")),
            new_key_ref=f"file:{key_path}",
            clock=_FrozenClock(),
        )
        session.commit()

    assert result.action == "started"
    assert result.active_key_fp == compute_key_fingerprint(SecretStr(NEW_KEY)).hex()
    assert result.legacy_key_fp == compute_key_fingerprint(SecretStr(OLD_KEY)).hex()

    with session_factory() as session, tenant_agnostic():
        slots = session.scalars(select(RootKeySlot)).all()
        audit = session.scalars(select(AuditLog)).one()
    assert {slot.key_fp for slot in slots} == {
        compute_key_fingerprint(SecretStr(OLD_KEY)),
        compute_key_fingerprint(SecretStr(NEW_KEY)),
    }
    assert len([slot for slot in slots if slot.is_active]) == 1
    retired = next(slot for slot in slots if not slot.is_active)
    assert retired.key_ref == rotate_root_key.ENV_ROOT_KEY_REF
    # ``UtcDateTime`` (cd-xma93) returns aware UTC on every dialect.
    assert retired.retired_at == PINNED
    assert retired.purge_after == PINNED + rotate_root_key.ROTATION_WINDOW
    assert audit.action == "key_rotation.started"
    assert audit.via == "cli"


def test_reencrypt_and_finalize_rotation(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "new.key"
    key_path.write_text(NEW_KEY, encoding="utf-8")
    key_path.chmod(0o600)
    settings = _settings()
    monkeypatch.setenv("CREWDAY_ROOT_KEY", OLD_KEY)

    with session_factory() as session:
        repo = SqlAlchemySecretEnvelopeRepository(session)
        old_env = Aes256GcmEnvelope(
            SecretStr(OLD_KEY), repository=repo, clock=_FrozenClock()
        )
        owner = EnvelopeOwner(kind="deployment_setting", id="smtp.password")
        pointer = old_env.encrypt(b"secret", purpose="smtp.password", owner=owner)
        rotate_root_key.start_rotation(
            session,
            settings=settings,
            new_key=bytearray(NEW_KEY.encode("utf-8")),
            new_key_ref=f"file:{key_path}",
            clock=_FrozenClock(),
        )
        reencrypt = rotate_root_key.reencrypt_legacy_rows(
            session,
            settings=settings,
            clock=_FrozenClock(),
        )
        finalized = rotate_root_key.finalize_rotation(
            session,
            settings=settings,
            finalize_now=True,
            clock=_FrozenClock(),
        )
        session.commit()

    assert reencrypt.rows_reencrypted == 1
    assert finalized.slots_purged == 1

    with session_factory() as session, tenant_agnostic():
        row = session.scalars(select(SecretEnvelope)).one()
        slots = session.scalars(select(RootKeySlot)).all()
        repo = SqlAlchemySecretEnvelopeRepository(session)
        new_env = Aes256GcmEnvelope(SecretStr(NEW_KEY), repository=repo)
        plaintext = new_env.decrypt(pointer, purpose="smtp.password")

    assert row.key_fp == compute_key_fingerprint(SecretStr(NEW_KEY))
    # ``UtcDateTime`` (cd-xma93) returns aware UTC on every dialect.
    assert row.rotated_at == PINNED
    assert plaintext == b"secret"
    assert len(slots) == 1
    assert slots[0].is_active is True


def test_file_loader_requires_0600(tmp_path: Path) -> None:
    key_path = tmp_path / "new.key"
    key_path.write_text(NEW_KEY, encoding="utf-8")
    key_path.chmod(0o644)

    with pytest.raises(rotate_root_key.RootKeyRotationError, match="0600"):
        rotate_root_key.load_new_key_file(key_path)


def test_zero_bytearray_overwrites_key_material() -> None:
    key = bytearray(NEW_KEY.encode("utf-8"))

    rotate_root_key.zero_key_material(key)

    assert set(key) == {0}


def test_legacy_new_value_is_rejected_before_use() -> None:
    with pytest.raises(rotate_root_key.RootKeyRotationError, match="shell history"):
        rotate_root_key.main(["--new", "do-not-put-this-in-history"])
