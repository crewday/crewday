"""Integration tests for the session signing-key rotation seam."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.secrets.models import SecretEnvelope
from app.admin.rotate_session_secret import (
    clear_all_sessions,
    rotate_session_signing_key,
)
from app.auth.keys import KeyDerivationError
from app.auth.session_signing_key import (
    SESSION_SIGNING_KEY_OWNER_ID,
    SESSION_SIGNING_KEY_OWNER_KIND,
    SESSION_SIGNING_KEY_PURPOSE,
    SessionSigningKeySource,
)
from app.config import Settings
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user

_PINNED = datetime(2026, 4, 29, 10, 0, 0, tzinfo=UTC)


@pytest.fixture
def settings(db_url: str) -> Settings:
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-session-signing-root-key"),
    )


def _session_row(session_id: str, user_id: str) -> SessionRow:
    return SessionRow(
        id=session_id,
        user_id=user_id,
        workspace_id=None,
        expires_at=_PINNED,
        absolute_expires_at=_PINNED,
        last_seen_at=_PINNED,
        ua_hash=None,
        ip_hash=None,
        fingerprint_hash=None,
        created_at=_PINNED,
        invalidated_at=None,
        invalidation_cause=None,
    )


def _session_key_rows(db_session: Session) -> list[SecretEnvelope]:
    return list(
        db_session.scalars(
            select(SecretEnvelope)
            .where(
                SecretEnvelope.owner_entity_kind == SESSION_SIGNING_KEY_OWNER_KIND,
                SecretEnvelope.owner_entity_id == SESSION_SIGNING_KEY_OWNER_ID,
                SecretEnvelope.purpose == SESSION_SIGNING_KEY_PURPOSE,
            )
            .order_by(SecretEnvelope.created_at.desc(), SecretEnvelope.id.desc())
        )
    )


def test_clear_all_sessions_deletes_and_flushes(db_session: Session) -> None:
    user = bootstrap_user(db_session, email="clear@example.com", display_name="Clear")
    db_session.add(_session_row("sess-clear", user.id))
    db_session.flush()

    clear_all_sessions(db_session)

    assert db_session.get(SessionRow, "sess-clear") is None


def test_rotate_writes_active_row_and_clears_sessions(
    db_session: Session, settings: Settings
) -> None:
    user = bootstrap_user(db_session, email="rotate@example.com", display_name="Rotate")
    db_session.add(_session_row("sess-rotate", user.id))
    db_session.flush()

    new_key = b"s" * 32
    rotate_session_signing_key(
        db_session,
        new_key,
        settings=settings,
        clock=FrozenClock(_PINNED),
    )

    rows = _session_key_rows(db_session)
    assert len(rows) == 1
    assert bytes(rows[0].ciphertext) != new_key
    assert db_session.get(SessionRow, "sess-rotate") is None
    assert SessionSigningKeySource(db_session, settings=settings).current() == new_key


def test_rotate_does_not_commit(db_session: Session, settings: Settings) -> None:
    committed = False

    @event.listens_for(db_session, "before_commit")
    def _record_commit(_session: Session) -> None:
        nonlocal committed
        committed = True

    try:
        rotate_session_signing_key(
            db_session,
            b"n" * 32,
            settings=settings,
            clock=FrozenClock(_PINNED),
        )
    finally:
        event.remove(db_session, "before_commit", _record_commit)

    assert committed is False
    assert len(_session_key_rows(db_session)) == 1


def test_rotate_rejects_empty_or_wrong_width_key(
    db_session: Session, settings: Settings
) -> None:
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        rotate_session_signing_key(db_session, b"", settings=settings)
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        rotate_session_signing_key(db_session, b"x" * 31, settings=settings)


def test_rotate_requires_root_key(db_session: Session) -> None:
    settings = Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=None,
    )

    with pytest.raises(KeyDerivationError, match="cannot rotate"):
        rotate_session_signing_key(db_session, b"x" * 32, settings=settings)


def test_delete_failure_rolls_back_envelope(
    db_session: Session,
    engine: Engine,
    settings: Settings,
) -> None:
    original_key = b"o" * 32
    rotate_session_signing_key(
        db_session,
        original_key,
        settings=settings,
        clock=FrozenClock(_PINNED),
    )
    db_session.commit()

    @event.listens_for(engine, "before_cursor_execute")
    def _fail_session_delete(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalised = statement.casefold().lstrip()
        if normalised.startswith(("delete from session", 'delete from "session"')):
            raise RuntimeError("delete failed")

    try:
        with pytest.raises(RuntimeError, match="delete failed"):
            rotate_session_signing_key(
                db_session,
                b"z" * 32,
                settings=settings,
                clock=FrozenClock(_PINNED),
            )
        db_session.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", _fail_session_delete)

    rows = _session_key_rows(db_session)
    assert len(rows) == 1
    assert (
        SessionSigningKeySource(db_session, settings=settings).current() == original_key
    )
