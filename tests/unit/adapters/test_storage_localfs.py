"""Unit tests for :class:`app.adapters.storage.localfs.LocalFsStorage`.

Covers the content-addressed write path (atomic rename, dedupe,
digest verification), the IO surface (``get`` / ``exists`` /
``delete``), and the signed-URL round trip (``sign_url`` +
``verify_signed_url``).

See ``docs/specs/01-architecture.md`` §"Adapters/storage" and
``docs/specs/15-security-privacy.md`` §"Blob download authorization".
"""

from __future__ import annotations

import hashlib
import io
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.base import Base
from app.adapters.storage.localfs import (
    LocalFsStorage,
    SignatureExpired,
    SignatureInvalid,
)
from app.adapters.storage.ports import Blob, BlobNotFound
from app.config import Settings
from app.security.hmac_signer import HmacSigner, rotate_hmac_key
from app.util.clock import FrozenClock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_EPOCH = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_SIGNING_KEY = b"\x11" * 32


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _store(tmp_path: Path, *, clock: FrozenClock | None = None) -> LocalFsStorage:
    return LocalFsStorage(
        tmp_path,
        signing_key=_SIGNING_KEY,
        clock=clock if clock is not None else FrozenClock(_EPOCH),
    )


class _ExplodingStream(io.BytesIO):
    """:class:`io.BytesIO` that raises ``RuntimeError`` on the Nth ``read``.

    Used to exercise the ``put`` error path — a stream that fails mid-
    write must leave the target path untouched and clean up the temp
    file. Subclassing :class:`io.BytesIO` keeps the object a proper
    :class:`IO[bytes]` (typeshed ships a special case for ``BytesIO``)
    so the call site typechecks without any ``cast`` or ignore.
    """

    def __init__(self, chunks: list[bytes], *, raise_on: int) -> None:
        super().__init__()
        self._chunks: list[bytes] = list(chunks)
        self._raise_on = raise_on
        self._reads = 0

    def read(self, size: int | None = -1) -> bytes:
        self._reads += 1
        if self._reads == self._raise_on:
            raise RuntimeError("boom")
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LocalFsStorage]:
    yield _store(tmp_path)


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-localfs-hmac-root-key"),
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


# ---------------------------------------------------------------------------
# put
# ---------------------------------------------------------------------------


