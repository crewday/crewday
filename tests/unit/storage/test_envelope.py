"""Unit tests for :mod:`app.adapters.storage.envelope`.

Covers both modes of the AES-256-GCM envelope:

* **Inline (cd-1ai, ``0x01``)** — round-trip; wire-format version
  byte; purpose binding; tamper / version / truncation rejection;
  empty root key derivation error; per-purpose key separation.
* **Row-backed (cd-znv4, ``0x02``)** — round-trip; pointer-tagged
  blob shape; row persisted with the §15 ``key_fp``;
  :class:`KeyFingerprintMismatch` on a key swap; missing-row
  resolution error; mixed-version decrypt (one cipher opens both
  inline and row-backed blobs); ``owner=None`` rejected on the
  row-backed branch.

The row-backed tests use an in-memory fake repository so the
cipher-level behaviour is exercised without dragging the SQL
adapter into a unit test. The SA concretion is covered by the
sibling :mod:`tests.unit.adapters.db.secrets` adapter tests.

See ``docs/specs/15-security-privacy.md`` §"Secret envelope" /
§"Key fingerprint" / §"Root key compromise playbook".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from app.adapters.storage.envelope import (
    Aes256GcmEnvelope,
    compute_key_fingerprint,
)
from app.adapters.storage.ports import (
    EnvelopeDecryptError,
    EnvelopeOwner,
    EnvelopeOwnerMismatch,
    KeyFingerprintMismatch,
)
from app.auth.keys import KeyDerivationError
from app.domain.secrets.ports import SecretEnvelopeRow

_KEY = SecretStr("x" * 32)
_OTHER_KEY = SecretStr("y" * 32)
_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# In-memory repository fake
# ---------------------------------------------------------------------------


class _InMemoryEnvelopeRepository:
    """Structural :class:`SecretEnvelopeRepository` fake.

    Keeps rows in a dict keyed by ``id``. ``insert`` / ``get_by_id``
    mirror the Protocol contract; the rest of the production seam
    (rotation walks, owner-scoped sweeps) lives outside this fake's
    surface — it's deliberately the smallest thing the cipher's
    encrypt + decrypt branches exercise.
    """

    def __init__(self) -> None:
        self.rows: dict[str, SecretEnvelopeRow] = {}
        self.legacy_keys: dict[bytes, SecretStr] = {}

    def insert(
        self,
        *,
        envelope_id: str,
        owner_entity_kind: str,
        owner_entity_id: str,
        purpose: str,
        ciphertext: bytes,
        nonce: bytes,
        key_fp: bytes,
        created_at: datetime,
    ) -> SecretEnvelopeRow:
        row = SecretEnvelopeRow(
            id=envelope_id,
            owner_entity_kind=owner_entity_kind,
            owner_entity_id=owner_entity_id,
            purpose=purpose,
            ciphertext=ciphertext,
            nonce=nonce,
            key_fp=key_fp,
            created_at=created_at,
            rotated_at=None,
        )
        self.rows[envelope_id] = row
        return row

    def get_by_id(self, *, envelope_id: str) -> SecretEnvelopeRow | None:
        return self.rows.get(envelope_id)

    def legacy_root_key_for_fp(self, *, key_fp: bytes) -> SecretStr | None:
        return self.legacy_keys.get(key_fp)


# ---------------------------------------------------------------------------
# Inline mode (cd-1ai, version byte 0x01)
# ---------------------------------------------------------------------------


class TestInlineMode:
    """Round-trip + negative paths for the legacy inline format."""

    def test_round_trip(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        plaintext = b"https://www.airbnb.com/ical/secret-token.ics"
        ciphertext = env.encrypt(plaintext, purpose="ical-feed-url")
        # Ciphertext is opaque — never contains the plaintext.
        assert plaintext not in ciphertext
        # First byte is the version marker.
        assert ciphertext[0] == 0x01
        # Decrypt recovers the original exactly.
        assert env.decrypt(ciphertext, purpose="ical-feed-url") == plaintext

    def test_fresh_nonce_per_call(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        plaintext = b"same-input"
        ct1 = env.encrypt(plaintext, purpose="p")
        ct2 = env.encrypt(plaintext, purpose="p")
        assert ct1 != ct2

    def test_purpose_mismatch_rejected(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        ciphertext = env.encrypt(b"secret", purpose="purpose-a")
        with pytest.raises(EnvelopeDecryptError):
            env.decrypt(ciphertext, purpose="purpose-b")

    def test_blank_purpose_rejected(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        with pytest.raises(ValueError, match="non-blank"):
            env.encrypt(b"secret", purpose="")
        with pytest.raises(ValueError, match="non-blank"):
            env.encrypt(b"secret", purpose="   ")

    def test_tampered_ciphertext_rejected(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        ciphertext = env.encrypt(b"secret", purpose="p")
        tampered = bytearray(ciphertext)
        tampered[20] ^= 0x01
        with pytest.raises(EnvelopeDecryptError):
            env.decrypt(bytes(tampered), purpose="p")

    def test_unknown_version_byte_rejected(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        ciphertext = env.encrypt(b"secret", purpose="p")
        # Both ``0x01`` (inline) and ``0x02`` (row-backed) are
        # defined; pick a byte outside both branches so the
        # unknown-version guard fires.
        mutated = b"\x03" + ciphertext[1:]
        with pytest.raises(EnvelopeDecryptError, match="unknown envelope version"):
            env.decrypt(mutated, purpose="p")

    def test_truncated_ciphertext_rejected(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        with pytest.raises(EnvelopeDecryptError, match="too short"):
            env.decrypt(b"\x01short", purpose="p")

    def test_empty_ciphertext_rejected(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        with pytest.raises(EnvelopeDecryptError, match="empty"):
            env.decrypt(b"", purpose="p")

    def test_empty_root_key_raises_derivation_error(self) -> None:
        env = Aes256GcmEnvelope(SecretStr(""))
        with pytest.raises(KeyDerivationError):
            env.encrypt(b"secret", purpose="p")

    def test_different_purposes_produce_different_ciphertext(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        ct_a = env.encrypt(b"same", purpose="a")
        ct_b = env.encrypt(b"same", purpose="b")
        assert ct_a != ct_b

    def test_expected_owner_ignored_for_legacy_inline_blob(self) -> None:
        env = Aes256GcmEnvelope(_KEY)
        ciphertext = env.encrypt(b"legacy", purpose="p")

        assert (
            env.decrypt(
                ciphertext,
                purpose="p",
                expected_owner=EnvelopeOwner(kind="ical_feed", id="feed-1"),
            )
            == b"legacy"
        )


# ---------------------------------------------------------------------------
# Row-backed mode (cd-znv4, version byte 0x02)
# ---------------------------------------------------------------------------


class _FrozenClock:
    """Deterministic clock used by row-backed encrypt tests."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class TestRowBackedMode:
    """Round-trip + persistence + key-fingerprint behaviour."""

    def test_round_trip_persists_row_and_returns_pointer(self) -> None:
        repo = _InMemoryEnvelopeRepository()
        env = Aes256GcmEnvelope(_KEY, repository=repo, clock=_FrozenClock(_PINNED))
        owner = EnvelopeOwner(kind="ical_feed", id="feed-001")
        plaintext = b"https://www.airbnb.com/ical/secret-token.ics"

        ciphertext = env.encrypt(plaintext, purpose="ical-feed-url", owner=owner)

        # Pointer-tagged shape: ``0x02 || envelope_id_utf8``.
        assert ciphertext[0] == 0x02
        envelope_id = ciphertext[1:].decode("utf-8")
        # Plaintext is never in the wire format.
        assert plaintext not in ciphertext
        # The row landed.
        assert envelope_id in repo.rows
        row = repo.rows[envelope_id]
        assert row.owner_entity_kind == "ical_feed"
        assert row.owner_entity_id == "feed-001"
        assert row.purpose == "ical-feed-url"
        # ``key_fp`` is the §15 8-byte SHA-256 prefix of the root key.
        assert row.key_fp == compute_key_fingerprint(_KEY)
        assert len(row.key_fp) == 8
        assert row.created_at == _PINNED
        assert row.rotated_at is None

        # Decrypt recovers the original.
        assert (
            env.decrypt(
                ciphertext,
                purpose="ical-feed-url",
                expected_owner=owner,
            )
            == plaintext
        )

    def test_expected_owner_mismatch_rejected(self) -> None:
        repo = _InMemoryEnvelopeRepository()
        env = Aes256GcmEnvelope(_KEY, repository=repo, clock=_FrozenClock(_PINNED))
        owner = EnvelopeOwner(kind="ical_feed", id="feed-a")
        ciphertext = env.encrypt(b"secret", purpose="p", owner=owner)

        with pytest.raises(EnvelopeOwnerMismatch) as exc_info:
            env.decrypt(
                ciphertext,
                purpose="p",
                expected_owner=EnvelopeOwner(kind="ical_feed", id="feed-b"),
            )

        assert exc_info.value.expected == EnvelopeOwner(kind="ical_feed", id="feed-b")
        assert exc_info.value.actual_kind == "ical_feed"
        assert exc_info.value.actual_id == "feed-a"
        assert isinstance(exc_info.value, EnvelopeDecryptError)
        assert "feed-a" not in str(exc_info.value)
        assert "feed-b" not in str(exc_info.value)

    def test_fresh_nonce_per_call(self) -> None:
        """Two row-backed encrypts of the same plaintext land different rows."""
        repo = _InMemoryEnvelopeRepository()
        env = Aes256GcmEnvelope(_KEY, repository=repo, clock=_FrozenClock(_PINNED))
        owner = EnvelopeOwner(kind="ical_feed", id="feed-x")
        ct1 = env.encrypt(b"same", purpose="p", owner=owner)
        ct2 = env.encrypt(b"same", purpose="p", owner=owner)
        # Pointer differs (fresh ULID per row).
        assert ct1 != ct2
        # Two rows persisted.
        assert len(repo.rows) == 2
        # Their ciphertexts + nonces are independent.
        rows = list(repo.rows.values())
        assert rows[0].ciphertext != rows[1].ciphertext
        assert rows[0].nonce != rows[1].nonce

    def test_purpose_mismatch_rejected(self) -> None:
        repo = _InMemoryEnvelopeRepository()
        env = Aes256GcmEnvelope(_KEY, repository=repo, clock=_FrozenClock(_PINNED))
        owner = EnvelopeOwner(kind="prop", id="p1")
        ciphertext = env.encrypt(b"secret", purpose="a", owner=owner)
        with pytest.raises(EnvelopeDecryptError):
            env.decrypt(ciphertext, purpose="b")

    def test_owner_required_in_row_backed_mode(self) -> None:
        repo = _InMemoryEnvelopeRepository()
        env = Aes256GcmEnvelope(_KEY, repository=repo, clock=_FrozenClock(_PINNED))
        with pytest.raises(ValueError, match="requires an `owner`"):
            env.encrypt(b"secret", purpose="p", owner=None)

    def test_owner_must_be_envelope_owner(self) -> None:
        repo = _InMemoryEnvelopeRepository()
        env = Aes256GcmEnvelope(_KEY, repository=repo, clock=_FrozenClock(_PINNED))
        # A bare tuple isn't an :class:`EnvelopeOwner` — reject loudly
        # so a wrong-shape arg can't sneak through into the row's
        # ``owner_entity_*`` columns.
        with pytest.raises(TypeError, match="EnvelopeOwner"):
            env.encrypt(b"secret", purpose="p", owner=("ical_feed", "f1"))

    def test_blank_owner_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-blank slug"):
            EnvelopeOwner(kind="", id="x")
        with pytest.raises(ValueError, match="non-blank slug"):
            EnvelopeOwner(kind="   ", id="x")

    def test_blank_owner_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-blank value"):
            EnvelopeOwner(kind="ical_feed", id="")
        with pytest.raises(ValueError, match="non-blank value"):
            EnvelopeOwner(kind="ical_feed", id="   ")

    def test_key_fingerprint_mismatch_raises_on_decrypt(self) -> None:
        """A row encrypted under key A cannot decrypt under key B."""
        repo = _InMemoryEnvelopeRepository()
        encrypt_env = Aes256GcmEnvelope(
            _KEY, repository=repo, clock=_FrozenClock(_PINNED)
        )
        owner = EnvelopeOwner(kind="ical_feed", id="feed-key-swap")
        ciphertext = encrypt_env.encrypt(b"secret", purpose="p", owner=owner)

        # Operator restored the wrong key — same DB, different
        # CREWDAY_ROOT_KEY. The §15 actionable shape names both
        # fingerprints.
        decrypt_env = Aes256GcmEnvelope(
            _OTHER_KEY, repository=repo, clock=_FrozenClock(_PINNED)
        )
        with pytest.raises(KeyFingerprintMismatch) as exc_info:
            decrypt_env.decrypt(ciphertext, purpose="p")

        assert exc_info.value.expected == compute_key_fingerprint(_KEY)
        assert exc_info.value.actual == compute_key_fingerprint(_OTHER_KEY)
        # Message carries both fingerprints.
        message = str(exc_info.value)
        assert compute_key_fingerprint(_KEY).hex() in message
        assert compute_key_fingerprint(_OTHER_KEY).hex() in message
        assert "Restore the correct key or re-encrypt." in message
        # KeyFingerprintMismatch is a subclass of EnvelopeDecryptError —
        # callers that ``except EnvelopeDecryptError`` still catch it.
        assert isinstance(exc_info.value, EnvelopeDecryptError)

    def test_matching_legacy_key_slot_opens_retired_row(self) -> None:
        """A retired-key row stays open while rotation re-encryption catches up."""
        repo = _InMemoryEnvelopeRepository()
        encrypt_env = Aes256GcmEnvelope(
            _KEY, repository=repo, clock=_FrozenClock(_PINNED)
        )
        owner = EnvelopeOwner(kind="ical_feed", id="feed-legacy-slot")
        ciphertext = encrypt_env.encrypt(b"secret", purpose="p", owner=owner)
        repo.legacy_keys[compute_key_fingerprint(_KEY)] = _KEY

        decrypt_env = Aes256GcmEnvelope(
            _OTHER_KEY, repository=repo, clock=_FrozenClock(_PINNED)
        )

        assert decrypt_env.decrypt(ciphertext, purpose="p") == b"secret"

    def test_missing_row_raises_decrypt_error(self) -> None:
        """A pointer-tagged blob whose row is gone fails loudly."""
        repo = _InMemoryEnvelopeRepository()
        env = Aes256GcmEnvelope(_KEY, repository=repo, clock=_FrozenClock(_PINNED))
        # Synthesise a pointer whose target row never existed.
        bogus = b"\x02" + b"01HW000000000000000000FAKE1"
        with pytest.raises(EnvelopeDecryptError, match="not found"):
            env.decrypt(bogus, purpose="p")

    def test_row_backed_decrypt_without_repository_fails(self) -> None:
        """A pointer-tagged blob handed to an inline-only cipher errors."""
        # Build a row-backed ciphertext.
        repo = _InMemoryEnvelopeRepository()
        env_with_repo = Aes256GcmEnvelope(
            _KEY, repository=repo, clock=_FrozenClock(_PINNED)
        )
        owner = EnvelopeOwner(kind="prop", id="p1")
        ciphertext = env_with_repo.encrypt(b"secret", purpose="p", owner=owner)

        # Inline-only cipher (legacy callsite) cannot resolve the
        # pointer; surface as a single EnvelopeDecryptError.
        env_no_repo = Aes256GcmEnvelope(_KEY)
        with pytest.raises(
            EnvelopeDecryptError, match="requires a SecretEnvelopeRepository"
        ):
            env_no_repo.decrypt(ciphertext, purpose="p")

    def test_row_backed_pointer_truncated_to_version_byte_only(self) -> None:
        repo = _InMemoryEnvelopeRepository()
        env = Aes256GcmEnvelope(_KEY, repository=repo, clock=_FrozenClock(_PINNED))
        with pytest.raises(EnvelopeDecryptError, match="no envelope id"):
            env.decrypt(b"\x02", purpose="p")

    def test_row_backed_pointer_too_long_rejected(self) -> None:
        """A malformed pointer (e.g. corrupted column) fails before DB hit.

        A real pointer is ``0x02 || ULID`` (27 bytes). A multi-MB blob
        masquerading as a row-backed pointer should be rejected at the
        cipher boundary so the repository never sees a giant string —
        defends against accidental data corruption and (toy) DoS via
        malformed owner columns.
        """
        repo = _InMemoryEnvelopeRepository()
        env = Aes256GcmEnvelope(_KEY, repository=repo, clock=_FrozenClock(_PINNED))
        # 1 KiB of garbage after the version byte — way past the 64-byte cap.
        bogus = b"\x02" + b"x" * 1024
        with pytest.raises(EnvelopeDecryptError, match="too long"):
            env.decrypt(bogus, purpose="p")

    def test_empty_root_key_fingerprint_rejected(self) -> None:
        """An empty root key has no meaningful fingerprint."""
        with pytest.raises(ValueError, match="empty root key"):
            compute_key_fingerprint(SecretStr(""))


