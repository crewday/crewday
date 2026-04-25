"""email_change_pending cd-601a

Revision ID: c5e7f9b1d3a4
Revises: b4d6e8fac3d4
Create Date: 2026-04-25 00:00:00.000000

Adds the ``email_change_pending`` table — the per-request ledger for
the §03 "Self-service email change" flow (cd-601a). Each row pairs a
magic-link nonce (``email_change_confirm`` purpose at request time;
``email_change_revert`` purpose at verify time) with the plaintext
addresses the confirm + revert routes need to swap ``users.email``
and revert it.

The table is tenant-agnostic — email is the identity anchor and a
swap applies globally across every workspace the user belongs to —
matching the :class:`User`, :class:`Session`, :class:`MagicLinkNonce`
shape (no ``workspace_id`` column).

**PII minimisation (§15).** Plaintext addresses live in
``previous_email`` / ``new_email`` for the duration of the magic-link
TTL (15 min for confirm + 72 h for revert). Audit rows continue to
carry ``email_hash`` only — the plaintext never reaches the
``audit_log`` table. A future sweeper hard-deletes rows whose revert
TTL has lapsed; until that lands, rows accumulate slowly (~one per
intentional change) and the operator can prune manually.

**No FK on ``request_jti`` / ``revert_jti``** — the magic-link nonce
rows can be swept by the GC while this row still needs to outlive
them (the revert TTL extends 72 h past the confirm consume). A hard
FK would force a cascade delete that would discard
``previous_email`` mid-revert-window.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service email change",
``docs/specs/12-rest-api.md`` §"Auth", and
``docs/specs/15-security-privacy.md`` §"Self-service lost-device &
email-change abuse mitigations".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5e7f9b1d3a4"
down_revision: str | Sequence[str] | None = "b4d6e8fac3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "email_change_pending",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("request_jti", sa.String(), nullable=False),
        sa.Column("revert_jti", sa.String(), nullable=True),
        sa.Column("previous_email", sa.String(), nullable=False),
        sa.Column("previous_email_lower", sa.String(), nullable=False),
        sa.Column("new_email", sa.String(), nullable=False),
        sa.Column("new_email_lower", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revert_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reverted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_email_change_pending_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_change_pending")),
        sa.UniqueConstraint(
            "request_jti",
            name=op.f("uq_email_change_pending_request_jti"),
        ),
        sa.UniqueConstraint(
            "revert_jti",
            name=op.f("uq_email_change_pending_revert_jti"),
        ),
    )
    with op.batch_alter_table("email_change_pending", schema=None) as batch_op:
        batch_op.create_index("ix_email_change_pending_user", ["user_id"], unique=False)
        batch_op.create_index(
            "ix_email_change_pending_request_jti", ["request_jti"], unique=False
        )
        batch_op.create_index(
            "ix_email_change_pending_revert_jti", ["revert_jti"], unique=False
        )
        batch_op.create_index(
            "ix_email_change_pending_revert_expires",
            ["revert_expires_at"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("email_change_pending", schema=None) as batch_op:
        batch_op.drop_index("ix_email_change_pending_revert_expires")
        batch_op.drop_index("ix_email_change_pending_revert_jti")
        batch_op.drop_index("ix_email_change_pending_request_jti")
        batch_op.drop_index("ix_email_change_pending_user")

    op.drop_table("email_change_pending")
