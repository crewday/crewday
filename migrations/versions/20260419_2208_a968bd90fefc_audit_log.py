"""audit_log

Revision ID: a968bd90fefc
Revises: 6cb7f0a6f8a3
Create Date: 2026-04-19 22:08:41.323513

Creates the append-only per-workspace mutation log consumed by
:mod:`app.audit` (see ``docs/specs/02-domain-model.md`` §"audit_log",
``docs/specs/15-security-privacy.md`` §"Audit log", and
``docs/specs/01-architecture.md`` §"Key runtime invariants" #3).
Every domain mutation writes one row in the same transaction.

The table is workspace-scoped: :mod:`app.adapters.db.audit` registers
``audit_log`` with :mod:`app.tenancy.registry`, so every SELECT /
UPDATE / DELETE is auto-filtered by ``workspace_id``. INSERTs bypass
the filter (SQLAlchemy event hook only rewrites reads + writes with
a FROM target); the writer is the single producer and sets
``workspace_id`` from :class:`~app.tenancy.WorkspaceContext`.

No foreign keys (soft refs per §02 preamble): ``workspace_id`` /
``actor_id`` / ``entity_id`` are ULID strings pointing at rows that
may be archived; enforcing FKs would force every audit emission to
resolve a live ``users`` / ``workspace`` row.

Two composite indexes cover the expected read shapes:

* ``ix_audit_log_workspace_created`` — per-workspace timeline feed
  (list newest-first, used by the manager Audit page and the
  ``audit_verify`` worker walk).
* ``ix_audit_log_workspace_entity`` — one entity's full history
  (task drawer, expense drawer, etc.).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a968bd90fefc"
down_revision: str | Sequence[str] | None = "6cb7f0a6f8a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("actor_kind", sa.String(), nullable=False),
        sa.Column("actor_grant_role", sa.String(), nullable=False),
        sa.Column("actor_was_owner_member", sa.Boolean(), nullable=False),
        sa.Column("entity_kind", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("diff", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.create_index(
            "ix_audit_log_workspace_created",
            ["workspace_id", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_audit_log_workspace_entity",
            ["workspace_id", "entity_kind", "entity_id"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_index("ix_audit_log_workspace_entity")
        batch_op.drop_index("ix_audit_log_workspace_created")
    op.drop_table("audit_log")
