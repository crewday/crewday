"""Envelope encryption for small secrets stored at rest.

The cipher implements the §15 "Secret envelope" surface in two
modes, distinguished by the wire-format version byte:

* ``0x01`` (inline) — the ciphertext is ``version || nonce ||
  AESGCM(ciphertext_with_tag)`` and lives directly in the owner's
  column. The cd-1ai cipher v1 wrote every secret in this mode;
  the §15 ``key_fp`` machinery is **not** stamped on inline blobs
  because there is no row to carry it.
* ``0x02`` (row-backed, cd-znv4) — the ciphertext is a tiny
  pointer-tagged blob: ``version || envelope_id_utf8``. The body,
  nonce, and ``key_fp`` live in a :class:`SecretEnvelope` row
  resolved through :class:`~app.domain.secrets.ports.SecretEnvelopeRepository`.

Mode selection is automatic on encrypt: if the cipher was
constructed with a ``repository`` it writes a row and returns the
pointer; otherwise it falls back to the inline format. Decrypt
branches on the version byte, so legacy ``0x01`` ciphertexts stay
openable forever even after every fresh write moves to ``0x02``.

The 32-byte AES-256-GCM key is derived from
:attr:`app.config.Settings.root_key` via
:func:`app.auth.keys.derive_subkey` with a caller-supplied
``purpose`` label (HKDF-Expand's ``info`` parameter — different
purposes produce unrelated key material).

``EnvelopeEncryptor`` is the **port** other layers consume.
Production code wires :class:`Aes256GcmEnvelope`; tests substitute
``InMemoryEnvelope`` (see ``tests/_fakes/envelope.py``) which is
a structural match with no key material.

**Threat model.** This helper defends against "attacker walks away
with the DB backup". The in-process root key is still plaintext in
memory while the service is running — that's inherent to any
online encryption. A DB-only exfiltration gets ciphertext + nonces;
without the root key there's no path to plaintext, and §15's
``key_fp`` machinery makes a wrong-key restore fail loudly rather
than silently corrupt rows.

See ``docs/specs/15-security-privacy.md`` §"Secret envelope" /
§"Key fingerprint" / §"Root key compromise playbook" and
``docs/specs/02-domain-model.md`` §"secret_envelope".
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC
from typing import Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from app.adapters.storage.ports import (
    EnvelopeDecryptError,
    EnvelopeEncryptor,
    EnvelopeOwner,
    KeyFingerprintMismatch,
)
from app.auth.keys import derive_subkey
from app.domain.secrets.ports import (
    EnvelopeNotFound,
    SecretEnvelopeRepository,
)
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "Aes256GcmEnvelope",
    "EnvelopeDecryptError",
    "EnvelopeEncryptor",
    "EnvelopeOwner",
    "KeyFingerprintMismatch",
    "compute_key_fingerprint",
]


# Pinned AES-GCM nonce length in bytes (96 bits — the NIST-SP-800-38D
# recommended default and what ``AESGCM.generate_nonce()`` defaults
# to). Kept explicit so the on-the-wire format is independent of
# upstream defaults if those ever shift.
_NONCE_LEN: Final[int] = 12
# Wire-format version bytes.
#
# * ``0x01`` — inline AESGCM (``version || nonce || ct_with_tag``);
#   the cd-1ai v1 layout. No ``key_fp`` is stamped because there is
#   no row to carry it.
# * ``0x02`` — pointer-tagged row reference; the body is
#   ``version || envelope_id_utf8``. The full ciphertext, nonce,
#   and ``key_fp`` live in the corresponding ``secret_envelope`` row.
_VERSION_INLINE: Final[int] = 0x01
_VERSION_ROW: Final[int] = 0x02
# §15 "Key fingerprint": first 8 bytes of SHA-256(root_key).
_FP_LEN: Final[int] = 8
# Hard cap on the row-backed pointer body. The pointer is
# ``0x02 || envelope_id_utf8`` and the id is a 26-char ULID, so the
# total wire length is exactly 27 bytes. We cap at 64 to leave a
# generous margin for any future id-shape change while still failing
# fast on a multi-MB malformed pointer (a corrupted owner column
# could otherwise drag a giant string through the repository call
# before the missing-row branch fires).
_ROW_POINTER_BODY_MAX_LEN: Final[int] = 64


def compute_key_fingerprint(root_key: SecretStr) -> bytes:
    """Return the 8-byte fingerprint §15 stamps on every row.

    ``key_fp = SHA-256(root_key_bytes)[:8]``. Matches the
    cryptographic definition the rotation worker
    (``crewday admin rotate-root-key``) and the restore-with-wrong-
    key guard (``crewday admin restore``) consult; pinning the
    derivation in one helper keeps every consumer aligned without
    re-deriving the truncation rule.

    Empty / unset root keys raise :class:`ValueError` rather than
    returning a SHA of empty bytes — an empty fingerprint would
    silently match every row encrypted under another empty key,
    which is meaningless and dangerous to leave in the wire format.
    """
    raw = root_key.get_secret_value().encode("utf-8")
    if not raw:
        raise ValueError(
            "cannot compute key fingerprint for an empty root key; "
            "set CREWDAY_ROOT_KEY first"
        )
    return hashlib.sha256(raw).digest()[:_FP_LEN]


class Aes256GcmEnvelope:
    """AES-256-GCM envelope backed by HKDF-derived subkeys.

    Two modes:

    * **Inline** (cd-1ai v1) — constructed with just a root key.
      ``encrypt`` returns ``0x01 || nonce || ct_with_tag`` for the
      caller to embed in the owner's column.
    * **Row-backed** (cd-znv4) — constructed with a root key + a
      :class:`SecretEnvelopeRepository`. ``encrypt`` writes a row,
      stamping the active key's 8-byte fingerprint on the row, and
      returns a tiny pointer-tagged blob ``0x02 || envelope_id_utf8``
      for the caller to embed in the owner's column.

    Decrypt branches on the version byte, so legacy ``0x01``
    ciphertexts stay openable forever — the back-compat contract the
    cd-1ai docstring promised.

    The constructor takes the root key as a :class:`pydantic.SecretStr`
    so no plaintext ever lands in ``repr`` or default serialisation.
    The derived 32-byte subkey is held on the stack inside
    :meth:`encrypt` / :meth:`decrypt` — we don't cache an
    :class:`AESGCM` instance since that would keep the key material
    pinned on the heap for longer than necessary.
    """

    __slots__ = ("_clock", "_repository", "_root_key")

    def __init__(
        self,
        root_key: SecretStr,
        *,
        repository: SecretEnvelopeRepository | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Bind the encryptor to ``root_key``.

        ``repository`` is optional — when ``None`` the cipher stays
        in inline ``0x01`` mode (back-compat for callers that still
        pass plain ciphertext through their owner column). When
        wired, every fresh ``encrypt`` writes a row and returns the
        pointer-tagged blob.

        ``clock`` is the source for the ``created_at`` stamp on
        row-backed encrypts. Defaults to :class:`SystemClock`; tests
        (and the rotation worker, when it lands) inject a frozen
        clock to make the ``created_at`` deterministic.

        Re-uses :func:`app.auth.keys.derive_subkey`'s validation — a
        ``None`` / empty root key raises :class:`KeyDerivationError`
        at encrypt / decrypt time (not here — the authoritative
        failure point is where the subkey is actually needed).
        """
        self._root_key = root_key
        self._repository = repository
        self._clock = clock if clock is not None else SystemClock()

    def encrypt(
        self,
        plaintext: bytes,
        *,
        purpose: str,
        owner: object | None = None,
    ) -> bytes:
        """Return an opaque ciphertext blob.

        **Inline mode** (no repository wired): returns ``0x01 ||
        nonce || AESGCM(key, nonce, plaintext)``.

        **Row-backed mode** (repository wired): persists a fresh row
        with the AES-GCM ciphertext + nonce + 8-byte key fingerprint
        + the ``(kind, id)`` owner pointer, and returns ``0x02 ||
        envelope_id_utf8`` for the caller to embed in the owner's
        column.

        ``owner`` is required in row-backed mode and ignored in
        inline mode. Passing ``owner=None`` to a row-backed cipher
        raises :class:`ValueError` — the row's ``owner_entity_kind``
        / ``owner_entity_id`` are NOT NULL columns and a row
        without provenance defeats the rotation-worker scope helper.
        Typed as :class:`object` to match the
        :class:`~app.adapters.storage.ports.EnvelopeEncryptor`
        Protocol; narrowed to :class:`EnvelopeOwner` on the
        row-backed branch.

        ``purpose`` is folded into the HKDF expand step as the
        ``info`` parameter. Every call generates a fresh random
        nonce via :func:`os.urandom` — never re-use a nonce with
        the same key (GCM's security reduction collapses
        catastrophically on nonce re-use).
        """
        purpose_label = _purpose_label(purpose)

        # Validate the row-backed contract before paying for AES-GCM —
        # the failure path stays cheap and any logged error from a
        # mis-wired caller is not preceded by a wasted nonce + cipher
        # call.
        if self._repository is not None:
            if owner is None:
                raise ValueError(
                    "row-backed Aes256GcmEnvelope.encrypt requires an `owner` "
                    "argument; the row's owner_entity_* columns are NOT NULL"
                )
            if not isinstance(owner, EnvelopeOwner):
                raise TypeError(
                    "row-backed Aes256GcmEnvelope.encrypt expects an "
                    f"EnvelopeOwner; got {type(owner).__name__}"
                )

        key = derive_subkey(self._root_key, purpose=purpose_label)
        aead = AESGCM(key)
        nonce = os.urandom(_NONCE_LEN)
        ct = aead.encrypt(nonce, plaintext, None)

        if self._repository is None:
            return bytes((_VERSION_INLINE,)) + nonce + ct

        # Mypy: ``owner`` is narrowed to ``EnvelopeOwner`` by the
        # isinstance check above; the assertion documents that intent
        # without an extra cast.
        assert isinstance(owner, EnvelopeOwner)

        envelope_id = new_ulid()
        key_fp = compute_key_fingerprint(self._root_key)
        created_at = self._clock.now().astimezone(UTC)
        self._repository.insert(
            envelope_id=envelope_id,
            owner_entity_kind=owner.kind,
            owner_entity_id=owner.id,
            purpose=purpose,
            ciphertext=ct,
            nonce=nonce,
            key_fp=key_fp,
            created_at=created_at,
        )
        return bytes((_VERSION_ROW,)) + envelope_id.encode("utf-8")

    def decrypt(self, ciphertext: bytes, *, purpose: str) -> bytes:
        """Inverse of :meth:`encrypt`.

        Branches on the leading version byte:

        * ``0x01`` — open the inline body in place. Legacy
          back-compat path — never raises
          :class:`KeyFingerprintMismatch` because inline blobs do
          not carry a fingerprint.
        * ``0x02`` — read the trailing ULID, fetch the row through
          the repository, check the row's ``key_fp`` against the
          active key's fingerprint, and open the AES-GCM body.

        Fails with :class:`EnvelopeDecryptError` (or its narrower
        :class:`KeyFingerprintMismatch` subclass) on shape /
        version / tag / fingerprint / pointer-resolution failure.
        The authentication tag is part of the ciphertext body — GCM
        binds plaintext to ciphertext, so a flipped bit anywhere in
        the body surfaces as a tag-mismatch rather than silent
        corruption.
        """
        if not ciphertext:
            raise EnvelopeDecryptError("ciphertext is empty; cannot decrypt")
        version = ciphertext[0]
        if version == _VERSION_INLINE:
            return self._decrypt_inline(ciphertext, purpose=purpose)
        if version == _VERSION_ROW:
            return self._decrypt_row_backed(ciphertext, purpose=purpose)
        raise EnvelopeDecryptError(
            f"unknown envelope version {version!r}; "
            f"expected {_VERSION_INLINE!r} (inline) or {_VERSION_ROW!r} (row-backed)"
        )

    # -- Internal mode-specific helpers --------------------------------

    def _decrypt_inline(self, ciphertext: bytes, *, purpose: str) -> bytes:
        """Open a legacy ``0x01`` inline ciphertext (cd-1ai layout)."""
        if len(ciphertext) < 1 + _NONCE_LEN + 16:
            # 16 = AES-GCM tag length. A shorter blob can't possibly
            # be a valid ciphertext; raise before we call into the
            # primitive so the error message is specific.
            raise EnvelopeDecryptError(
                "ciphertext too short to be a valid AES-GCM envelope"
            )
        nonce = ciphertext[1 : 1 + _NONCE_LEN]
        body = ciphertext[1 + _NONCE_LEN :]
        key = derive_subkey(self._root_key, purpose=_purpose_label(purpose))
        aead = AESGCM(key)
        try:
            return aead.decrypt(nonce, body, None)
        except Exception as exc:  # cryptography raises InvalidTag etc.
            raise EnvelopeDecryptError(
                "envelope decryption failed; ciphertext is not valid "
                "under the current root key for the given purpose"
            ) from exc

    def _decrypt_row_backed(self, ciphertext: bytes, *, purpose: str) -> bytes:
        """Open a ``0x02`` pointer-tagged row-backed ciphertext."""
        if self._repository is None:
            # The caller asked us to open a row-backed blob but never
            # wired the repository. Fail loudly — silently falling
            # through to "unknown version" would mask the wiring bug.
            raise EnvelopeDecryptError(
                "row-backed envelope decrypt requires a SecretEnvelopeRepository; "
                "the cipher was constructed without one"
            )
        envelope_id_bytes = ciphertext[1:]
        if not envelope_id_bytes:
            raise EnvelopeDecryptError("row-backed ciphertext carries no envelope id")
        if len(envelope_id_bytes) > _ROW_POINTER_BODY_MAX_LEN:
            # Malformed pointer (corrupted owner column? wrong-version
            # blob smuggled in?); fail before passing a multi-MB string
            # down to the repository's primary-key lookup.
            raise EnvelopeDecryptError(
                f"row-backed envelope id is too long "
                f"({len(envelope_id_bytes)} bytes; max {_ROW_POINTER_BODY_MAX_LEN})"
            )
        try:
            envelope_id = envelope_id_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EnvelopeDecryptError(
                "row-backed envelope id is not valid UTF-8"
            ) from exc

        try:
            row = self._repository.get_by_id(envelope_id=envelope_id)
        except EnvelopeNotFound as exc:
            # Defensive: most repos return ``None`` rather than raise,
            # but the Protocol allows either shape — collapse both
            # surfaces onto the same caller-visible error.
            raise EnvelopeDecryptError(
                f"row-backed envelope {envelope_id!r} not found in repository"
            ) from exc
        if row is None:
            raise EnvelopeDecryptError(
                f"row-backed envelope {envelope_id!r} not found in repository"
            )

        # §15 "Key fingerprint": open rows under the active key when the
        # fingerprint matches. During rotation, a retired root_key_slot may
        # resolve the legacy key for rows the re-encryption worker has not
        # rewritten yet.
        active_fp = compute_key_fingerprint(self._root_key)
        root_key = self._root_key
        if row.key_fp != active_fp:
            legacy_key = self._repository.legacy_root_key_for_fp(key_fp=row.key_fp)
            if legacy_key is None:
                raise KeyFingerprintMismatch(expected=row.key_fp, actual=active_fp)
            root_key = legacy_key

        key = derive_subkey(root_key, purpose=_purpose_label(purpose))
        aead = AESGCM(key)
        try:
            return aead.decrypt(row.nonce, row.ciphertext, None)
        except Exception as exc:  # cryptography raises InvalidTag etc.
            raise EnvelopeDecryptError(
                "envelope decryption failed; ciphertext is not valid "
                "under the current root key for the given purpose"
            ) from exc


def _purpose_label(purpose: str) -> str:
    """Namespace a raw ``purpose`` under the envelope seam.

    Prefixes with ``"storage.envelope."`` so a collision with an
    auth-layer subkey label (``"magic-link"``, ``"session-cookie"``)
    is structurally impossible. HKDF's expand step already makes
    the namespaces disjoint byte-for-byte, but the explicit prefix
    keeps the audit story readable when operators scan subkey
    labels in code.
    """
    if not purpose or not purpose.strip():
        raise ValueError("envelope purpose must be a non-blank label")
    return f"storage.envelope.{purpose}"
