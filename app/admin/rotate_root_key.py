"""Host-only root-key rotation helpers.

The public entry point is the ``crewday admin rotate-root-key`` CLI. There is
intentionally no HTTP surface: root-key rotation is a deployment operator
action that runs on the server host against the configured database.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.secrets.models import RootKeySlot, SecretEnvelope
from app.adapters.db.secrets.repositories import (
    SqlAlchemySecretEnvelopeRepository,
    resolve_root_key_ref,
)
from app.adapters.db.session import make_uow
from app.adapters.storage.envelope import Aes256GcmEnvelope, compute_key_fingerprint
from app.audit import write_deployment_audit
from app.auth.keys import derive_subkey
from app.config import Settings, get_settings
from app.tenancy import tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

SYSTEM_ACTOR_ID = "00000000000000000000000000"
ROTATION_WINDOW = timedelta(hours=72)
ENV_ROOT_KEY_REF = "env:CREWDAY_ROOT_KEY"


@dataclass(frozen=True, slots=True)
class RootKeyRotationResult:
    action: str
    active_key_fp: str
    legacy_key_fp: str | None = None
    rows_reencrypted: int = 0
    slots_purged: int = 0


class RootKeyRotationError(RuntimeError):
    """Operator-facing root-key rotation failure."""


def load_new_key_file(path: Path) -> bytearray:
    """Read a root key from a 0600 operator-owned regular file."""
    _validate_key_file_mode(path)
    return normalise_new_key_material(path.read_bytes())


def normalise_new_key_material(raw: bytes) -> bytearray:
    """Validate CLI-supplied new root-key material.

    The runtime derives fingerprints from the UTF-8 string stored in
    ``CREWDAY_ROOT_KEY``. For compatibility, base64-encoded 32-byte keys keep
    their base64 text as the root-key string; raw 32-byte ASCII keys are also
    accepted for local/operator tooling.
    """
    stripped = raw.strip()
    if not stripped:
        raise RootKeyRotationError("new root key must not be empty")
    try:
        stripped.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RootKeyRotationError("new root key must be UTF-8 text") from exc

    try:
        decoded = base64.b64decode(stripped, validate=True)
    except Exception:
        decoded = b""
    if len(decoded) == 32 or len(stripped) == 32:
        return bytearray(stripped)
    raise RootKeyRotationError(
        "new root key must be base64-encoded 32 bytes or raw 32-byte text"
    )


def start_rotation(
    session: Session,
    *,
    settings: Settings,
    new_key: bytearray,
    new_key_ref: str,
    clock: Clock | None = None,
) -> RootKeyRotationResult:
    """Register the new active key and keep the old key slot for 72 hours."""
    old_key = _settings_root_key(settings)
    new_secret = _secret_from_bytearray(new_key)
    now = _now(clock)
    old_fp = compute_key_fingerprint(old_key)
    new_fp = compute_key_fingerprint(new_secret)
    if old_fp == new_fp:
        raise RootKeyRotationError("new root key matches the active root key")

    with tenant_agnostic():
        for slot in session.scalars(
            select(RootKeySlot).where(RootKeySlot.is_active.is_(True))
        ):
            slot.is_active = False
            slot.retired_at = now
            slot.purge_after = now + ROTATION_WINDOW
        _upsert_slot(
            session,
            key_fp=old_fp,
            key_ref=ENV_ROOT_KEY_REF,
            is_active=False,
            activated_at=now,
            retired_at=now,
            purge_after=now + ROTATION_WINDOW,
            notes="retired by rotate-root-key",
        )
        _upsert_slot(
            session,
            key_fp=new_fp,
            key_ref=new_key_ref,
            is_active=True,
            activated_at=now,
            retired_at=None,
            purge_after=None,
            notes="active by rotate-root-key",
        )
        _deployment_audit(
            session,
            action="key_rotation.started",
            entity_id=new_fp.hex(),
            diff={"legacy_key_fp": old_fp.hex(), "active_key_ref": new_key_ref},
            clock=clock,
        )
        session.flush()
    return RootKeyRotationResult(
        action="started",
        active_key_fp=new_fp.hex(),
        legacy_key_fp=old_fp.hex(),
    )


def reencrypt_legacy_rows(
    session: Session,
    *,
    settings: Settings,
    clock: Clock | None = None,
) -> RootKeyRotationResult:
    """Rewrite all secret envelopes not stamped with the active fingerprint."""
    active_key = _active_root_key(session, settings=settings)
    active_fp = compute_key_fingerprint(active_key)
    repository = SqlAlchemySecretEnvelopeRepository(session)
    decrypt_root = settings.root_key if settings.root_key is not None else active_key
    decryptor = Aes256GcmEnvelope(decrypt_root, repository=repository, clock=clock)
    now = _now(clock)
    count = 0

    with tenant_agnostic():
        rows = list(
            session.scalars(
                select(SecretEnvelope).where(SecretEnvelope.key_fp != active_fp)
            )
        )
        for row in rows:
            plaintext = decryptor.decrypt(
                b"\x02" + row.id.encode("utf-8"),
                purpose=row.purpose,
            )
            nonce = os.urandom(12)
            key = derive_subkey(active_key, purpose=f"storage.envelope.{row.purpose}")
            row.ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
            row.nonce = nonce
            row.key_fp = active_fp
            row.rotated_at = now
            count += 1
        if count:
            _deployment_audit(
                session,
                action="key_rotation.progress",
                entity_id=active_fp.hex(),
                diff={"rows": count},
                clock=clock,
            )
        session.flush()
    return RootKeyRotationResult(
        action="reencrypted",
        active_key_fp=active_fp.hex(),
        rows_reencrypted=count,
    )


def finalize_rotation(
    session: Session,
    *,
    settings: Settings,
    finalize_now: bool = False,
    clock: Clock | None = None,
) -> RootKeyRotationResult:
    """Purge retired key slots after all rows have moved to the active key."""
    active_key = _active_root_key(session, settings=settings)
    active_fp = compute_key_fingerprint(active_key)
    now = _now(clock)
    purged = 0

    with tenant_agnostic():
        slots = list(
            session.scalars(
                select(RootKeySlot).where(RootKeySlot.is_active.is_(False))
            )
        )
        for slot in slots:
            pending = session.scalar(
                select(func.count())
                .select_from(SecretEnvelope)
                .where(SecretEnvelope.key_fp == slot.key_fp)
            ) or 0
            if pending:
                raise RootKeyRotationError(
                    f"cannot finalize; {pending} envelope row(s) still use "
                    f"{bytes(slot.key_fp).hex()}"
                )
            if not finalize_now and (
                slot.purge_after is None or slot.purge_after > now
            ):
                continue
            session.delete(slot)
            purged += 1
        if purged:
            _deployment_audit(
                session,
                action="key_rotation.finalized",
                entity_id=active_fp.hex(),
                diff={"slots_purged": purged, "finalize_now": finalize_now},
                clock=clock,
            )
        session.flush()
    return RootKeyRotationResult(
        action="finalized",
        active_key_fp=active_fp.hex(),
        slots_purged=purged,
    )


def result_payload(result: RootKeyRotationResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "action": result.action,
        "active_key_fp": result.active_key_fp,
        "rows_reencrypted": result.rows_reencrypted,
        "slots_purged": result.slots_purged,
    }
    if result.legacy_key_fp is not None:
        payload["legacy_key_fp"] = result.legacy_key_fp
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.admin.rotate_root_key")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--new-key-file", type=Path)
    mode.add_argument("--new-key-stdin", action="store_true")
    mode.add_argument("--reencrypt", action="store_true")
    mode.add_argument("--finalize", action="store_true")
    mode.add_argument("--finalize-now", action="store_true")
    mode.add_argument("--new", dest="legacy_new_value")
    args = parser.parse_args(argv)

    if args.legacy_new_value is not None:
        raise RootKeyRotationError(
            "--new <value> is refused because it leaks root keys through shell "
            "history and process listings; use --new-key-file or --new-key-stdin"
        )

    settings = get_settings()
    with make_uow() as raw_session:
        assert isinstance(raw_session, Session)
        if args.new_key_file is not None:
            key = load_new_key_file(args.new_key_file)
            try:
                result = start_rotation(
                    raw_session,
                    settings=settings,
                    new_key=key,
                    new_key_ref=f"file:{args.new_key_file}",
                )
            finally:
                zero_key_material(key)
        elif args.new_key_stdin:
            if sys.stdin.isatty():
                raise RootKeyRotationError("--new-key-stdin refuses an interactive TTY")
            key = normalise_new_key_material(sys.stdin.buffer.read())
            try:
                result = start_rotation(
                    raw_session,
                    settings=settings,
                    new_key=key,
                    new_key_ref=ENV_ROOT_KEY_REF,
                )
            finally:
                zero_key_material(key)
        elif args.reencrypt:
            result = reencrypt_legacy_rows(raw_session, settings=settings)
        else:
            result = finalize_rotation(
                raw_session,
                settings=settings,
                finalize_now=bool(args.finalize_now),
            )
    print(json.dumps(result_payload(result), sort_keys=True))
    return 0


def _settings_root_key(settings: Settings) -> SecretStr:
    if settings.root_key is None:
        raise RootKeyRotationError("CREWDAY_ROOT_KEY must be set")
    return settings.root_key


def _active_root_key(session: Session, *, settings: Settings) -> SecretStr:
    with tenant_agnostic():
        slot = session.scalars(
            select(RootKeySlot).where(RootKeySlot.is_active.is_(True))
        ).first()
    if slot is not None:
        resolved = resolve_root_key_ref(slot.key_ref)
        if resolved is not None:
            return resolved
    return _settings_root_key(settings)


def _upsert_slot(
    session: Session,
    *,
    key_fp: bytes,
    key_ref: str,
    is_active: bool,
    activated_at: datetime,
    retired_at: datetime | None,
    purge_after: datetime | None,
    notes: str,
) -> None:
    slot = session.scalars(
        select(RootKeySlot).where(RootKeySlot.key_fp == key_fp)
    ).first()
    if slot is None:
        session.add(
            RootKeySlot(
                id=new_ulid(),
                key_fp=key_fp,
                key_ref=key_ref,
                is_active=is_active,
                activated_at=activated_at,
                retired_at=retired_at,
                purge_after=purge_after,
                notes=notes,
            )
        )
        return
    slot.key_ref = key_ref
    slot.is_active = is_active
    slot.activated_at = activated_at
    slot.retired_at = retired_at
    slot.purge_after = purge_after
    slot.notes = notes


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
        entity_kind="root_key_slot",
        entity_id=entity_id,
        action=action,
        diff=diff,
        via="cli",
        clock=clock,
    )


def _secret_from_bytearray(value: bytearray) -> SecretStr:
    return SecretStr(bytes(value).decode("utf-8"))


def zero_key_material(value: bytearray) -> None:
    """Overwrite mutable key material held by CLI input handling."""
    for index in range(len(value)):
        value[index] = 0


def _validate_key_file_mode(path: Path) -> None:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise RootKeyRotationError("--new-key-file must point to a regular file")
    if info.st_uid != os.getuid():
        raise RootKeyRotationError("--new-key-file must be owned by the current user")
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o600:
        raise RootKeyRotationError("--new-key-file must have mode 0600")


def _now(clock: Clock | None) -> datetime:
    source = clock if clock is not None else SystemClock()
    return source.now().astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
