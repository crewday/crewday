"""Local-filesystem :class:`~app.adapters.storage.ports.Storage` backend.

Blobs are content-addressed by SHA-256 and live on disk under
``<data_dir>/uploads/<ab>/<abcdef…>`` where ``<ab>`` is the first two
hex characters of the 64-char digest. The two-level fanout keeps
``ls`` on any single directory bounded (≤ 65,536 entries worst case)
and leaves the key space flat enough for simple rsync-based backups.

See ``docs/specs/01-architecture.md`` §"Adapters/storage",
``docs/specs/21-assets.md`` and
``docs/specs/15-security-privacy.md`` §"Blob download authorization".

## Why a temp file + ``os.replace``

``put`` is the only mutation path. It writes bytes to
``<full-hash>.tmp`` in the same directory as the final target, streams
through :func:`hashlib.sha256` while writing so a mid-stream tamper is
caught before the rename, and uses :func:`os.replace` to promote the
temp file into place atomically. Both files live on the same
filesystem, which is the contract POSIX requires for
``rename(2)`` atomicity — putting the temp under ``/tmp`` would break
that on any deploy where ``/tmp`` is a separate mount.

## Signed URLs

``sign_url`` produces a relative path of the form
``/api/v1/files/<signature>?h=<hash>&e=<exp>``. The signature is
``HMAC-SHA256(signing_key, f"{content_hash}.{exp}")`` rendered hex.
Including ``h`` in the query is redundant with the signed content
but gives the verifier a cross-check that the caller didn't swap the
hash portion of the URL between issue and verification — without it
we'd have to re-walk the URL to reconstruct the message, and any
buggy rewrite (trailing slash, case fold) would silently flip a
verification failure into a verification success on the *wrong* blob.

The signer is injected by the caller with purpose ``"storage-sign"``
so this module stays free of config and database reads — the storage
layer doesn't know what :class:`Settings` is.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Final, Protocol
from urllib.parse import parse_qs, urlsplit

from app.adapters.storage.ports import Blob, BlobNotFound
from app.util.clock import Clock, SystemClock

__all__ = [
    "LocalFsStorage",
    "SignatureExpired",
    "SignatureInvalid",
]


# Buffer size for streaming IO. 64 KiB is the sweet spot for Linux
# page-cache throughput on SHA-256 hashing: smaller reads add syscall
# overhead, larger reads don't help because the hashing step becomes
# the bottleneck, not the read.
_CHUNK_SIZE: Final[int] = 64 * 1024

# Only lowercase 64-char hex is valid. SHA-256 is 256 bits → 64 hex
# chars; the lowercase requirement is a content-addressing invariant
# so a caller can't store the same bytes twice under
# ``AB…`` and ``ab…``.
_HASH_LEN: Final[int] = 64

# Relative-path template for the signed URL. The router mounts at
# this prefix (§12) and the verifier only accepts this shape.
_SIGNED_URL_PREFIX: Final[str] = "/api/v1/files/"
_STORAGE_SIGN_PURPOSE: Final[str] = "storage-sign"


class SignatureInvalid(ValueError):
    """Raised when a signed URL's signature does not verify."""


class SignatureExpired(ValueError):
    """Raised when a signed URL's ``exp`` is in the past."""


class _HmacSigner(Protocol):
    def sign(self, message: bytes, *, purpose: str) -> str:
        """Return the hex HMAC-SHA256 signature for ``message``."""

    def verify(self, message: bytes, signature: str, *, purpose: str) -> bool:
        """Return whether ``signature`` verifies for ``message``."""


class _StaticHmacSigner:
    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes):
            raise TypeError("signing_key must be bytes")
        if not key:
            raise ValueError("signing_key must be non-empty")
        self._key = key

    def sign(self, message: bytes, *, purpose: str) -> str:
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def verify(self, message: bytes, signature: str, *, purpose: str) -> bool:
        if len(signature) != 64:
            return False
        try:
            bytes.fromhex(signature)
        except ValueError:
            return False
        expected = self.sign(message, purpose=purpose)
        return hmac.compare_digest(expected, signature)


