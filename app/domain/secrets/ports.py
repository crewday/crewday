"""Repository Protocol for the §15 ``secret_envelope`` row.

The persisted envelope row is the storage half of the §15 "Secret
envelope" surface — paired with the encryption seam in
:mod:`app.adapters.storage.envelope`. Two readers / writers consume
this Protocol today:

* :class:`app.adapters.storage.envelope.Aes256GcmEnvelope` in its
  row-backed mode (cd-znv4): every encrypt persists a row and
  returns a tiny pointer-tagged blob; every decrypt resolves the
  pointer back to the row before opening the AES-GCM body.
* The future ``crewday admin rotate-root-key --reencrypt`` worker
  (see §15 "Root key compromise playbook") walks rows by
  ``key_fp`` and rewrites each one under the new key.

The Protocol is deliberately narrow — only the operations the cipher
+ rotation worker need. Listing / search / per-owner enumeration land
once a real consumer needs them.

Spec: ``docs/specs/02-domain-model.md`` §"secret_envelope",
``docs/specs/15-security-privacy.md`` §"Secret envelope" /
§"Root key compromise playbook",
``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 (the
repository Protocol lives on the domain side).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import SecretStr

__all__ = [
    "EnvelopeNotFound",
    "SecretEnvelopeRepository",
    "SecretEnvelopeRow",
]


class EnvelopeNotFound(LookupError):
    """The pointer-tagged blob references a row that does not exist.

    Raised by :meth:`SecretEnvelopeRepository.get_by_id` when the row
    behind a ``0x02`` pointer-tagged ciphertext has been swept (legacy
    rotation cleanup, manual deletion) or never existed. The cipher
    layer surfaces this as
    :class:`~app.adapters.storage.ports.EnvelopeDecryptError` with an
    actionable message — callers see a single failure mode regardless
    of whether the body bytes were corrupted, the version is unknown,
    the row has been swept, or the key fingerprint mismatches.
    """


@dataclass(frozen=True, slots=True)
class SecretEnvelopeRow:
    """Immutable projection of a ``secret_envelope`` row.

    Mirrors :class:`app.adapters.db.secrets.models.SecretEnvelope`
    column-for-column. Declared on the seam so the SA adapter
    projects ORM rows into a domain-owned shape without forcing the
    cipher layer (or the future rotation worker) to import the ORM
    class.

    ``owner_entity_kind`` / ``owner_entity_id`` are free-form pointers
    into whichever table actually owns the secret (``ical_feed``,
    ``property``, ``workspace_setting``, ...). Validation lives on the
    *caller* — the envelope row is a passive sink.

    ``ciphertext`` is the AES-GCM ciphertext-with-tag body; ``nonce``
    is the 12-byte AES-GCM nonce (the version byte is implicit:
    ``0x02`` for every row-backed envelope, the only version this
    table holds).

    ``key_fp`` is the 8-byte SHA-256 prefix of the root key in force
    at encrypt time. The rotation worker filters on this column; the
    cipher's decrypt path checks it against the active key's
    fingerprint and raises
    :class:`~app.adapters.storage.ports.KeyFingerprintMismatch` on a
    mismatch.

    ``rotated_at`` stays ``None`` until the rotation worker
    re-encrypts the row under a new key — the column is informational
    today; rotation lands with cd-rotate-root-key.
    """

    id: str
    owner_entity_kind: str
    owner_entity_id: str
    purpose: str
    ciphertext: bytes
    nonce: bytes
    key_fp: bytes
    created_at: datetime
    rotated_at: datetime | None


class SecretEnvelopeRepository(Protocol):
    """Read + write seam for the ``secret_envelope`` table.

    Concrete implementation:
    :class:`app.adapters.db.secrets.repositories.SqlAlchemySecretEnvelopeRepository`.
    Tests can wire an in-memory fake — the Protocol is structural so
    no ``runtime_checkable`` annotation is needed.

    The repo never commits — the caller's UoW owns the transaction
    boundary (§01 "Key runtime invariants" #3). :meth:`insert` flushes
    so the caller's own follow-up read (and the audit writer's FK
    reference, if applicable) sees the new row.

    The ``secret_envelope`` table is **not** workspace-scoped:
    individual rows reference owner entities that may themselves be
    workspace-scoped (``ical_feed``, ``property``, ...) or deployment-
    wide (``deployment_setting`` once SMTP / OpenRouter rotation
    lands). The row itself stays tenant-agnostic so the rotation
    worker (which runs deployment-wide) does not need to walk every
    workspace ctx.
    """

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
        """Insert a fresh envelope row and return its projection.

        Flushes so a peer read in the same UoW sees the new row.
        """
        ...

    def get_by_id(self, *, envelope_id: str) -> SecretEnvelopeRow | None:
        """Return the row for ``envelope_id`` or ``None`` when absent.

        Returning ``None`` rather than raising lets the caller (the
        cipher's decrypt branch) surface the failure with the same
        :class:`~app.adapters.storage.ports.EnvelopeDecryptError`
        envelope it uses for tag / version mismatches — a single
        failure mode for every "this ciphertext is not openable
        right now" reason.
        """
        ...

    def legacy_root_key_for_fp(self, *, key_fp: bytes) -> SecretStr | None:
        """Return the retired root key for ``key_fp`` when a slot resolves.

        Row-backed decrypt uses this during root-key rotation: a row stamped
        with a retired fingerprint can still open while the re-encryption
        worker catches up. ``None`` means no usable legacy slot is available.
        """
        ...
