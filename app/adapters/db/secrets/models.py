"""``secret_envelope`` SQLAlchemy model.

Implements the §15 "Secret envelope" / §02 "secret_envelope" surface:
one row per persisted secret, AES-256-GCM ciphertext + nonce + the
8-byte fingerprint of the root key in force at encrypt time. Pairs
with :mod:`app.adapters.storage.envelope` (the cipher seam) and
:mod:`app.domain.secrets.ports` (the read / write Protocol).

**Not workspace-scoped.** Rows reference owner entities that may be
workspace-scoped (``ical_feed``, ``property``, ...) or deployment-
wide (``smtp_password``, ``openrouter_api_key``, ...). The rotation
worker (cd-rotate-root-key) walks every row regardless of tenant, so
forcing a workspace-id filter on the table itself would block the
deployment-wide rotation path. Same posture as
:class:`~app.adapters.db.identity.models.User`.

**Why ``LargeBinary`` for ``ciphertext`` / ``nonce`` / ``key_fp``.**
SQLite stores ``BLOB`` round-trippable bytes; Postgres stores
``BYTEA``. SQLAlchemy's :class:`LargeBinary` projects to both. We do
**not** carry a length cap on ``ciphertext`` because the body width is
``len(plaintext) + 16`` (AES-GCM tag), and plaintext sizes are
caller-bounded (e.g. iCal URLs cap at 2048 bytes). ``nonce`` is fixed
at 12 bytes by the cipher and ``key_fp`` at 8 bytes by §15 — but we
don't pin column-level length checks because Alembic's batch-rebuild
on SQLite would reproject ``LargeBinary`` to ``BLOB`` and lose the
length anyway. The cipher enforces both lengths at write time.

See ``docs/specs/02-domain-model.md`` §"secret_envelope" and
``docs/specs/15-security-privacy.md`` §"Secret envelope".
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    LargeBinary,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = [
    "RootKeySlot",
    "SecretEnvelope",
]


class RootKeySlot(Base):
    """Pointer to active and recently retired deployment root keys.

    The row stores only metadata and a ``key_ref`` pointer such as
    ``env:CREWDAY_ROOT_KEY`` or ``file:/run/secrets/crewday-root-key``.
    Key material never lives in this table; the cipher resolves the
    referenced value only when it needs to open a legacy envelope row.
    """

    __tablename__ = "root_key_slot"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    key_fp: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    key_ref: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    purge_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_root_key_slot_key_fp", "key_fp"),
        Index("ix_root_key_slot_is_active", "is_active"),
        Index("ix_root_key_slot_purge_after", "purge_after"),
    )


class SecretEnvelope(Base):
    """One persisted AES-256-GCM envelope row.

    Columns mirror §02 "secret_envelope" verbatim:

    * ``id`` — ULID PK; the value the cipher's pointer-tagged
      ciphertext (``0x02 || envelope_id``) carries.
    * ``owner_entity_kind`` / ``owner_entity_id`` — free-form pointer
      into whichever table actually owns the secret. The repo / cipher
      do not validate the FK target — different owner kinds live in
      different tables (and some don't exist as ORM rows at all,
      e.g. deployment-wide settings carried in env / files), so a
      hard FK is impossible.
    * ``purpose`` — short slug (``ical-feed-url``, ``smtp-password``,
      ``openrouter-key``, ...). Folded into HKDF-Expand at the
      cipher layer so different purposes produce unrelated key
      streams. Stored here for audit / rotation-trail readability.
    * ``ciphertext`` — AES-GCM ciphertext + 16-byte tag. Opaque
      bytes; never read except through the cipher.
    * ``nonce`` — 12-byte AES-GCM nonce. Fresh per row; never reused
      with the same key. The cipher enforces uniqueness via
      :func:`os.urandom`.
    * ``key_fp`` — 8-byte SHA-256 prefix of the root key in force
      at encrypt time. The §15 "Root key compromise playbook"
      keys off this column: the rotation worker filters legacy
      rows on ``WHERE key_fp = <old_fp>`` and re-encrypts each one
      under the active key.
    * ``created_at`` — first encrypt timestamp. Aware UTC.
    * ``rotated_at`` — set when the rotation worker re-encrypts the
      row under a new key. NULL until the first rotation hits this
      row. Today rotation is not yet implemented; the column lands
      now so cd-rotate-root-key can stamp it without a follow-up
      migration.

    **Index.** ``ix_secret_envelope_key_fp`` carries the rotation
    worker's hot path (``SELECT ... WHERE key_fp = <old_fp>`` —
    walk every row still encrypted under the old key). Without it
    rotation degrades to a full table scan on every progress report.
    """

    __tablename__ = "secret_envelope"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_entity_kind: Mapped[str] = mapped_column(String, nullable=False)
    owner_entity_id: Mapped[str] = mapped_column(String, nullable=False)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # 8-byte SHA-256 prefix of the root key. Width is enforced at the
    # cipher layer (the pointer-tagged encrypt path slices to 8 bytes
    # before insert); the column itself is plain ``LargeBinary`` so
    # SQLite's batch-rebuild doesn't try to reproject a length-pinned
    # type that the dialect can't represent.
    key_fp: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Hot path for the rotation worker — see class docstring.
        Index("ix_secret_envelope_key_fp", "key_fp"),
        # Owner-scoped lookup (e.g. "every secret owned by this
        # property" once a per-owner sweep / cleanup lands). Composite
        # ``(kind, id)`` because the kind narrows first to a single
        # table-equivalent.
        Index(
            "ix_secret_envelope_owner",
            "owner_entity_kind",
            "owner_entity_id",
        ),
    )
