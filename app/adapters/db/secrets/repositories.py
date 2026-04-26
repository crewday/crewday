"""SA-backed concretion of :class:`SecretEnvelopeRepository`.

Pairs with :mod:`app.adapters.db.secrets.models` (the ORM) and
:mod:`app.domain.secrets.ports` (the Protocol). Consumed by
:class:`app.adapters.storage.envelope.Aes256GcmEnvelope` in its
row-backed mode (cd-znv4) and, in a follow-up,
``crewday admin rotate-root-key --reencrypt`` to walk legacy rows.

The repo carries an open :class:`~sqlalchemy.orm.Session` and never
commits — the caller's UoW owns the transaction boundary (§01 "Key
runtime invariants" #3). :meth:`insert` flushes so the cipher's
own follow-up read (and the audit writer's FK reference, if any)
sees the new row.

The ``secret_envelope`` table is **not** workspace-scoped; the rows
themselves cross deployment + workspace boundaries. The repo wraps
its reads / writes in :func:`app.tenancy.tenant_agnostic` so a
workspace-scoped caller (e.g. the iCal feed registration path
running inside a :class:`~app.tenancy.WorkspaceContext`) does not
trip the ORM tenant filter on a row that has no ``workspace_id``
column at all. The wrapping is centralised here so the cipher does
not have to litter its callsites with the context manager.

See ``docs/specs/02-domain-model.md`` §"secret_envelope" and
``docs/specs/15-security-privacy.md`` §"Secret envelope".
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.adapters.db.secrets.models import SecretEnvelope
from app.domain.secrets.ports import (
    SecretEnvelopeRepository,
    SecretEnvelopeRow,
)
from app.tenancy import tenant_agnostic

__all__ = [
    "SqlAlchemySecretEnvelopeRepository",
]


def _to_row(row: SecretEnvelope) -> SecretEnvelopeRow:
    """Project an ORM :class:`SecretEnvelope` into the seam-level row.

    Field-by-field copy. The ``LargeBinary`` columns round-trip as
    :class:`bytes` on both SQLite (BLOB) and Postgres (BYTEA);
    SQLAlchemy normalises the dialect-side types for us.
    ``created_at`` / ``rotated_at`` carry tzinfo — re-attached to UTC
    on the SQLite path where the dialect strips the marker, mirroring
    the same guard the cd-24im email-change adapter uses.
    """
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    rotated_at = row.rotated_at
    if rotated_at is not None and rotated_at.tzinfo is None:
        rotated_at = rotated_at.replace(tzinfo=UTC)
    return SecretEnvelopeRow(
        id=row.id,
        owner_entity_kind=row.owner_entity_kind,
        owner_entity_id=row.owner_entity_id,
        purpose=row.purpose,
        ciphertext=bytes(row.ciphertext),
        nonce=bytes(row.nonce),
        key_fp=bytes(row.key_fp),
        created_at=created_at,
        rotated_at=rotated_at,
    )


class SqlAlchemySecretEnvelopeRepository(SecretEnvelopeRepository):
    """SA-backed concretion of :class:`SecretEnvelopeRepository`.

    Constructor takes an open SQLAlchemy session; methods read /
    write through it without committing. The cipher (or rotation
    worker) wraps the surrounding UoW.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

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
        row = SecretEnvelope(
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
        # justification: secret_envelope has no workspace_id column;
        # rotation walks every row regardless of tenant.
        with tenant_agnostic():
            self._session.add(row)
            self._session.flush()
        return _to_row(row)

    def get_by_id(self, *, envelope_id: str) -> SecretEnvelopeRow | None:
        # justification: secret_envelope has no workspace_id column;
        # decrypt resolves pointer-tagged blobs deployment-wide.
        with tenant_agnostic():
            row = self._session.get(SecretEnvelope, envelope_id)
        if row is None:
            return None
        return _to_row(row)
