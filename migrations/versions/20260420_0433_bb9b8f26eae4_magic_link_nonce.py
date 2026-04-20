"""magic_link_nonce

Revision ID: bb9b8f26eae4
Revises: dbf10d5d2f11
Create Date: 2026-04-20 04:33:00.000000

Adds the ``magic_link_nonce`` table — the single-use ledger for every
magic-link emission (``POST /auth/magic/request``) and consumption
(``POST /auth/magic/consume``). See ``docs/specs/03-auth-and-tokens.md``
§"Magic link format" and cd-4zz.

Each row represents one pending (``consumed_at IS NULL``) or consumed
(``consumed_at`` set) link. Single-use is enforced by a conditional
``UPDATE … WHERE consumed_at IS NULL`` in
:func:`app.auth.magic_link.consume_link` — SQLite's transaction
serialisation and Postgres' row-level locks both guarantee exactly one
concurrent consumer sees ``rowcount = 1``; the loser gets
``409 already_consumed``.

**Privacy (§15).** ``created_ip_hash`` and ``created_email_hash`` are
SHA-256 digests of the source IP / canonicalised email salted with a
deployment-scoped HKDF subkey of ``settings.root_key`` (see
:mod:`app.auth.keys`). The plaintext values never hit disk, but the
hashes are stable enough to drive rate-limiting correlations without
becoming a PII leak surface on backup disclosure.

**Tenant-agnostic.** No ``workspace_id`` column — magic links span
pre-workspace (``signup_verify``) and post-workspace (``recover_passkey``,
``email_change_confirm``, ``grant_invite``) purposes. Followed the
same pattern as ``user`` / ``webauthn_challenge``; the domain service
wraps every read/write in :func:`app.tenancy.tenant_agnostic`.

No foreign keys: ``subject_id`` points into different spaces per
``purpose`` (``user.id``, a future ``signup_session.id``, or an
``invite.id``) — forcing one FK would rule out the other flows. The
domain layer validates the reference per-purpose.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bb9b8f26eae4"
down_revision: str | Sequence[str] | None = "dbf10d5d2f11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "magic_link_nonce",
        sa.Column("jti", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_ip_hash", sa.String(), nullable=False),
        sa.Column("created_email_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("jti", name=op.f("pk_magic_link_nonce")),
    )
    with op.batch_alter_table("magic_link_nonce", schema=None) as batch_op:
        batch_op.create_index(
            "ix_magic_link_nonce_email_hash", ["created_email_hash"], unique=False
        )
        batch_op.create_index(
            "ix_magic_link_nonce_expires", ["expires_at"], unique=False
        )
        batch_op.create_index(
            "ix_magic_link_nonce_ip_hash", ["created_ip_hash"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("magic_link_nonce", schema=None) as batch_op:
        batch_op.drop_index("ix_magic_link_nonce_ip_hash")
        batch_op.drop_index("ix_magic_link_nonce_expires")
        batch_op.drop_index("ix_magic_link_nonce_email_hash")

    op.drop_table("magic_link_nonce")
