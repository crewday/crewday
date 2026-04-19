"""workspace

Revision ID: f247f50e8bee
Revises: a968bd90fefc
Create Date: 2026-04-19 22:27:17.963314

Creates the two tenancy-anchor tables (see
``docs/specs/02-domain-model.md`` §"workspaces" and §"user_workspace",
``docs/specs/01-architecture.md`` §"Workspace addressing"):

* ``workspace`` — one row per tenant. ``slug`` is globally unique and
  carries the URL label (``<host>/w/<slug>/...``). The table is
  intentionally NOT registered as workspace-scoped: the slug→id
  resolver has to run before any :class:`~app.tenancy.WorkspaceContext`
  exists, so the tenant filter must skip it.

* ``user_workspace`` — derived (user, workspace) membership junction
  populated by the role-grant refresh worker. Registered as
  workspace-scoped in :mod:`app.adapters.db.workspace`. The FK on
  ``workspace_id`` (``ON DELETE CASCADE``) sweeps the junction when a
  workspace is hard-deleted. ``user_id`` is a soft reference until
  cd-w92 lands ``users``.

The v1 slice ships only the columns downstream tasks need today
(``id``, ``slug``, ``name``, ``plan``, ``quota_json``, ``created_at``,
``owner_onboarded_at``); the richer §02 surface (``verification_state``,
``signup_ip``, ``default_language`` / ``_currency`` / ``_country`` /
``_locale``, ``settings_json``, …) lands in cd-n6p / cd-055 follow-ups
without breaking this migration's public write contract.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f247f50e8bee"
down_revision: str | Sequence[str] | None = "a968bd90fefc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "workspace",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("plan", sa.String(), nullable=False),
        sa.Column("quota_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_onboarded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "plan IN ('free', 'pro', 'enterprise', 'unlimited')",
            name=op.f("ck_workspace_plan"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace")),
        sa.UniqueConstraint("slug", name=op.f("uq_workspace_slug")),
    )
    op.create_table(
        "user_workspace",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('workspace_grant', 'property_grant', 'org_grant', "
            "'work_engagement')",
            name=op.f("ck_user_workspace_source"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_user_workspace_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "workspace_id", name=op.f("pk_user_workspace")
        ),
    )
    with op.batch_alter_table("user_workspace", schema=None) as batch_op:
        batch_op.create_index(
            "ix_user_workspace_workspace", ["workspace_id"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("user_workspace", schema=None) as batch_op:
        batch_op.drop_index("ix_user_workspace_workspace")

    op.drop_table("user_workspace")
    op.drop_table("workspace")
