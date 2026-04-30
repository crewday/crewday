"""Tests for deployment-wide HMAC signer slots."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.base import Base
from app.auth.keys import derive_subkey
from app.config import Settings
from app.security.hmac_signer import HMAC_KEY_BYTES, HmacSigner, rotate_hmac_key
from app.util.clock import FrozenClock

_PINNED = datetime(2026, 4, 29, 10, 0, 0, tzinfo=UTC)
_PURPOSE = "guest-link"


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-hmac-signer-root-key"),
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(
        dbapi_connection: object, _connection_record: object
    ) -> None:
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        yield session


def test_signer_falls_back_to_root_key_subkey(
    db_session: Session, settings: Settings
) -> None:
    signer = HmacSigner(db_session, settings=settings, clock=FrozenClock(_PINNED))
    message = b"guest-link-payload"

    assert signer.current_key(purpose=_PURPOSE) == derive_subkey(
        settings.root_key, purpose=_PURPOSE
    )
    signature = signer.sign(message, purpose=_PURPOSE)

    assert signer.verify(message, signature, purpose=_PURPOSE)
    assert not signer.verify(message + b"!", signature, purpose=_PURPOSE)


def test_rotation_preserves_previous_primary_until_purge_after(
    db_session: Session, settings: Settings
) -> None:
    before = HmacSigner(db_session, settings=settings, clock=FrozenClock(_PINNED))
    old_signature = before.sign(b"payload", purpose=_PURPOSE)

    rotate_hmac_key(
        db_session,
        _PURPOSE,
        b"n" * HMAC_KEY_BYTES,
        purge_after=_PINNED + timedelta(hours=72),
        settings=settings,
        clock=FrozenClock(_PINNED + timedelta(seconds=1)),
    )

    after = HmacSigner(
        db_session,
        settings=settings,
        clock=FrozenClock(_PINNED + timedelta(hours=1)),
    )
    assert after.verify(b"payload", old_signature, purpose=_PURPOSE)

    new_signature = after.sign(b"payload", purpose=_PURPOSE)
    assert new_signature != old_signature
    assert after.verify(b"payload", new_signature, purpose=_PURPOSE)


def test_second_rotation_preserves_row_backed_primary_until_purge_after(
    db_session: Session, settings: Settings
) -> None:
    first = HmacSigner(db_session, settings=settings, clock=FrozenClock(_PINNED))
    fallback_signature = first.sign(b"payload", purpose=_PURPOSE)
    first_primary = b"1" * HMAC_KEY_BYTES
    second_primary = b"2" * HMAC_KEY_BYTES

    rotate_hmac_key(
        db_session,
        _PURPOSE,
        first_primary,
        purge_after=_PINNED + timedelta(hours=72),
        settings=settings,
        clock=FrozenClock(_PINNED + timedelta(seconds=1)),
    )
    middle = HmacSigner(
        db_session,
        settings=settings,
        clock=FrozenClock(_PINNED + timedelta(hours=1)),
    )
    row_backed_signature = middle.sign(b"payload", purpose=_PURPOSE)

    rotate_hmac_key(
        db_session,
        _PURPOSE,
        second_primary,
        purge_after=_PINNED + timedelta(hours=73),
        settings=settings,
        clock=FrozenClock(_PINNED + timedelta(hours=2)),
    )

    after = HmacSigner(
        db_session,
        settings=settings,
        clock=FrozenClock(_PINNED + timedelta(hours=3)),
    )
    assert after.current_key(purpose=_PURPOSE) == second_primary
    assert after.verify(b"payload", fallback_signature, purpose=_PURPOSE)
    assert after.verify(b"payload", row_backed_signature, purpose=_PURPOSE)


def test_expired_legacy_slot_is_not_accepted(
    db_session: Session, settings: Settings
) -> None:
    before = HmacSigner(db_session, settings=settings, clock=FrozenClock(_PINNED))
    old_signature = before.sign(b"payload", purpose=_PURPOSE)

    rotate_hmac_key(
        db_session,
        _PURPOSE,
        b"n" * HMAC_KEY_BYTES,
        purge_after=_PINNED + timedelta(seconds=5),
        settings=settings,
        clock=FrozenClock(_PINNED + timedelta(seconds=1)),
    )

    after_purge = HmacSigner(
        db_session,
        settings=settings,
        clock=FrozenClock(_PINNED + timedelta(seconds=6)),
    )
    assert not after_purge.verify(b"payload", old_signature, purpose=_PURPOSE)


def test_itsdangerous_key_list_verifies_old_and_signs_newest(
    db_session: Session, settings: Settings
) -> None:
    fallback_key = derive_subkey(settings.root_key, purpose=_PURPOSE)
    old_serializer = URLSafeTimedSerializer(
        secret_key=fallback_key,
        salt="guest-link-v1",
    )
    old_token = old_serializer.dumps({"version": "old"})

    primary_key = b"n" * HMAC_KEY_BYTES
    rotate_hmac_key(
        db_session,
        _PURPOSE,
        primary_key,
        purge_after=_PINNED + timedelta(hours=72),
        settings=settings,
        clock=FrozenClock(_PINNED + timedelta(seconds=1)),
    )

    keys = HmacSigner(
        db_session,
        settings=settings,
        clock=FrozenClock(_PINNED + timedelta(hours=1)),
    ).verification_keys(purpose=_PURPOSE)
    keyring_serializer = URLSafeTimedSerializer(
        secret_key=keys,
        salt="guest-link-v1",
    )
    assert keyring_serializer.loads(old_token) == {"version": "old"}

    new_token = keyring_serializer.dumps({"version": "new"})
    primary_serializer = URLSafeTimedSerializer(
        secret_key=primary_key,
        salt="guest-link-v1",
    )
    assert primary_serializer.loads(new_token) == {"version": "new"}
    with pytest.raises(BadSignature):
        old_serializer.loads(new_token)


def test_verify_rejects_malformed_signature(
    db_session: Session, settings: Settings
) -> None:
    signer = HmacSigner(db_session, settings=settings, clock=FrozenClock(_PINNED))

    assert not signer.verify(b"payload", "z" * 64, purpose=_PURPOSE)
    assert not signer.verify(b"payload", "f" * 63, purpose=_PURPOSE)


def test_session_factory_scope_is_closed(engine: Engine, settings: Settings) -> None:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    events: list[str] = []

    @contextmanager
    def session_scope() -> Iterator[Session]:
        events.append("enter")
        with factory() as session:
            try:
                yield session
            finally:
                events.append("exit")

    signer = HmacSigner(
        session_factory=session_scope,
        settings=settings,
        clock=FrozenClock(_PINNED),
    )
    signature = signer.sign(b"payload", purpose=_PURPOSE)

    assert signer.verify(b"payload", signature, purpose=_PURPOSE)
    assert events == ["enter", "exit", "enter", "exit"]


def test_rotate_requires_exact_width_key(
    db_session: Session, settings: Settings
) -> None:
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        rotate_hmac_key(
            db_session,
            _PURPOSE,
            b"short",
            purge_after=_PINNED + timedelta(hours=72),
            settings=settings,
            clock=FrozenClock(_PINNED),
        )