class LocalFsStorage:
    """Content-addressed blob store backed by the local filesystem.

    Construction parameters:

    * ``data_dir`` — root where the ``uploads/`` tree lives. The
      directory is created on first write; parents must already
      exist (operators wire ``$CREWDAY_DATA_DIR`` at deploy time).
    * ``signing_key`` — legacy 32-byte static HMAC key used by tests
      and old wiring.
    * ``signer`` — preferred signer/verifier seam. Production passes
      the deployment-wide row-backed HMAC signer with purpose
      ``"storage-sign"``.
    * ``clock`` — :class:`app.util.clock.Clock` for deterministic
      tests. Defaults to :class:`SystemClock`.

    Thread-safety: the store is safe for concurrent ``put`` calls on
    the same content hash — the atomic rename makes the winner's
    bytes visible indivisibly, and the pre-write existence check
    short-circuits the loser. No in-process locking is attempted
    (the FS is the coordination surface).
    """

    __slots__ = ("clock", "data_dir", "signer")

    def __init__(
        self,
        data_dir: Path,
        *,
        signing_key: bytes | None = None,
        signer: _HmacSigner | None = None,
        clock: Clock | None = None,
    ) -> None:
        if signing_key is None and signer is None:
            raise ValueError("signing_key or signer is required")
        if signing_key is not None and signer is not None:
            raise ValueError("pass either signing_key or signer, not both")
        self.data_dir = Path(data_dir)
        if signer is None:
            if signing_key is None:  # pragma: no cover - guarded above
                raise ValueError("signing_key or signer is required")
            signer = _StaticHmacSigner(signing_key)
        self.signer = signer
        self.clock: Clock = clock if clock is not None else SystemClock()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @property
    def _uploads_root(self) -> Path:
        return self.data_dir / "uploads"

    def _path_for(self, content_hash: str) -> Path:
        """Return the on-disk path for ``content_hash``.

        Assumes ``content_hash`` has been validated by
        :func:`_validate_hash`.
        """
        return self._uploads_root / content_hash[:2] / content_hash

    # ------------------------------------------------------------------
    # Port surface
    # ------------------------------------------------------------------

    def put(
        self,
        content_hash: str,
        data: IO[bytes],
        *,
        content_type: str | None = None,
    ) -> Blob:
        _validate_hash(content_hash)
        target = self._path_for(content_hash)

        if target.is_symlink():
            # A symlink under ``uploads/`` is never produced by this
            # store; its presence means either operator error or a
            # pre-placed attacker file trying to make ``put`` /
            # ``get`` read-or-clobber files outside the uploads tree.
            # Refuse loudly rather than follow it.
            raise ValueError(
                f"refusing to operate on symlink at blob path for {content_hash!r}"
            )

        if target.is_file():
            # Dedupe: the blob is already on disk. Verify the existing
            # bytes still hash to what the caller claims before handing
            # the metadata back — protects against bit-rot and makes
            # a collision attempt loud (mismatched hash → ValueError).
            size = _verify_existing(target, content_hash)
            stat = target.stat()
            created_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            return Blob(
                content_hash=content_hash,
                size_bytes=size,
                content_type=content_type,
                created_at=created_at,
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(f"{content_hash}.tmp")

        try:
            size = _stream_to_temp(data, tmp_path, content_hash)
        except BaseException:
            # Any error mid-write (stream raised, digest mismatch, IO
            # failure) leaves the target path untouched. Best-effort
            # unlink of the temp so we don't accumulate half-written
            # files — missing_ok because the write may have failed
            # before the file was even created.
            tmp_path.unlink(missing_ok=True)
            raise

        # Atomic promote: same-directory rename is guaranteed atomic on
        # POSIX; :func:`os.replace` covers Windows too (REPLACE_EXISTING
        # semantics) even though we only target Linux/macOS today.
        os.replace(tmp_path, target)
        _fsync_dir(target.parent)

        return Blob(
            content_hash=content_hash,
            size_bytes=size,
            content_type=content_type,
            created_at=self.clock.now(),
        )

    def get(self, content_hash: str) -> IO[bytes]:
        _validate_hash(content_hash)
        target = self._path_for(content_hash)
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(target, flags)
        except FileNotFoundError as exc:
            raise BlobNotFound(content_hash) from exc
        # O_NOFOLLOW: if an attacker pre-placed a symlink under
        # uploads/ the open raises OSError(ELOOP) rather than
        # reading arbitrary files the service user can read. Same
        # defense-in-depth as :func:`_stream_to_temp`.
        return os.fdopen(fd, "rb")

    def exists(self, content_hash: str) -> bool:
        _validate_hash(content_hash)
        return self._path_for(content_hash).is_file()

    def delete(self, content_hash: str) -> None:
        _validate_hash(content_hash)
        self._path_for(content_hash).unlink(missing_ok=True)

    def sign_url(self, content_hash: str, *, ttl_seconds: int) -> str:
        _validate_hash(content_hash)
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")
        exp = int(self.clock.now().timestamp()) + ttl_seconds
        signature = self.signer.sign(
            _signature_message(content_hash, exp),
            purpose=_STORAGE_SIGN_PURPOSE,
        )
        return f"{_SIGNED_URL_PREFIX}{signature}?h={content_hash}&e={exp}"

    def verify_signed_url(self, url: str, *, now: datetime) -> str:
        """Return ``content_hash`` if ``url`` verifies.

        Raises :class:`SignatureInvalid` for any structural or
        cryptographic failure, :class:`SignatureExpired` when the
        signature verifies but ``exp`` is already in the past. The
        expiry check runs *after* the signature check so a tampered
        URL never reveals "it would have expired anyway".
        """
        parts = urlsplit(url)
        path = parts.path
        if not path.startswith(_SIGNED_URL_PREFIX):
            raise SignatureInvalid("url path does not match signed-url prefix")
        signature = path[len(_SIGNED_URL_PREFIX) :]
        if not signature or "/" in signature:
            raise SignatureInvalid("url signature segment is missing or malformed")

        query = parse_qs(parts.query, keep_blank_values=False)
        hash_values = query.get("h", [])
        exp_values = query.get("e", [])
        if len(hash_values) != 1 or len(exp_values) != 1:
            raise SignatureInvalid(
                "url must carry exactly one 'h' and one 'e' query param"
            )
        content_hash = hash_values[0]
        try:
            _validate_hash(content_hash)
        except ValueError as exc:
            raise SignatureInvalid("url 'h' is not a valid content hash") from exc
        try:
            exp = int(exp_values[0])
        except ValueError as exc:
            raise SignatureInvalid("url 'e' is not an integer") from exc

        if not self.signer.verify(
            _signature_message(content_hash, exp),
            signature,
            purpose=_STORAGE_SIGN_PURPOSE,
        ):
            raise SignatureInvalid("url signature does not verify")

        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            raise ValueError("now must be an aware datetime")
        if int(now.timestamp()) >= exp:
            raise SignatureExpired(f"signed url expired at unix {exp}")

        return content_hash


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _validate_hash(content_hash: str) -> None:
    """Raise :class:`ValueError` if ``content_hash`` is not 64-char lower hex."""
    if len(content_hash) != _HASH_LEN or not all(
        c in "0123456789abcdef" for c in content_hash
    ):
        raise ValueError(
            f"content_hash must be {_HASH_LEN}-character lowercase hex; "
            f"got {content_hash!r}"
        )


def _signature_message(content_hash: str, exp: int) -> bytes:
    """Return the ASCII message signed for ``content_hash`` and ``exp``."""
    return f"{content_hash}.{exp}".encode("ascii")


def _stream_to_temp(src: IO[bytes], dst: Path, expected_hash: str) -> int:
    """Copy ``src`` to ``dst`` chunked, verifying the digest; return byte count.

    Raises :class:`ValueError` if the streamed bytes don't hash to
    ``expected_hash``. The temp file is flushed + ``fsync``-ed before
    the caller promotes it with :func:`os.replace`, so the rename
    target is durable if the process dies before the return.

    Opened with ``O_CREAT | O_EXCL | O_NOFOLLOW | O_WRONLY`` so that
    (a) a pre-existing tmp file (stale from a crashed write, or
    placed by an attacker with write access under ``$DATA/uploads/``)
    causes the put to fail loudly rather than clobber / follow a
    symlink, and (b) a symlink at the tmp path cannot redirect the
    write into an attacker-controlled file outside the uploads tree.
    """
    digest = hashlib.sha256()
    size = 0
    # O_NOFOLLOW: refuse to follow a symlink at the tmp path.
    # O_EXCL: fail if the tmp file already exists — no clobber.
    # O_CLOEXEC: keep the fd from leaking to child processes.
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(dst, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = src.read(_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                out.write(chunk)
                size += len(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        # ``os.fdopen`` takes ownership of ``fd``; on its failure the
        # fd is closed. If :func:`os.open` itself raised, we never
        # reach here. Nothing to do — the caller's outer ``except``
        # unlinks the tmp path.
        raise
    actual = digest.hexdigest()
    if actual != expected_hash:
        raise ValueError(
            f"content_hash mismatch: expected {expected_hash!r}, got {actual!r}"
        )
    return size


def _verify_existing(target: Path, expected_hash: str) -> int:
    """Re-hash ``target`` while streaming, assert match, return byte count.

    Streams through :class:`hashlib.sha256` in :const:`_CHUNK_SIZE`
    chunks so re-putting a 100 MiB blob doesn't slurp the file into
    memory. Any mismatch raises :class:`ValueError` — content
    addressing is the correctness invariant of this store, and a
    silent mismatch would make subsequent ``get`` calls return the
    wrong bytes under the caller's hash.

    Opened with ``O_NOFOLLOW`` so a symlink pre-placed at the target
    path cannot turn the re-hash into an oracle on arbitrary files
    the service user can read. Same rationale as
    :func:`_stream_to_temp`.
    """
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(target, flags)
    with os.fdopen(fd, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    actual = digest.hexdigest()
    if actual != expected_hash:
        raise ValueError(
            f"content_hash mismatch on existing blob: "
            f"expected {expected_hash!r}, got {actual!r}"
        )
    return size


def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync so the rename is durable across crashes.

    Platforms that don't support ``fsync`` on a directory handle
    (Windows) raise :class:`OSError`; treat that as a no-op — the
    atomic-rename durability story is a POSIX concern and the
    Windows path still gets the filesystem's best effort via the
    parent :func:`os.replace`.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        # fsync on a directory is not supported everywhere; swallow
        # because the operation is a best-effort durability hint, not
        # a correctness requirement.
        pass
    finally:
        os.close(fd)
