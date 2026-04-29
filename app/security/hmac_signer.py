"""Deployment-wide HMAC signing keys.

This module owns the row-backed signing-key slots for deployment-wide
HMAC surfaces such as guest welcome links and signed file URLs. It
intentionally does not cover outbound webhooks: webhook subscriptions
already own per-subscription ``secret_envelope`` rows and rotate on a
different boundary.

Slot convention in ``secret_envelope``:

* ``owner_entity_kind = "deployment_hmac_key"``
* ``owner_entity_id = <logical purpose>`` such as ``"guest-link"``
  or ``"storage-sign"``
* ``purpose = "hmac.primary"`` for primary rows
* ``purpose = "hmac.legacy.<slot_id>"`` for legacy rows
* legacy ``rotated_at`` stores ``purge_after``

The newest primary row signs. Verification accepts the newest primary
plus legacy rows whose ``purge_after`` is still in the future. If a
deployment has not installed rows yet, the signer falls back to the
legacy root-key-derived subkey for that purpose so existing tokens and
URLs remain behavior-compatible.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterator
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from typing import Final, Protocol

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.secrets.models import SecretEnvelope
from app.adapters.db.secrets.repositories import SqlAlchemySecretEnvelopeRepository
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.adapters.storage.ports import EnvelopeOwner
from app.auth.keys import KeyDerivationError, derive_subkey
from app.config import Settings, get_settings
from app.tenancy import tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "HMAC_KEY_BYTES",
    "HMAC_KEY_OWNER_KIND",
    "HMAC_PRIMARY_PURPOSE",
    "HmacSigner",
    "rotate_hmac_key",
]

HMAC_KEY_BYTES: Final[int] = 32
HMAC_KEY_OWNER_KIND: Final[str] = "deployment_hmac_key"
HMAC_PRIMARY_PURPOSE: Final[str] = "hmac.primary"
_LEGACY_PURPOSE_PREFIX: Final[str] = "hmac.legacy."
_ROW_BACKED_ENVELOPE_VERSION: Final[int] = 0x02
_SIGNATURE_HEX_LEN: Final[int] = 64
_PURPOSE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


class _SessionFactory(Protocol):
    def __call__(self) -> AbstractContextManager[Session]:
        """Return a session context manager owned by the caller."""


def _validate_purpose(purpose: str) -> str:
    if not _PURPOSE_RE.fullmatch(purpose):
        raise ValueError(
            "purpose must be a non-empty lowercase slug containing only "
            "letters, digits, '.', '_' or '-'"
        )
    return purpose


def _validate_key(key: bytes, *, label: str) -> bytes:
    if not isinstance(key, bytes):
        raise TypeError(f"{label} must be bytes")
    if len(key) != HMAC_KEY_BYTES:
        raise ValueError(f"{label} must be exactly {HMAC_KEY_BYTES} bytes")
    return key


def _require_root_key(settings: Settings, *, action: str) -> SecretStr:
    root_key = settings.root_key
    if root_key is None:
        raise KeyDerivationError(
            f"settings.root_key is not set; cannot {action}. "
            "Set CREWDAY_ROOT_KEY to a long random value."
        )
    return root_key


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _pointer_for(row: SecretEnvelope) -> bytes:
    return bytes((_ROW_BACKED_ENVELOPE_VERSION,)) + row.id.encode("utf-8")


def _legacy_purpose() -> str:
    return f"{_LEGACY_PURPOSE_PREFIX}{new_ulid()}"


def _primary_row(session: Session, *, purpose: str) -> SecretEnvelope | None:
    with tenant_agnostic():
        return session.scalars(
            select(SecretEnvelope)
            .where(
                SecretEnvelope.owner_entity_kind == HMAC_KEY_OWNER_KIND,
                SecretEnvelope.owner_entity_id == purpose,
                SecretEnvelope.purpose == HMAC_PRIMARY_PURPOSE,
            )
            .order_by(SecretEnvelope.created_at.desc(), SecretEnvelope.id.desc())
            .limit(1)
        ).first()


def _legacy_rows(
    session: Session, *, purpose: str, now: datetime
) -> Iterator[SecretEnvelope]:
    with tenant_agnostic():
        rows = session.scalars(
            select(SecretEnvelope)
            .where(
                SecretEnvelope.owner_entity_kind == HMAC_KEY_OWNER_KIND,
                SecretEnvelope.owner_entity_id == purpose,
                SecretEnvelope.purpose.like(f"{_LEGACY_PURPOSE_PREFIX}%"),
            )
            .order_by(SecretEnvelope.created_at.asc(), SecretEnvelope.id.asc())
        ).all()
    for row in rows:
        purge_after = row.rotated_at
        if purge_after is None:
            continue
        if _aware_utc(purge_after) > now:
            yield row


def _decrypt_row(
    envelope: Aes256GcmEnvelope, row: SecretEnvelope, *, label: str
) -> bytes:
    return _validate_key(
        envelope.decrypt(_pointer_for(row), purpose=row.purpose),
        label=label,
    )


class HmacSigner:
    """Sign and verify deployment-wide HMAC-SHA256 payloads."""

    def __init__(
        self,
        session: Session | None = None,
        *,
        session_factory: _SessionFactory | None = None,
        settings: Settings | None = None,
        clock: Clock | None = None,
    ) -> None:
        if session is None and session_factory is None:
            raise ValueError("session or session_factory is required")
        if session is not None and session_factory is not None:
            raise ValueError("pass either session or session_factory, not both")
        self._session = session
        self._session_factory = session_factory
        self._settings = settings
        self._clock = clock if clock is not None else SystemClock()

    def sign(self, message: bytes, *, purpose: str) -> str:
        """Return the hex HMAC-SHA256 signature for ``message``."""
        if not isinstance(message, bytes):
            raise TypeError("message must be bytes")
        key = self.current_key(purpose=purpose)
        return hmac.new(key, message, hashlib.sha256).hexdigest()

    def verify(self, message: bytes, signature: str, *, purpose: str) -> bool:
        """Return ``True`` if ``signature`` matches any live key slot."""
        if not isinstance(message, bytes):
            raise TypeError("message must be bytes")
        if len(signature) != _SIGNATURE_HEX_LEN:
            return False
        try:
            bytes.fromhex(signature)
        except ValueError:
            return False

        for key in self.verification_keys(purpose=purpose):
            expected = hmac.new(key, message, hashlib.sha256).hexdigest()
            if hmac.compare_digest(expected, signature):
                return True
        return False

    def current_key(self, *, purpose: str) -> bytes:
        """Return the current primary key, or the root-key fallback."""
        keys = self.verification_keys(purpose=purpose)
        return keys[-1]

    def verification_keys(self, *, purpose: str) -> tuple[bytes, ...]:
        """Return legacy verification keys followed by the signing key.

        The order matches ``itsdangerous`` key rotation semantics:
        oldest-to-newest for verification, newest last for signing.
        """
        logical_purpose = _validate_purpose(purpose)
        settings = self._settings if self._settings is not None else get_settings()
        now = self._clock.now().astimezone(UTC)
        with self._session_scope() as session:
            primary = _primary_row(session, purpose=logical_purpose)
            if primary is None:
                fallback = derive_subkey(settings.root_key, purpose=logical_purpose)
                return (_validate_key(fallback, label="fallback key"),)

            envelope = Aes256GcmEnvelope(
                _require_root_key(settings, action="read row-backed HMAC keys"),
                repository=SqlAlchemySecretEnvelopeRepository(session),
            )
            keys = [
                _decrypt_row(envelope, row, label="legacy key")
                for row in _legacy_rows(session, purpose=logical_purpose, now=now)
            ]
            keys.append(_decrypt_row(envelope, primary, label="primary key"))
            return tuple(keys)

    def _session_scope(self) -> AbstractContextManager[Session]:
        if self._session_factory is not None:
            return self._session_factory()
        if self._session is None:  # pragma: no cover - guarded by __init__
            raise RuntimeError("HmacSigner has no session source")
        return nullcontext(self._session)


def rotate_hmac_key(
    session: Session,
    purpose: str,
    new_key: bytes,
    *,
    purge_after: datetime,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> None:
    """Install a new primary key and preserve the old primary as legacy.

    The caller owns commit / rollback. When no row-backed primary
    exists, the old key is the root-key-derived fallback for the same
    purpose, which preserves pre-rotation guest links and signed file
    URLs for the configured overlap window.
    """
    logical_purpose = _validate_purpose(purpose)
    key = _validate_key(new_key, label="new_key")
    if purge_after.tzinfo is None or purge_after.tzinfo.utcoffset(purge_after) is None:
        raise ValueError("purge_after must be an aware datetime")

    resolved_settings = settings if settings is not None else get_settings()
    root_key = _require_root_key(resolved_settings, action="rotate HMAC signing keys")
    resolved_clock = clock if clock is not None else SystemClock()
    envelope = Aes256GcmEnvelope(
        root_key,
        repository=SqlAlchemySecretEnvelopeRepository(session),
        clock=resolved_clock,
    )

    old_primary = _primary_row(session, purpose=logical_purpose)
    if old_primary is None:
        old_key = derive_subkey(root_key, purpose=logical_purpose)
    else:
        old_key = _decrypt_row(envelope, old_primary, label="old primary key")

    legacy_pointer = envelope.encrypt(
        _validate_key(old_key, label="old primary key"),
        purpose=_legacy_purpose(),
        owner=EnvelopeOwner(kind=HMAC_KEY_OWNER_KIND, id=logical_purpose),
    )
    legacy_id = legacy_pointer[1:].decode("utf-8")
    with tenant_agnostic():
        legacy_row = session.get(SecretEnvelope, legacy_id)
    if legacy_row is None:  # pragma: no cover - inserted by encrypt above
        raise RuntimeError("newly inserted legacy HMAC key row was not readable")
    legacy_row.rotated_at = purge_after.astimezone(UTC)

    envelope.encrypt(
        key,
        purpose=HMAC_PRIMARY_PURPOSE,
        owner=EnvelopeOwner(kind=HMAC_KEY_OWNER_KIND, id=logical_purpose),
    )
    session.flush()
