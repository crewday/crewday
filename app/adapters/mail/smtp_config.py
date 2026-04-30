"""Runtime SMTP configuration resolved from deployment settings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, cast

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.db.capabilities.models import DeploymentSetting
from app.adapters.db.ports import UnitOfWork
from app.adapters.db.secrets.repositories import SqlAlchemySecretEnvelopeRepository
from app.adapters.db.session import make_uow
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.adapters.storage.ports import EnvelopeDecryptError
from app.tenancy import tenant_agnostic

__all__ = [
    "SMTP_BOUNCE_DOMAIN_SETTING",
    "SMTP_FROM_SETTING",
    "SMTP_HOST_SETTING",
    "SMTP_PASSWORD_DISPLAY_STUB",
    "SMTP_PASSWORD_PURPOSE",
    "SMTP_PASSWORD_SETTING",
    "SMTP_PORT_SETTING",
    "SMTP_SETTING_KEYS",
    "SMTP_TIMEOUT_SETTING",
    "SMTP_USER_SETTING",
    "SMTP_USE_TLS_SETTING",
    "DeploymentSmtpConfigSource",
    "SmtpConfig",
    "SmtpConfigError",
    "SmtpConfigSource",
    "StaticSmtpConfigSource",
    "smtp_envelope_id_from_pointer",
    "smtp_envelope_pointer",
    "smtp_password_display_stub",
]

SMTP_HOST_SETTING: Final[str] = "smtp.host"
SMTP_PORT_SETTING: Final[str] = "smtp.port"
SMTP_USER_SETTING: Final[str] = "smtp.user"
SMTP_PASSWORD_SETTING: Final[str] = "smtp.password_envelope_id"
SMTP_FROM_SETTING: Final[str] = "smtp.from"
SMTP_USE_TLS_SETTING: Final[str] = "smtp.use_tls"
SMTP_BOUNCE_DOMAIN_SETTING: Final[str] = "smtp.bounce_domain"
SMTP_TIMEOUT_SETTING: Final[str] = "smtp.timeout"
SMTP_SETTING_KEYS: Final[tuple[str, ...]] = (
    SMTP_HOST_SETTING,
    SMTP_PORT_SETTING,
    SMTP_USER_SETTING,
    SMTP_PASSWORD_SETTING,
    SMTP_FROM_SETTING,
    SMTP_USE_TLS_SETTING,
    SMTP_BOUNCE_DOMAIN_SETTING,
    SMTP_TIMEOUT_SETTING,
)

SMTP_PASSWORD_PURPOSE: Final[str] = "smtp.password"
SMTP_PASSWORD_DISPLAY_STUB: Final[str] = "**********"
_SMTP_ENVELOPE_ROW_VERSION: Final[int] = 0x02


class SmtpConfigError(RuntimeError):
    """Raised when persisted SMTP configuration is malformed or undecryptable."""


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    """Resolved SMTP settings for one send attempt."""

    host: str | None
    port: int
    from_addr: str | None
    user: str | None
    password: SecretStr | None
    use_tls: bool
    timeout: int
    bounce_domain: str | None


class SmtpConfigSource(Protocol):
    """Resolve SMTP settings at send time."""

    def config(self) -> SmtpConfig:
        """Return the active SMTP configuration."""
        ...


class StaticSmtpConfigSource:
    """SMTP source for constructor-supplied config and tests."""

    __slots__ = ("_config",)

    def __init__(self, config: SmtpConfig) -> None:
        self._config = config

    def config(self) -> SmtpConfig:
        return self._config


class DeploymentSmtpConfigSource:
    """Resolve SMTP settings from deployment_setting rows, then env fallback."""

    __slots__ = ("_env", "_root_key", "_uow_factory")

    def __init__(
        self,
        *,
        env: SmtpConfig,
        root_key: SecretStr | None,
        uow_factory: Callable[[], UnitOfWork] = make_uow,
    ) -> None:
        self._env = env
        self._root_key = root_key
        self._uow_factory = uow_factory

    def config(self) -> SmtpConfig:
        try:
            with self._uow_factory() as raw_session:
                session = cast(Session, raw_session)
                with tenant_agnostic():
                    rows = {
                        row.key: row.value
                        for row in session.scalars(
                            select(DeploymentSetting).where(
                                DeploymentSetting.key.in_(SMTP_SETTING_KEYS)
                            )
                        ).all()
                    }
                password = self._password(rows, session)
        except SQLAlchemyError as exc:
            raise SmtpConfigError("smtp settings could not be loaded") from exc
        return SmtpConfig(
            host=_db_or_env_optional_str(rows, SMTP_HOST_SETTING, self._env.host),
            port=_db_or_env_port(rows, SMTP_PORT_SETTING, self._env.port),
            user=_db_or_env_optional_str(rows, SMTP_USER_SETTING, self._env.user),
            password=password,
            from_addr=_db_or_env_optional_str(
                rows, SMTP_FROM_SETTING, self._env.from_addr
            ),
            use_tls=_db_or_env_bool(rows, SMTP_USE_TLS_SETTING, self._env.use_tls),
            timeout=_db_or_env_positive_int(
                rows, SMTP_TIMEOUT_SETTING, self._env.timeout
            ),
            bounce_domain=_db_or_env_optional_str(
                rows, SMTP_BOUNCE_DOMAIN_SETTING, self._env.bounce_domain
            ),
        )

    def _password(self, rows: dict[str, object], session: Session) -> SecretStr | None:
        if SMTP_PASSWORD_SETTING not in rows:
            return self._env.password
        envelope_id = rows[SMTP_PASSWORD_SETTING]
        if not isinstance(envelope_id, str) or not envelope_id.strip():
            raise SmtpConfigError(
                "smtp password setting is malformed; expected envelope id"
            )
        root_key = self._root_key
        if root_key is None:
            raise SmtpConfigError("smtp password setting requires CREWDAY_ROOT_KEY")
        envelope = Aes256GcmEnvelope(
            root_key,
            repository=SqlAlchemySecretEnvelopeRepository(session),
        )
        try:
            plaintext = envelope.decrypt(
                smtp_envelope_pointer(envelope_id), purpose=SMTP_PASSWORD_PURPOSE
            )
        except EnvelopeDecryptError as exc:
            raise SmtpConfigError(
                "smtp password setting could not be decrypted"
            ) from exc
        try:
            decoded = plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SmtpConfigError("smtp password is not valid UTF-8") from exc
        if not decoded.strip():
            raise SmtpConfigError("smtp password is blank")
        return SecretStr(decoded)


def smtp_envelope_pointer(envelope_id: str) -> bytes:
    """Return the row-backed envelope pointer for ``envelope_id``."""
    if not envelope_id or not envelope_id.strip():
        raise ValueError("smtp envelope id must be non-blank")
    return bytes((_SMTP_ENVELOPE_ROW_VERSION,)) + envelope_id.encode("utf-8")


def smtp_envelope_id_from_pointer(pointer: bytes) -> str:
    """Extract the row id from a row-backed envelope pointer."""
    if len(pointer) < 2 or pointer[0] != _SMTP_ENVELOPE_ROW_VERSION:
        raise ValueError("smtp envelope pointer is not row-backed")
    try:
        envelope_id = pointer[1:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("smtp envelope pointer id is not UTF-8") from exc
    if not envelope_id.strip():
        raise ValueError("smtp envelope pointer id is blank")
    return envelope_id


def smtp_password_display_stub() -> str:
    """Public-safe marker for a configured SMTP password."""
    return SMTP_PASSWORD_DISPLAY_STUB


def _db_or_env_optional_str(
    rows: dict[str, object], key: str, env_value: str | None
) -> str | None:
    if key not in rows:
        return env_value
    value = rows[key]
    if isinstance(value, str) and value.strip():
        return value
    raise SmtpConfigError(f"{key} setting is malformed; expected non-blank string")


def _db_or_env_int(rows: dict[str, object], key: str, env_value: int) -> int:
    if key not in rows:
        return env_value
    value = rows[key]
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise SmtpConfigError(f"{key} setting is malformed; expected non-negative integer")


def _db_or_env_port(rows: dict[str, object], key: str, env_value: int) -> int:
    value = _db_or_env_int(rows, key, env_value)
    if 1 <= value <= 65535:
        return value
    raise SmtpConfigError(f"{key} setting is malformed; expected TCP port 1-65535")


def _db_or_env_positive_int(rows: dict[str, object], key: str, env_value: int) -> int:
    value = _db_or_env_int(rows, key, env_value)
    if value > 0:
        return value
    raise SmtpConfigError(f"{key} setting is malformed; expected positive integer")


def _db_or_env_bool(rows: dict[str, object], key: str, env_value: bool) -> bool:
    if key not in rows:
        return env_value
    value = rows[key]
    if isinstance(value, bool):
        return value
    raise SmtpConfigError(f"{key} setting is malformed; expected boolean")
