"""Tests for the session signing-key source seam."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.base import Base
from app.admin.rotate_session_secret import rotate_session_signing_key
from app.auth.keys import KeyDerivationError, derive_subkey
from app.auth.session_signing_key import SessionSigningKeySource
from app.config import Settings
from app.util.clock import FrozenClock

_PINNED = datetime(2026, 4, 29, 10, 0, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-session-signing-root-key"),
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


def test_falls_back_to_root_key_subkey(db_session: Session, settings: Settings) -> None:
    key = SessionSigningKeySource(db_session, settings=settings).current()

    assert key == derive_subkey(settings.root_key, purpose="session-cookie")
    assert len(key) == 32


def test_reads_latest_session_signing_key_envelope(
    db_session: Session, settings: Settings
) -> None:
    first_key = b"a" * 32
    second_key = b"b" * 32
    rotate_session_signing_key(
        db_session,
        first_key,
        settings=settings,
        clock=FrozenClock(_PINNED),
    )
    rotate_session_signing_key(
        db_session,
        second_key,
        settings=settings,
        clock=FrozenClock(_PINNED + timedelta(seconds=1)),
    )

    assert (
        SessionSigningKeySource(db_session, settings=settings).current() == second_key
    )


def test_missing_root_key_fallback_fails_cleanly(db_session: Session) -> None:
    settings = Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=None,
    )

    with pytest.raises(KeyDerivationError, match=r"settings\.root_key is not set"):
        SessionSigningKeySource(db_session, settings=settings).current()


def test_missing_root_key_row_backed_fails_cleanly(
    db_session: Session, settings: Settings
) -> None:
    rotate_session_signing_key(
        db_session,
        b"r" * 32,
        settings=settings,
        clock=FrozenClock(_PINNED),
    )
    settings_without_root = Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=None,
    )

    with pytest.raises(
        KeyDerivationError,
        match="cannot read the row-backed session signing key",
    ):
        SessionSigningKeySource(db_session, settings=settings_without_root).current()