# ---------------------------------------------------------------------------
# Mixed-version (back-compat)
# ---------------------------------------------------------------------------


class TestMixedVersionDecrypt:
    """Spec §15 "Forward compatibility": legacy 0x01 + new 0x02 coexist."""

    def test_one_cipher_opens_both_versions(self) -> None:
        """A row-backed cipher decrypts a legacy inline blob."""
        repo = _InMemoryEnvelopeRepository()
        # Encrypt one secret under the legacy inline path (no repo).
        legacy_env = Aes256GcmEnvelope(_KEY)
        legacy_blob = legacy_env.encrypt(b"legacy-url", purpose="ical-feed-url")

        # Encrypt another under the row-backed path.
        new_env = Aes256GcmEnvelope(_KEY, repository=repo, clock=_FrozenClock(_PINNED))
        owner = EnvelopeOwner(kind="ical_feed", id="feed-1")
        new_blob = new_env.encrypt(b"fresh-url", purpose="ical-feed-url", owner=owner)

        # The row-backed cipher opens both — legacy and fresh — under
        # the same key, the spec's "forward compatibility" contract.
        assert new_env.decrypt(legacy_blob, purpose="ical-feed-url") == b"legacy-url"
        assert new_env.decrypt(new_blob, purpose="ical-feed-url") == b"fresh-url"


# ---------------------------------------------------------------------------
# compute_key_fingerprint
# ---------------------------------------------------------------------------


class TestComputeKeyFingerprint:
    """The §15 ``key_fp`` derivation: ``SHA-256(root_key)[:8]``."""

    def test_returns_eight_bytes(self) -> None:
        fp = compute_key_fingerprint(_KEY)
        assert isinstance(fp, bytes)
        assert len(fp) == 8

    def test_deterministic(self) -> None:
        assert compute_key_fingerprint(_KEY) == compute_key_fingerprint(_KEY)

    def test_distinct_for_distinct_keys(self) -> None:
        assert compute_key_fingerprint(_KEY) != compute_key_fingerprint(_OTHER_KEY)