class TestPut:
    def test_put_writes_under_fanout_path(
        self, store: LocalFsStorage, tmp_path: Path
    ) -> None:
        payload = b"hello blob"
        content_hash = _sha256(payload)
        blob = store.put(content_hash, io.BytesIO(payload))

        expected_path = tmp_path / "uploads" / content_hash[:2] / content_hash
        assert expected_path.is_file()
        assert expected_path.read_bytes() == payload
        assert blob == Blob(
            content_hash=content_hash,
            size_bytes=len(payload),
            content_type=None,
            created_at=_EPOCH,
        )

    def test_put_preserves_content_type_on_blob(self, store: LocalFsStorage) -> None:
        payload = b"image-bytes"
        content_hash = _sha256(payload)
        blob = store.put(content_hash, io.BytesIO(payload), content_type="image/png")
        assert blob.content_type == "image/png"

    def test_put_twice_same_bytes_is_noop_and_idempotent(
        self, store: LocalFsStorage, tmp_path: Path
    ) -> None:
        payload = b"dedupe me"
        content_hash = _sha256(payload)
        first = store.put(content_hash, io.BytesIO(payload))
        target = tmp_path / "uploads" / content_hash[:2] / content_hash
        mtime_before = target.stat().st_mtime

        second = store.put(content_hash, io.BytesIO(payload))
        mtime_after = target.stat().st_mtime
        # No rewrite — the mtime is unchanged because the store
        # short-circuited on the existing file.
        assert mtime_before == mtime_after
        assert first.content_hash == second.content_hash
        assert first.size_bytes == second.size_bytes

    def test_put_dedupe_returns_content_type_from_current_call(
        self, store: LocalFsStorage
    ) -> None:
        payload = b"dedupe mime"
        content_hash = _sha256(payload)
        store.put(content_hash, io.BytesIO(payload), content_type="image/png")
        blob = store.put(content_hash, io.BytesIO(payload), content_type="image/jpeg")
        # content_type isn't persisted; each call's declared MIME is
        # reflected on the returned Blob.
        assert blob.content_type == "image/jpeg"

    def test_put_with_mismatched_hash_raises(self, store: LocalFsStorage) -> None:
        payload = b"actual bytes"
        bogus_hash = "f" * 64
        with pytest.raises(ValueError, match="content_hash mismatch"):
            store.put(bogus_hash, io.BytesIO(payload))

    def test_put_with_mismatched_hash_leaves_no_final_file(
        self, store: LocalFsStorage, tmp_path: Path
    ) -> None:
        payload = b"actual bytes"
        bogus_hash = "f" * 64
        with pytest.raises(ValueError):
            store.put(bogus_hash, io.BytesIO(payload))
        target = tmp_path / "uploads" / bogus_hash[:2] / bogus_hash
        assert not target.exists()
        # Temp is cleaned up too — no leftover ``<hash>.tmp`` in the
        # fanout directory.
        tmp = target.with_name(f"{bogus_hash}.tmp")
        assert not tmp.exists()

    def test_put_with_invalid_hash_format_raises(self, store: LocalFsStorage) -> None:
        for bad in ["short", "G" * 64, "A" * 64, "0" * 63, "0" * 65, ""]:
            with pytest.raises(ValueError, match="lowercase hex"):
                store.put(bad, io.BytesIO(b""))

    def test_put_mid_stream_error_leaves_no_final_file_and_cleans_tmp(
        self, store: LocalFsStorage, tmp_path: Path
    ) -> None:
        # Build a payload whose hash is known, then use an exploding
        # stream that fails on the 3rd read (after 2 chunks have
        # reached the temp file).
        chunk = b"x" * 8
        chunks = [chunk, chunk]
        dummy_hash = _sha256(b"".join(chunks) + chunk)  # arbitrary but valid hex
        stream = _ExplodingStream(chunks, raise_on=3)
        with pytest.raises(RuntimeError, match="boom"):
            store.put(dummy_hash, stream)

        target = tmp_path / "uploads" / dummy_hash[:2] / dummy_hash
        assert not target.exists()
        tmp = target.with_name(f"{dummy_hash}.tmp")
        assert not tmp.exists()

    def test_put_creates_fanout_directory_with_parents(self, tmp_path: Path) -> None:
        # ``uploads/`` doesn't exist yet — put must create both the
        # uploads root and the fanout subdirectory.
        store = _store(tmp_path)
        payload = b"fresh tree"
        content_hash = _sha256(payload)
        assert not (tmp_path / "uploads").exists()
        store.put(content_hash, io.BytesIO(payload))
        assert (tmp_path / "uploads" / content_hash[:2]).is_dir()

    def test_put_detects_bit_rot_on_existing_blob(
        self, store: LocalFsStorage, tmp_path: Path
    ) -> None:
        # Seed a blob, then corrupt it out-of-band. A second ``put``
        # for the same declared hash must refuse rather than silently
        # hand back the corrupted bytes under a hash that no longer
        # matches.
        payload = b"original"
        content_hash = _sha256(payload)
        store.put(content_hash, io.BytesIO(payload))
        target = tmp_path / "uploads" / content_hash[:2] / content_hash
        target.write_bytes(b"tampered!")

        with pytest.raises(ValueError, match="content_hash mismatch on existing blob"):
            store.put(content_hash, io.BytesIO(payload))


# ---------------------------------------------------------------------------
# get / exists / delete
# ---------------------------------------------------------------------------


