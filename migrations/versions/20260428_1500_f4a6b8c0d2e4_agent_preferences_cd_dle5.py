"""agent_preferences_cd_dle5

Revision ID: f4a6b8c0d2e4
Revises: e3f5a7b9c1d3
Create Date: 2026-04-28 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a6b8c0d2e4"
down_revision: str | Sequence[str] | None = "e3f5a7b9c1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "agent_approval_mode",
                sa.String(),
                nullable=False,
                server_default="strict",
            )
        )
        batch_op.create_check_constraint(
            "user_agent_approval_mode",
            "agent_approval_mode IN ('bypass', 'auto', 'strict')",
        )

    op.create_table(
        "agent_preference",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("scope_kind", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("body_md", sa.String(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked_actions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "default_approval_mode",
            sa.String(),
            nullable=False,
            server_default="auto",
        ),
        sa.Column("updated_by_user_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scope_kind IN ('workspace', 'property', 'user')",
            name=op.f("ck_agent_preference_agent_preference_scope_kind"),
        ),
        sa.CheckConstraint(
            "default_approval_mode IN ('bypass', 'auto', 'strict')",
            name=op.f("ck_agent_preference_agent_preference_default_approval_mode"),
        ),
        sa.CheckConstraint(
            "token_count >= 0",
            name=op.f("ck_agent_preference_agent_preference_token_count"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["user.id"],
            name=op.f("fk_agent_preference_updated_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_agent_preference_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_preference")),
        sa.UniqueConstraint(
            "workspace_id",
            "scope_kind",
            "scope_id",
            name="uq_agent_preference_workspace_scope",
        ),
    )
    with op.batch_alter_table("agent_preference", schema=None) as batch_op:
        batch_op.create_index(
            "ix_agent_preference_workspace_scope",
            ["workspace_id", "scope_kind", "scope_id"],
            unique=False,
        )

    op.create_table(
        "agent_preference_revision",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("preference_id", sa.String(), nullable=False),
        sa.Column("body_md", sa.String(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("blocked_actions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("default_approval_mode", sa.String(), nullable=False),
        sa.Column("updated_by_user_id", sa.String(), nullable=True),
        sa.Column("change_note", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "token_count >= 0",
            name=op.f("ck_agent_preference_revision_agent_preference_revision_tokens"),
        ),
        sa.ForeignKeyConstraint(
            ["preference_id"],
            ["agent_preference.id"],
            name=op.f("fk_agent_preference_revision_preference_id_agent_preference"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["user.id"],
            name=op.f("fk_agent_preference_revision_updated_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_agent_preference_revision_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_preference_revision")),
    )
    with op.batch_alter_table("agent_preference_revision", schema=None) as batch_op:
        batch_op.create_index(
            "ix_agent_preference_revision_preference_created",
            ["preference_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("agent_preference_revision", schema=None) as batch_op:
        batch_op.drop_index("ix_agent_preference_revision_preference_created")
    op.drop_table("agent_preference_revision")

    with op.batch_alter_table("agent_preference", schema=None) as batch_op:
        batch_op.drop_index("ix_agent_preference_workspace_scope")
    op.drop_table("agent_preference")

    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_constraint("user_agent_approval_mode", type_="check")
        batch_op.drop_column("agent_approval_mode")
