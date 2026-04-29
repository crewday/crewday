"""Admin seam for rotating deployment-wide session signing material."""

from __future__ import annotations

from typing import Final

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.secrets.repositories import SqlAlchemySecretEnvelopeRepository
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.adapters.storage.ports import EnvelopeOwner
from app.auth.keys import KeyDerivationError
from app.auth.session_signing_key import (
    SESSION_SIGNING_KEY_OWNER_ID,
    SESSION_SIGNING_KEY_OWNER_KIND,
    SESSION_SIGNING_KEY_PURPOSE,
)
from app.config import Settings, get_settings
from app.tenancy import tenant_agnostic
from app.util.clock import Clock, SystemClock

__all__ = [
    "SESSION_SIGNING_KEY_BYTES",
    "clear_all_sessions",
    "rotate_session_signing_key",
]

SESSION_SIGNING_KEY_BYTES: Final[int] = 32


def clear_all_sessions(session: Session) -> None:
    """Delete every web session row and flush inside the caller's transaction."""
    with tenant_agnostic():
        session.execute(delete(SessionRow))
        session.flush()


def rotate_session_signing_key(
    session: Session,
    new_key: bytes,
    *,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> None:
    """Write a new active signing-key envelope and clear sessions atomically.

    The caller owns commit / rollback. A later CLI can wrap this helper
    in its own transaction and audit handling.
    """
    if not isinstance(new_key, bytes):
        raise TypeError("new_key must be bytes")
    if len(new_key) != SESSION_SIGNING_KEY_BYTES:
        raise ValueError("new_key must be exactly 32 bytes")

    resolved_settings = settings if settings is not None else get_settings()
    root_key = resolved_settings.root_key
    if root_key is None:
        raise KeyDerivationError(
            "settings.root_key is not set; cannot rotate the session signing key. "
            "Set CREWDAY_ROOT_KEY to a long random value."
        )
    resolved_clock = clock if clock is not None else SystemClock()
    envelope = Aes256GcmEnvelope(
        root_key,
        repository=SqlAlchemySecretEnvelopeRepository(session),
        clock=resolved_clock,
    )
    envelope.encrypt(
        new_key,
        purpose=SESSION_SIGNING_KEY_PURPOSE,
        owner=EnvelopeOwner(
            kind=SESSION_SIGNING_KEY_OWNER_KIND,
            id=SESSION_SIGNING_KEY_OWNER_ID,
        ),
    )
    clear_all_sessions(session)