class TestRead:
    def test_get_roundtrips_bytes(self, store: LocalFsStorage) -> None:
        payload = b"round trip" * 100
        content_hash = _sha256(payload)
        store.put(content_hash, io.BytesIO(payload))
        handle: IO[bytes] = store.get(content_hash)
        try:
            assert handle.read() == payload
            # Read-only: writing must fail.
            with pytest.raises(OSError):
                handle.write(b"nope")
        finally:
            handle.close()

    def test_get_missing_raises_blob_not_found(self, store: LocalFsStorage) -> None:
        missing = "0" * 64
        with pytest.raises(BlobNotFound):
            store.get(missing)

    def test_get_invalid_hash_raises_value_error(self, store: LocalFsStorage) -> None:
        with pytest.raises(ValueError, match="lowercase hex"):
            store.get("not-hex")

    def test_exists_truthy_and_falsy(self, store: LocalFsStorage) -> None:
        payload = b"present"
        present_hash = _sha256(payload)
        absent_hash = "a" * 64
        store.put(present_hash, io.BytesIO(payload))
        assert store.exists(present_hash) is True
        assert store.exists(absent_hash) is False


class TestDelete:
    def test_delete_removes_blob(self, store: LocalFsStorage, tmp_path: Path) -> None:
        payload = b"delete me"
        content_hash = _sha256(payload)
        store.put(content_hash, io.BytesIO(payload))
        target = tmp_path / "uploads" / content_hash[:2] / content_hash
        assert target.is_file()
        store.delete(content_hash)
        assert not target.exists()
        assert store.exists(content_hash) is False

    def test_delete_missing_is_silent(self, store: LocalFsStorage) -> None:
        # Missing hash → no raise, no crash.
        store.delete("b" * 64)

    def test_delete_invalid_hash_raises(self, store: LocalFsStorage) -> None:
        with pytest.raises(ValueError, match="lowercase hex"):
            store.delete("nope")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_empty_signing_key_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="signing_key"):
            LocalFsStorage(tmp_path, signing_key=b"")

    def test_legacy_static_signing_key_accepts_existing_long_keys(
        self, tmp_path: Path
    ) -> None:
        store = LocalFsStorage(
            tmp_path,
            signing_key=b"legacy-static-signing-key-longer-than-32-bytes",
            clock=FrozenClock(_EPOCH),
        )
        content_hash = _sha256(b"hello")

        url = store.sign_url(content_hash, ttl_seconds=900)

        assert store.verify_signed_url(url, now=_EPOCH) == content_hash

    def test_default_clock_is_system_clock(self, tmp_path: Path) -> None:
        # Constructed without a clock: sign_url must still work and
        # return an ``e=`` value in the near future.
        store = LocalFsStorage(tmp_path, signing_key=_SIGNING_KEY)
        payload = b"no clock"
        content_hash = _sha256(payload)
        store.put(content_hash, io.BytesIO(payload))
        url = store.sign_url(content_hash, ttl_seconds=60)
        exp = int(parse_qs(urlsplit(url).query)["e"][0])
        assert exp > 0  # unix seconds; sanity check


# ---------------------------------------------------------------------------
# sign_url + verify_signed_url
# ---------------------------------------------------------------------------


