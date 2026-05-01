"""passkey_credential aaguid cd-8mf3

Revision ID: b1c3d5e7f902
Revises: a0b2c4d6e8f1
Create Date: 2026-05-01 09:00:00.000000

Adds the ``aaguid`` column to ``passkey_credential``. Spec §03
"Privacy" whitelists the AAGUID (an authenticator-model / vendor id,
not a per-user fingerprint) as one of the seven fields we may store
about a WebAuthn credential. The original cd-w92 identity migration
omitted the column even though :class:`PasskeyCredentialRef` and the
``passkey.registered`` audit diff already surfaced it; cd-8mf3 closes
that gap so new credentials persist the value py_webauthn returns
on :class:`VerifiedRegistration`.

The column is **nullable** by design: rows enrolled before this
revision have no AAGUID (we never observed it at write time), and
the spec does not require a backfill — the field is descriptive
metadata, not a security gate. New credentials always carry a value
because :func:`app.auth.passkey._insert_passkey_and_audit` reads it
straight off the verified attestation.

**Reversibility.** ``downgrade`` drops the column — values are lost
on downgrade, which is acceptable for a forward-only metadata field.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1c3d5e7f902"
down_revision: str | Sequence[str] | None = "a0b2c4d6e8f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``aaguid`` column to ``passkey_credential``."""
    with op.batch_alter_table("passkey_credential", schema=None) as batch_op:
        batch_op.add_column(sa.Column("aaguid", sa.String(length=36), nullable=True))


def downgrade() -> None:
    """Drop the ``aaguid`` column."""
    with op.batch_alter_table("passkey_credential", schema=None) as batch_op:
        batch_op.drop_column("aaguid")
