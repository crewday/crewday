"""signup_attempt

Revision ID: eee32ef09c5d
Revises: bb9b8f26eae4
Create Date: 2026-04-20 05:17:00.000000

Adds the ``signup_attempt`` table — one row per ``POST /signup/start``
request, carried through magic-link verify + passkey enrolment to the
workspace-creation commit. See
``docs/specs/03-auth-and-tokens.md`` §"Self-serve signup" and cd-3i5.

**PII minimisation (§15).** The row stores ``email_lower`` (the
case-folded lookup form) and ``email_hash`` / ``ip_hash`` (SHA-256
digests salted with the deployment's HKDF subkey). Plaintext email is
needed at complete time to seed the ``user`` row; plaintext IP never
hits disk — only the hash correlates abuse across rows without
becoming a PII sink.

**Unique (`email_lower`, `desired_slug`).** Idempotent re-requests
within the 15-minute TTL land on the same row. Different desired
slugs for the same email each get their own row — the upstream guards
(``slug_taken`` / homoglyph) fire before the second insert would
collide on ``workspace.slug``.

**Tenant-agnostic.** No ``workspace_id`` column — the row precedes
the workspace's existence. The domain service wraps reads / writes in
:func:`app.tenancy.tenant_agnostic`, matching the ``user`` /
``magic_link_nonce`` / ``webauthn_challenge`` pattern.

No foreign keys: ``workspace_id`` (populated on completion) is a soft
reference; the signup row lives in the identity layer while the
workspace is workspace-scoped, so a hard FK would drag the row across
the tenancy seam. ``signup_gc`` and the admin audit-reader both treat
it as a best-effort pointer.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "eee32ef09c5d"
down_revision: str | Sequence[str] | None = "bb9b8f26eae4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "signup_attempt",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("email_lower", sa.String(), nullable=False),
        sa.Column("email_hash", sa.String(), nullable=False),
        sa.Column("desired_slug", sa.String(), nullable=False),
        sa.Column("ip_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("workspace_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signup_attempt")),
        sa.UniqueConstraint(
            "email_lower",
            "desired_slug",
            name="uq_signup_attempt_email_slug",
        ),
    )
    with op.batch_alter_table("signup_attempt", schema=None) as batch_op:
        batch_op.create_index(
            "ix_signup_attempt_email_hash", ["email_hash"], unique=False
        )
        batch_op.create_index("ix_signup_attempt_expires", ["expires_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("signup_attempt", schema=None) as batch_op:
        batch_op.drop_index("ix_signup_attempt_expires")
        batch_op.drop_index("ix_signup_attempt_email_hash")

    op.drop_table("signup_attempt")
