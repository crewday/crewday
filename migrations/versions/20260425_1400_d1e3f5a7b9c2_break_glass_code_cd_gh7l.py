"""break_glass_code cd-gh7l

Revision ID: d1e3f5a7b9c2
Revises: c0d2e4f6a8b1
Create Date: 2026-04-25 14:00:00.000000

Adds the ``break_glass_code`` table — the per-user step-up credential
ledger for §03 "Break-glass codes" + "Self-service lost-device
recovery" (cd-gh7l). Each row holds one argon2id-hashed recovery code;
the redemption flow inside :func:`app.auth.recovery.request_recovery`
verifies a submitted plaintext against an unused row, burns it
(``used_at = now()``), and stamps ``consumed_magic_link_id`` with the
freshly-minted magic link's jti once the matching link is issued.

The table is workspace-scoped at *management* time (the workspace
bootstrap ritual + the future ``/me/security`` re-mint UI both run
under a live :class:`WorkspaceContext`), but **tenant-agnostic at
redemption time**: recovery executes before any workspace ctx is
resolved. The recovery helper opts out of the ORM tenant filter via
:func:`app.tenancy.tenant_agnostic` — same pattern the invite-accept
flow uses on the bare host.

**``workspace_id`` is informational, not a redemption gate.** The
column reflects "the workspace the code was issued under" so the UI
can render "issued by Workspace X" on the code listing; the
redemption path looks up unused rows by ``user_id`` only.

**Indexes.**

* ``ix_break_glass_code_workspace_user`` — composite on
  ``(workspace_id, user_id)`` for the workspace-scoped management
  surfaces (count active codes in a workspace, list codes for a
  user, etc.).
* ``ix_break_glass_code_user_unused`` — partial on ``user_id`` where
  ``used_at IS NULL``. Hot path for the redemption flow: "find every
  unused code for this user". Restricting to unused rows keeps the
  index tiny — burnt rows are forensic baggage, not lookup keys.
  Dialect kwargs match the cd-wchi / cd-4saj idiom.

See ``docs/specs/03-auth-and-tokens.md`` §"Break-glass codes" /
§"Self-service lost-device recovery" and
``docs/specs/15-security-privacy.md`` §"Step-up bypass is not a
fallback".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1e3f5a7b9c2"
down_revision: str | Sequence[str] | None = "c0d2e4f6a8b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "break_glass_code",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("hash", sa.String(), nullable=False),
        sa.Column("hash_params", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_magic_link_id", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_break_glass_code_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_break_glass_code_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_break_glass_code")),
    )
    with op.batch_alter_table("break_glass_code", schema=None) as batch_op:
        batch_op.create_index(
            "ix_break_glass_code_workspace_user",
            ["workspace_id", "user_id"],
            unique=False,
        )

    # Partial index emitted at the top level (not inside the batch
    # context) because dialect-specific ``WHERE`` kwargs do not
    # survive a batch rebuild cleanly — same idiom as
    # ``uq_role_grant_deployment_user_role`` in cd-wchi.
    op.create_index(
        "ix_break_glass_code_user_unused",
        "break_glass_code",
        ["user_id"],
        unique=False,
        sqlite_where=sa.text("used_at IS NULL"),
        postgresql_where=sa.text("used_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Drop the partial index first — its dialect-specific WHERE
    # doesn't survive a batch rebuild.
    op.drop_index("ix_break_glass_code_user_unused", table_name="break_glass_code")

    with op.batch_alter_table("break_glass_code", schema=None) as batch_op:
        batch_op.drop_index("ix_break_glass_code_workspace_user")

    op.drop_table("break_glass_code")
