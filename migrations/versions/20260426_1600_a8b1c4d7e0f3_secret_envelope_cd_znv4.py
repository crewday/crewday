"""secret_envelope cd-znv4

Revision ID: a8b1c4d7e0f3
Revises: f7a9b1c3d5e6
Create Date: 2026-04-26 16:00:00.000000

Lands the §15 ``secret_envelope`` table — the row-backed storage half
of the AES-256-GCM seam in :mod:`app.adapters.storage.envelope`. Until
this revision the cipher persisted ciphertext **inline** in the
owner's column (e.g. ``ical_feed.url``). Inline mode stays alive for
back-compat (every legacy row decrypts under the version-byte ``0x01``
branch), but every new encrypt under cd-znv4 lands a row here and the
caller stores a tiny pointer-tagged blob (``0x02 || envelope_id``).

Shape mirrors §02 "secret_envelope" verbatim:

* ``id`` — ULID PK.
* ``owner_entity_kind`` / ``owner_entity_id`` — free-form pointer
  into whichever table owns the secret. No FK because different
  owner kinds live in different tables (and some owners are not ORM
  rows at all — e.g. deployment-wide secrets carried in env / files).
* ``purpose`` — short slug folded into HKDF-Expand at the cipher
  layer; stored here for audit / rotation-trail readability.
* ``ciphertext`` — AES-GCM ciphertext + 16-byte tag.
* ``nonce`` — 12-byte AES-GCM nonce.
* ``key_fp`` — 8-byte SHA-256 prefix of the root key in force at
  encrypt time. The §15 "Root key compromise playbook" keys off
  this column: the rotation worker filters legacy rows on
  ``WHERE key_fp = <old_fp>``.
* ``created_at`` — first encrypt timestamp.
* ``rotated_at`` — set when the rotation worker re-encrypts under a
  new key. NULL until rotation hits the row. Today rotation is not
  yet implemented; the column lands now so cd-rotate-root-key can
  stamp it without a follow-up migration.

**Not workspace-scoped.** Rows reference owner entities that may be
workspace-scoped (``ical_feed``, ``property``, ...) or deployment-
wide (``smtp_password``, ``openrouter_api_key``, ...). The rotation
worker walks every row regardless of tenant. Skipping the
:func:`app.tenancy.registry.register` call is intentional — same
posture as ``user`` / ``magic_link_nonce`` / ``webauthn_challenge``.

**Indexes.**

* ``ix_secret_envelope_key_fp`` — rotation worker hot path
  (``WHERE key_fp = <old_fp>`` walk every row still encrypted under
  the old key). Without it rotation degrades to a full table scan
  on every progress report.
* ``ix_secret_envelope_owner`` — composite ``(owner_entity_kind,
  owner_entity_id)`` for per-owner sweeps once a delete-cascade
  cleanup helper lands.

**Reversibility.** ``downgrade()`` drops the indexes + the table.
Any rows it carried are lost on rollback; an operator running a
real rollback should make sure no live ``ical_feed.url`` (or
sibling owner column) carries a ``0x02`` pointer-tagged blob first
— the cipher cannot decrypt those once the table is gone.

See ``docs/specs/02-domain-model.md`` §"secret_envelope" and
``docs/specs/15-security-privacy.md`` §"Secret envelope".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8b1c4d7e0f3"
down_revision: str | Sequence[str] | None = "f7a9b1c3d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "secret_envelope",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_entity_kind", sa.String(), nullable=False),
        sa.Column("owner_entity_id", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_fp", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_secret_envelope")),
    )
    with op.batch_alter_table("secret_envelope", schema=None) as batch_op:
        batch_op.create_index(
            "ix_secret_envelope_key_fp",
            ["key_fp"],
            unique=False,
        )
        batch_op.create_index(
            "ix_secret_envelope_owner",
            ["owner_entity_kind", "owner_entity_id"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the indexes + the table. Any rows it carried are lost; an
    operator running a real rollback should drain ``0x02`` pointer-
    tagged blobs out of every owner column first — the cipher cannot
    decrypt those once the table is gone. Inline ``0x01`` ciphertexts
    remain decryptable.
    """
    with op.batch_alter_table("secret_envelope", schema=None) as batch_op:
        batch_op.drop_index("ix_secret_envelope_owner")
        batch_op.drop_index("ix_secret_envelope_key_fp")

    op.drop_table("secret_envelope")
