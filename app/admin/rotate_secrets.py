"""Host-only rotation helpers for deployment secrets."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import smtplib
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.capabilities.models import DeploymentSetting
from app.adapters.db.secrets.repositories import SqlAlchemySecretEnvelopeRepository
from app.adapters.llm.openrouter import (
    OPENROUTER_API_KEY_PURPOSE,
    OPENROUTER_API_KEY_SETTING,
    openrouter_api_key_display_stub,
    openrouter_envelope_id_from_pointer,
)
from app.adapters.mail.smtp_config import (
    SMTP_BOUNCE_DOMAIN_SETTING,
    SMTP_FROM_SETTING,
    SMTP_HOST_SETTING,
    SMTP_PASSWORD_PURPOSE,
    SMTP_PASSWORD_SETTING,
    SMTP_PORT_SETTING,
    SMTP_TIMEOUT_SETTING,
    SMTP_USE_TLS_SETTING,
    SMTP_USER_SETTING,
    smtp_envelope_id_from_pointer,
    smtp_password_display_stub,
)
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.adapters.storage.ports import EnvelopeOwner
from app.admin.rotate_root_key import SYSTEM_ACTOR_ID
from app.admin.rotate_session_secret import rotate_session_signing_key
from app.audit import write_deployment_audit
from app.config import Settings
from app.security.hmac_signer import HMAC_KEY_BYTES, rotate_hmac_key
from app.tenancy import tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "DEFAULT_HMAC_PURPOSES",
    "HMAC_ROTATION_WINDOW",
    "OpenRouterProbe",
    "SecretRotationError",
    "SecretRotationResult",
    "SmtpProbe",
    "SmtpRotationCredentials",
    "load_secret_file",
    "parse_smtp_credentials",
    "rotate_hmac_signing_key",
    "rotate_openrouter_key",
    "rotate_session_secret",
    "rotate_smtp_credentials",
    "rotation_result_payload",
    "secret_bytes_from_input",
    "zero_key_material",
]

DEFAULT_HMAC_PURPOSES: Final[tuple[str, ...]] = ("guest-link", "storage-sign")
HMAC_ROTATION_WINDOW: Final[timedelta] = timedelta(hours=72)
_DEFAULT_OPENROUTER_BASE_URL: Final[str] = "https://openrouter.ai/api/v1"
_IMPLICIT_TLS_PORT: Final[int] = 465
_PLAIN_PORT: Final[int] = 25


class SecretRotationError(RuntimeError):
    """Operator-facing secret-rotation failure."""


class OpenRouterProbe(Protocol):
    def __call__(self, api_key: str, *, base_url: str) -> None:
        """Validate an OpenRouter API key before persistence."""


@dataclass(frozen=True, slots=True)
class SecretRotationResult:
    action: str
    rotated: tuple[str, ...]
    legacy_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class SmtpRotationCredentials:
    host: str
    port: int
    from_addr: str
    password: bytearray
    user: str | None = None
    use_tls: bool = True
    timeout: int = 10
    bounce_domain: str | None = None


SmtpProbe = Callable[[SmtpRotationCredentials], None]


def load_secret_file(path: Path) -> bytearray:
    """Read a secret from a 0600 file after root-key-style validation."""
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise SecretRotationError("secret file must point to a regular file")
    if info.st_uid != os.getuid():
        raise SecretRotationError("secret file must be owned by the current user")
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o600:
        raise SecretRotationError("secret file must have mode 0600")
    return secret_bytes_from_input(path.read_bytes())


def secret_bytes_from_input(raw: bytes, *, exact_len: int | None = None) -> bytearray:
    value = bytearray(raw.strip())
    if not value:
        raise SecretRotationError("secret input must not be empty")
    if exact_len is not None and len(value) == exact_len:
        return value
    try:
        bytes(value).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretRotationError("secret input must be UTF-8 text") from exc
    if exact_len is None:
        return value
    try:
        decoded = base64.b64decode(bytes(value), validate=True)
    except Exception:
        decoded = b""
    if len(decoded) == exact_len:
        return bytearray(decoded)
    if len(value) == exact_len:
        return value
    raise SecretRotationError(f"secret input must be exactly {exact_len} bytes")


def zero_key_material(value: bytearray) -> None:
    for index in range(len(value)):
        value[index] = 0


def parse_smtp_credentials(raw: bytes) -> SmtpRotationCredentials:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecretRotationError(
            "SMTP credentials must be a UTF-8 JSON object"
        ) from exc
    if not isinstance(parsed, dict):
        raise SecretRotationError("SMTP credentials must be a JSON object")
    return SmtpRotationCredentials(
        host=_required_str(parsed, "host"),
        port=_port(parsed.get("port", 587)),
        from_addr=_required_str(parsed, "from", fallback_key="from_addr"),
        user=_optional_str(parsed.get("user")),
        password=secret_bytes_from_input(_required_str(parsed, "password").encode()),
        use_tls=_bool(parsed.get("use_tls", True), key="use_tls"),
        timeout=_positive_int(parsed.get("timeout", 10), key="timeout"),
        bounce_domain=_optional_str(parsed.get("bounce_domain")),
    )


def rotate_smtp_credentials(
    session: Session,
    *,
    settings: Settings,
    credentials: SmtpRotationCredentials,
    probe: SmtpProbe | None = None,
    clock: Clock | None = None,
) -> SecretRotationResult:
    """Validate and persist SMTP runtime credentials."""
    (probe or _probe_smtp)(credentials)
    now = _now(clock)
    password = _secret_text(credentials.password)
    password_envelope_id = _encrypt_secret(
        session,
        settings=settings,
        plaintext=password.encode("utf-8"),
        purpose=SMTP_PASSWORD_PURPOSE,
        setting_key=SMTP_PASSWORD_SETTING,
    )
    values: dict[str, object] = {
        SMTP_HOST_SETTING: credentials.host,
        SMTP_PORT_SETTING: credentials.port,
        SMTP_FROM_SETTING: credentials.from_addr,
        SMTP_PASSWORD_SETTING: password_envelope_id,
        SMTP_USE_TLS_SETTING: credentials.use_tls,
        SMTP_TIMEOUT_SETTING: credentials.timeout,
    }
    if credentials.user is not None:
        values[SMTP_USER_SETTING] = credentials.user
    if credentials.bounce_domain is not None:
        values[SMTP_BOUNCE_DOMAIN_SETTING] = credentials.bounce_domain
    with tenant_agnostic():
        for key, value in values.items():
            _upsert_setting(session, key=key, value=value, updated_at=now)
        _deployment_audit(
            session,
            action="secrets.smtp.rotated",
            entity_id="smtp",
            diff={
                "keys": sorted(values),
                "password": {"display_stub": smtp_password_display_stub()},
            },
            clock=clock,
        )
        session.flush()
    return SecretRotationResult(
        action="secrets.smtp.rotated",
        rotated=tuple(sorted(values)),
    )


def rotate_openrouter_key(
    session: Session,
    *,
    settings: Settings,
    api_key: bytearray,
    probe: OpenRouterProbe | None = None,
    base_url: str = _DEFAULT_OPENROUTER_BASE_URL,
    clock: Clock | None = None,
) -> SecretRotationResult:
    """Validate and persist the OpenRouter API key."""
    key_text = _secret_text(api_key)
    (probe or _probe_openrouter)(key_text, base_url=base_url)
    now = _now(clock)
    envelope_id = _encrypt_secret(
        session,
        settings=settings,
        plaintext=key_text.encode("utf-8"),
        purpose=OPENROUTER_API_KEY_PURPOSE,
        setting_key=OPENROUTER_API_KEY_SETTING,
    )
    with tenant_agnostic():
        _upsert_setting(
            session,
            key=OPENROUTER_API_KEY_SETTING,
            value=envelope_id,
            updated_at=now,
        )
        _deployment_audit(
            session,
            action="secrets.openrouter.rotated",
            entity_id="openrouter",
            diff={"api_key": {"display_stub": openrouter_api_key_display_stub()}},
            clock=clock,
        )
        session.flush()
    return SecretRotationResult(
        action="secrets.openrouter.rotated",
        rotated=(OPENROUTER_API_KEY_SETTING,),
    )


def rotate_hmac_signing_key(
    session: Session,
    *,
    settings: Settings,
    new_key: bytearray,
    purposes: Iterable[str] = DEFAULT_HMAC_PURPOSES,
    clock: Clock | None = None,
) -> SecretRotationResult:
    """Rotate deployment HMAC keys for all configured logical purposes."""
    key = bytes(secret_bytes_from_input(bytes(new_key), exact_len=HMAC_KEY_BYTES))
    now = _now(clock)
    purge_after = now + HMAC_ROTATION_WINDOW
    rotated: list[str] = []
    for purpose in purposes:
        rotate_hmac_key(
            session,
            purpose,
            key,
            purge_after=purge_after,
            settings=settings,
            clock=clock,
        )
        rotated.append(purpose)
    with tenant_agnostic():
        _deployment_audit(
            session,
            action="secrets.hmac.rotated",
            entity_id="hmac",
            diff={"purposes": rotated, "legacy_until": purge_after.isoformat()},
            clock=clock,
        )
        session.flush()
    return SecretRotationResult(
        action="secrets.hmac.rotated",
        rotated=tuple(rotated),
        legacy_until=purge_after,
    )


def rotate_session_secret(
    session: Session,
    *,
    settings: Settings,
    new_key: bytearray,
    clock: Clock | None = None,
) -> SecretRotationResult:
    """Rotate session signing material and clear all session rows."""
    key = bytes(secret_bytes_from_input(bytes(new_key), exact_len=32))
    rotate_session_signing_key(session, key, settings=settings, clock=clock)
    with tenant_agnostic():
        _deployment_audit(
            session,
            action="secrets.session.rotated",
            entity_id="session",
            diff={"sessions": "cleared"},
            clock=clock,
        )
        session.flush()
    return SecretRotationResult(
        action="secrets.session.rotated",
        rotated=("session.signing_key",),
    )


def rotation_result_payload(result: SecretRotationResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "action": result.action,
        "rotated": list(result.rotated),
    }
    if result.legacy_until is not None:
        payload["legacy_until"] = result.legacy_until.isoformat()
    return payload


def _encrypt_secret(
    session: Session,
    *,
    settings: Settings,
    plaintext: bytes,
    purpose: str,
    setting_key: str,
) -> str:
    root_key = settings.root_key
    if root_key is None:
        raise SecretRotationError("CREWDAY_ROOT_KEY is required to rotate secrets")
    envelope = Aes256GcmEnvelope(
        root_key,
        repository=SqlAlchemySecretEnvelopeRepository(session),
    )
    pointer = envelope.encrypt(
        plaintext,
        purpose=purpose,
        owner=EnvelopeOwner(kind="deployment_setting", id=setting_key),
    )
    if setting_key == OPENROUTER_API_KEY_SETTING:
        return openrouter_envelope_id_from_pointer(pointer)
    return smtp_envelope_id_from_pointer(pointer)


def _upsert_setting(
    session: Session, *, key: str, value: object, updated_at: datetime
) -> None:
    row = session.scalars(
        select(DeploymentSetting).where(DeploymentSetting.key == key)
    ).first()
    if row is None:
        session.add(
            DeploymentSetting(
                key=key,
                value=value,
                updated_at=updated_at,
                updated_by=SYSTEM_ACTOR_ID,
            )
        )
        return
    row.value = value
    row.updated_at = updated_at
    row.updated_by = SYSTEM_ACTOR_ID


def _deployment_audit(
    session: Session,
    *,
    action: str,
    entity_id: str,
    diff: dict[str, object],
    clock: Clock | None,
) -> None:
    write_deployment_audit(
        session,
        actor_id=SYSTEM_ACTOR_ID,
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        correlation_id=new_ulid(clock=clock),
        entity_kind="deployment_secret",
        entity_id=entity_id,
        action=action,
        diff=diff,
        via="cli",
        clock=clock,
    )


def _probe_smtp(credentials: SmtpRotationCredentials) -> None:
    smtp: smtplib.SMTP | smtplib.SMTP_SSL
    if credentials.port == _IMPLICIT_TLS_PORT and credentials.use_tls:
        smtp = smtplib.SMTP_SSL(
            credentials.host, credentials.port, timeout=credentials.timeout
        )
    else:
        smtp = smtplib.SMTP(
            credentials.host, credentials.port, timeout=credentials.timeout
        )
    try:
        smtp.ehlo()
        if (
            credentials.use_tls
            and credentials.port != _IMPLICIT_TLS_PORT
            and credentials.port != _PLAIN_PORT
        ):
            smtp.starttls()
            smtp.ehlo()
        if credentials.user is not None:
            smtp.login(credentials.user, _secret_text(credentials.password))
    except Exception as exc:
        raise SecretRotationError("SMTP credential probe failed") from exc
    finally:
        with contextlib.suppress(smtplib.SMTPException):
            smtp.quit()


def _probe_openrouter(api_key: str, *, base_url: str) -> None:
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SecretRotationError("OpenRouter key probe failed") from exc


def _required_str(
    values: dict[str, Any], key: str, *, fallback_key: str | None = None
) -> str:
    raw = values.get(key)
    if raw is None and fallback_key is not None:
        raw = values.get(fallback_key)
    value = _optional_str(raw)
    if value is None:
        raise SecretRotationError(f"SMTP credentials missing non-blank {key!r}")
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value
    raise SecretRotationError("expected a non-blank string")


def _bool(value: object, *, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise SecretRotationError(f"{key} must be boolean")


def _port(value: object) -> int:
    number = _positive_int(value, key="port")
    if number > 65535:
        raise SecretRotationError("port must be between 1 and 65535")
    return number


def _positive_int(value: object, *, key: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    raise SecretRotationError(f"{key} must be a positive integer")


def _secret_text(value: bytearray) -> str:
    try:
        text = bytes(value).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretRotationError("secret input must be UTF-8 text") from exc
    if not text.strip():
        raise SecretRotationError("secret input must not be empty")
    return text


def _now(clock: Clock | None) -> datetime:
    source = clock if clock is not None else SystemClock()
    return source.now().astimezone(UTC)
