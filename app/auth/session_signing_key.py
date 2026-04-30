"""Session signing-key source.

The web session cookie is currently an opaque random token backed by
the ``session`` table. This adapter provides the independent
deployment-wide signing material source that the rotation CLI will use
without changing the cookie format.
"""

from __future__ import annotations

from typing import Final

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.secrets.models import SecretEnvelope
from app.adapters.db.secrets.repositories import SqlAlchemySecretEnvelopeRepository
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.auth.keys import KeyDerivationError, derive_subkey
from app.config import Settings, get_settings
from app.tenancy import tenant_agnostic

__all__ = [
    "SESSION_SIGNING_KEY_OWNER_ID",
    "SESSION_SIGNING_KEY_OWNER_KIND",
    "SESSION_SIGNING_KEY_PURPOSE",
    "SessionSigningKeySource",
]

SESSION_SIGNING_KEY_OWNER_KIND: Final[str] = "deployment"
SESSION_SIGNING_KEY_OWNER_ID: Final[str] = "session.signing_key"
SESSION_SIGNING_KEY_PURPOSE: Final[str] = "session.signing_key"
_FALLBACK_PURPOSE: Final[str] = "session-cookie"
_ROW_BACKED_ENVELOPE_VERSION: Final[int] = 0x02


def _require_root_key(settings: Settings) -> SecretStr:
    root_key = settings.root_key
    if root_key is None:
        raise KeyDerivationError(
            "settings.root_key is not set; cannot read the row-backed session "
            "signing key. Set CREWDAY_ROOT_KEY to a long random value."
        )
    return root_key


class SessionSigningKeySource:
    """Resolve the active deployment-wide session signing key."""

    def __init__(
        self,
        session: Session,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._settings = settings

    def current(self) -> bytes:
        """Return active row-backed signing material or the root-key fallback."""
        settings = self._settings if self._settings is not None else get_settings()
        with tenant_agnostic():
            row = self._session.scalars(
                select(SecretEnvelope)
                .where(
                    SecretEnvelope.owner_entity_kind == SESSION_SIGNING_KEY_OWNER_KIND,
                    SecretEnvelope.owner_entity_id == SESSION_SIGNING_KEY_OWNER_ID,
                    SecretEnvelope.purpose == SESSION_SIGNING_KEY_PURPOSE,
                )
                .order_by(SecretEnvelope.created_at.desc(), SecretEnvelope.id.desc())
                .limit(1)
            ).first()

        if row is None:
            return derive_subkey(settings.root_key, purpose=_FALLBACK_PURPOSE)

        envelope = Aes256GcmEnvelope(
            _require_root_key(settings),
            repository=SqlAlchemySecretEnvelopeRepository(self._session),
        )
        pointer = bytes((_ROW_BACKED_ENVELOPE_VERSION,)) + row.id.encode("utf-8")
        return envelope.decrypt(pointer, purpose=SESSION_SIGNING_KEY_PURPOSE)
