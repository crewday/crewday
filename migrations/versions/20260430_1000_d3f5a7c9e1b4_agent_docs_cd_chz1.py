"""agent_docs_cd_chz1

Revision ID: d3f5a7c9e1b4
Revises: c2e4f6a8d0b2
Create Date: 2026-04-30 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3f5a7c9e1b4"
down_revision: str | Sequence[str] | None = "c2e4f6a8d0b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "agent_doc",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("summary", sa.String(), nullable=True),
        sa.Column("body_md", sa.String(), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("default_hash", sa.String(length=16), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_agent_doc_agent_doc_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_doc")),
    )
    with op.batch_alter_table("agent_doc", schema=None) as batch_op:
        batch_op.create_index(
            "uq_agent_doc_active_slug",
            ["slug"],
            unique=True,
            sqlite_where=sa.text("is_active = 1"),
            postgresql_where=sa.text("is_active = true"),
        )

    op.create_table(
        "agent_doc_revision",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("doc_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body_md", sa.String(), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.String(), nullable=True),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_agent_doc_revision_agent_doc_revision_version"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_agent_doc_revision_created_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["doc_id"],
            ["agent_doc.id"],
            name=op.f("fk_agent_doc_revision_doc_id_agent_doc"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_doc_revision")),
        sa.UniqueConstraint(
            "doc_id",
            "version",
            name="uq_agent_doc_revision_version",
        ),
    )
    with op.batch_alter_table("agent_doc_revision", schema=None) as batch_op:
        batch_op.create_index(
            "ix_agent_doc_revision_doc_created",
            ["doc_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("agent_doc_revision", schema=None) as batch_op:
        batch_op.drop_index("ix_agent_doc_revision_doc_created")
    op.drop_table("agent_doc_revision")

    with op.batch_alter_table("agent_doc", schema=None) as batch_op:
        batch_op.drop_index("uq_agent_doc_active_slug")
    op.drop_table("agent_doc")