class TestSignUrl:
    def test_sign_url_shape(self, store: LocalFsStorage) -> None:
        content_hash = _sha256(b"hello")
        url = store.sign_url(content_hash, ttl_seconds=900)
        parts = urlsplit(url)
        assert parts.path.startswith("/api/v1/files/")
        signature = parts.path[len("/api/v1/files/") :]
        assert len(signature) == 64  # hex-encoded SHA-256

        query = parse_qs(parts.query)
        assert query["h"] == [content_hash]
        exp = int(query["e"][0])
        assert exp == int(_EPOCH.timestamp()) + 900

    def test_sign_url_rejects_negative_ttl(self, store: LocalFsStorage) -> None:
        with pytest.raises(ValueError, match="ttl_seconds"):
            store.sign_url(_sha256(b"x"), ttl_seconds=-1)

    def test_verify_happy_path_returns_hash(self, store: LocalFsStorage) -> None:
        content_hash = _sha256(b"hello")
        url = store.sign_url(content_hash, ttl_seconds=900)
        # Verify one second before expiry.
        near_end = _EPOCH + timedelta(seconds=899)
        assert store.verify_signed_url(url, now=near_end) == content_hash

    def test_verify_tampered_hash_raises_invalid(self, store: LocalFsStorage) -> None:
        content_hash = _sha256(b"hello")
        url = store.sign_url(content_hash, ttl_seconds=900)
        swapped = url.replace(f"h={content_hash}", f"h={'c' * 64}")
        with pytest.raises(SignatureInvalid, match="does not verify"):
            store.verify_signed_url(swapped, now=_EPOCH)

    def test_verify_tampered_exp_raises_invalid(self, store: LocalFsStorage) -> None:
        content_hash = _sha256(b"hello")
        url = store.sign_url(content_hash, ttl_seconds=900)
        original_exp = int(_EPOCH.timestamp()) + 900
        forged_exp = original_exp + 3600  # extend by an hour
        tampered = url.replace(f"e={original_exp}", f"e={forged_exp}")
        with pytest.raises(SignatureInvalid, match="does not verify"):
            store.verify_signed_url(tampered, now=_EPOCH)

    def test_verify_tampered_signature_raises_invalid(
        self, store: LocalFsStorage
    ) -> None:
        content_hash = _sha256(b"hello")
        url = store.sign_url(content_hash, ttl_seconds=900)
        parts = urlsplit(url)
        bad_sig = "0" * 64
        tampered = f"/api/v1/files/{bad_sig}?{parts.query}"
        with pytest.raises(SignatureInvalid, match="does not verify"):
            store.verify_signed_url(tampered, now=_EPOCH)

    @pytest.mark.parametrize("bad_sig", ["z" * 64, "f" * 63])
    def test_verify_rejects_malformed_signature_segment(
        self, store: LocalFsStorage, bad_sig: str
    ) -> None:
        content_hash = _sha256(b"hello")
        url = store.sign_url(content_hash, ttl_seconds=900)
        parts = urlsplit(url)
        tampered = f"/api/v1/files/{bad_sig}?{parts.query}"

        with pytest.raises(SignatureInvalid, match="does not verify"):
            store.verify_signed_url(tampered, now=_EPOCH)

    def test_verify_expired_raises_expired(self, store: LocalFsStorage) -> None:
        content_hash = _sha256(b"hello")
        url = store.sign_url(content_hash, ttl_seconds=0)
        # ttl_seconds=0 means exp == now; any later verification is
        # past the expiry. Advance by one second to make the check
        # unambiguous.
        later = _EPOCH + timedelta(seconds=1)
        with pytest.raises(SignatureExpired):
            store.verify_signed_url(url, now=later)

    def test_verify_with_wrong_signing_key_raises_invalid(self, tmp_path: Path) -> None:
        issuer = LocalFsStorage(
            tmp_path,
            signing_key=b"\x11" * 32,
            clock=FrozenClock(_EPOCH),
        )
        verifier = LocalFsStorage(
            tmp_path,
            signing_key=b"\x22" * 32,
            clock=FrozenClock(_EPOCH),
        )
        content_hash = _sha256(b"hello")
        url = issuer.sign_url(content_hash, ttl_seconds=900)
        with pytest.raises(SignatureInvalid, match="does not verify"):
            verifier.verify_signed_url(url, now=_EPOCH)

    def test_verify_accepts_url_signed_before_hmac_rotation(
        self, tmp_path: Path, db_session: Session, settings: Settings
    ) -> None:
        issuer = LocalFsStorage(
            tmp_path,
            signer=HmacSigner(db_session, settings=settings, clock=FrozenClock(_EPOCH)),
            clock=FrozenClock(_EPOCH),
        )
        content_hash = _sha256(b"hello")
        url = issuer.sign_url(content_hash, ttl_seconds=900)

        rotate_hmac_key(
            db_session,
            "storage-sign",
            b"s" * 32,
            purge_after=_EPOCH + timedelta(hours=72),
            settings=settings,
            clock=FrozenClock(_EPOCH + timedelta(seconds=1)),
        )

        verifier = LocalFsStorage(
            tmp_path,
            signer=HmacSigner(
                db_session,
                settings=settings,
                clock=FrozenClock(_EPOCH + timedelta(hours=1)),
            ),
            clock=FrozenClock(_EPOCH + timedelta(hours=1)),
        )
        assert (
            verifier.verify_signed_url(url, now=_EPOCH + timedelta(seconds=899))
            == content_hash
        )

    def test_verify_rejects_url_with_wrong_prefix(self, store: LocalFsStorage) -> None:
        content_hash = _sha256(b"hello")
        url = store.sign_url(content_hash, ttl_seconds=900)
        rerooted = url.replace("/api/v1/files/", "/uploads/")
        with pytest.raises(SignatureInvalid, match="prefix"):
            store.verify_signed_url(rerooted, now=_EPOCH)

    def test_verify_rejects_url_missing_query_params(
        self, store: LocalFsStorage
    ) -> None:
        content_hash = _sha256(b"hello")
        url = store.sign_url(content_hash, ttl_seconds=900)
        # Strip the ``h=`` param entirely.
        path, query = url.split("?", 1)
        reduced_query = "&".join(p for p in query.split("&") if not p.startswith("h="))
        with pytest.raises(SignatureInvalid, match="query param"):
            store.verify_signed_url(f"{path}?{reduced_query}", now=_EPOCH)

    def test_verify_rejects_naive_now(self, store: LocalFsStorage) -> None:
        content_hash = _sha256(b"hello")
        url = store.sign_url(content_hash, ttl_seconds=900)
        with pytest.raises(ValueError, match="aware"):
            store.verify_signed_url(url, now=datetime(2026, 1, 1, 12, 0, 0))  # naive

    def test_verify_rejects_duplicate_hash_param(self, store: LocalFsStorage) -> None:
        # Defense-in-depth against a caller appending a second ``h=``
        # (e.g. cache-busting middleware, proxy rewrite). ``parse_qs``
        # yields a list per key; the verifier must reject anything
        # other than exactly one ``h`` and one ``e``.
        content_hash = _sha256(b"hello")
        url = store.sign_url(content_hash, ttl_seconds=900)
        doubled = f"{url}&h={'0' * 64}"
        with pytest.raises(SignatureInvalid, match="query param"):
            store.verify_signed_url(doubled, now=_EPOCH)

    def test_verify_rejects_duplicate_exp_param(self, store: LocalFsStorage) -> None:
        content_hash = _sha256(b"hello")
        url = store.sign_url(content_hash, ttl_seconds=900)
        doubled = f"{url}&e=99999999999"
        with pytest.raises(SignatureInvalid, match="query param"):
            store.verify_signed_url(doubled, now=_EPOCH)


# ---------------------------------------------------------------------------
# Hardening: path traversal, symlink attacks, tmp pre-existence
# ---------------------------------------------------------------------------


class TestHardening:
    """Defense-in-depth checks the store must hold even if a later
    caller accidentally loosens the hash validator or the uploads
    tree is somehow writable by a second user."""

    @pytest.mark.parametrize(
        "bogus",
        [
            "../" * 10 + "etc/passwd",
            "../../../../etc/passwd",
            "/etc/passwd",
            "a" * 63 + "/",
            "a" * 62 + "/b",
            "\x00" + "a" * 63,
        ],
    )
    def test_put_rejects_path_traversal_shaped_hash(
        self, store: LocalFsStorage, tmp_path: Path, bogus: str
    ) -> None:
        # ``_validate_hash`` enforces exactly 64 lowercase hex; any
        # ``/``, ``.``, NUL, or uppercase byte trips it before a file
        # operation runs. Lock this in with a parametrised test so a
        # future "lenient" change gets caught immediately.
        with pytest.raises(ValueError, match="lowercase hex"):
            store.put(bogus, io.BytesIO(b"nope"))
        # Nothing outside the uploads tree was touched.
        assert not (tmp_path / "uploads").exists()

    @pytest.mark.parametrize("method", ["get", "exists", "delete", "sign_url"])
    def test_readonly_methods_reject_path_traversal_shaped_hash(
        self, store: LocalFsStorage, method: str
    ) -> None:
        bogus = "../../../../etc/passwd"
        with pytest.raises(ValueError, match="lowercase hex"):
            if method == "sign_url":
                store.sign_url(bogus, ttl_seconds=60)
            elif method == "get":
                store.get(bogus)
            elif method == "exists":
                store.exists(bogus)
            elif method == "delete":
                store.delete(bogus)

    def test_put_refuses_symlink_at_target_path(
        self, store: LocalFsStorage, tmp_path: Path
    ) -> None:
        # An attacker pre-creates a symlink at the final blob path
        # pointing at ``/etc/passwd``. ``put`` must refuse loudly
        # rather than (a) follow the symlink on the existence check
        # and try to dedupe against the victim file, or (b) replace
        # it. The refusal happens before any IO against the symlink
        # target.
        payload = b"attacker-victim"
        content_hash = _sha256(payload)
        target = tmp_path / "uploads" / content_hash[:2] / content_hash
        target.parent.mkdir(parents=True, exist_ok=True)
        victim = tmp_path / "victim.txt"
        victim.write_bytes(b"secret")
        target.symlink_to(victim)

        with pytest.raises(ValueError, match="symlink"):
            store.put(content_hash, io.BytesIO(payload))
        # Victim untouched.
        assert victim.read_bytes() == b"secret"
        # Symlink itself still there — ``put`` did not silently
        # replace it. An operator clean-up is explicit.
        assert target.is_symlink()

    def test_put_refuses_symlink_at_tmp_path(
        self, store: LocalFsStorage, tmp_path: Path
    ) -> None:
        # An attacker pre-creates ``<hash>.tmp`` as a symlink into
        # ``/tmp/victim``. Without ``O_NOFOLLOW|O_EXCL`` on the tmp
        # open, :func:`_stream_to_temp` would write the streamed
        # bytes into the victim. With the hardening in place the
        # open raises ``OSError`` (ELOOP or EEXIST) and the caller's
        # ``except BaseException`` unlinks the tmp symlink.
        payload = b"payload bytes"
        content_hash = _sha256(payload)
        tmp_dir = tmp_path / "uploads" / content_hash[:2]
        tmp_dir.mkdir(parents=True, exist_ok=True)
        victim = tmp_path / "victim.txt"
        victim.write_bytes(b"do-not-clobber")
        tmp_symlink = tmp_dir / f"{content_hash}.tmp"
        tmp_symlink.symlink_to(victim)

        with pytest.raises(OSError):
            store.put(content_hash, io.BytesIO(payload))
        # Victim untouched.
        assert victim.read_bytes() == b"do-not-clobber"
        # Final blob path was never created.
        target = tmp_dir / content_hash
        assert not target.exists()

    def test_put_refuses_pre_existing_regular_tmp(
        self, store: LocalFsStorage, tmp_path: Path
    ) -> None:
        # A stale ``<hash>.tmp`` from a crashed write (or an attacker
        # seed) must cause the put to fail under ``O_EXCL`` instead
        # of silently truncating + rewriting it.
        payload = b"fresh"
        content_hash = _sha256(payload)
        tmp_dir = tmp_path / "uploads" / content_hash[:2]
        tmp_dir.mkdir(parents=True, exist_ok=True)
        stale_tmp = tmp_dir / f"{content_hash}.tmp"
        stale_tmp.write_bytes(b"stale")

        with pytest.raises(FileExistsError):
            store.put(content_hash, io.BytesIO(payload))
        # The failing ``put``'s ``except BaseException: unlink`` path
        # clears the stale tmp so a follow-up retry can succeed.
        assert not stale_tmp.exists()
        # No final blob either.
        assert not (tmp_dir / content_hash).exists()

    def test_get_refuses_symlink_at_target(
        self, store: LocalFsStorage, tmp_path: Path
    ) -> None:
        # Even if a symlink somehow ends up at the target, ``get``
        # must not follow it. ``O_NOFOLLOW`` → ``OSError(ELOOP)``.
        content_hash = "a" * 64
        target = tmp_path / "uploads" / content_hash[:2] / content_hash
        target.parent.mkdir(parents=True, exist_ok=True)
        victim = tmp_path / "victim.txt"
        victim.write_bytes(b"secret")
        target.symlink_to(victim)

        with pytest.raises(OSError):
            store.get(content_hash)
