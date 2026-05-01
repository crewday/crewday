"""llm prompt templates cd-4if3

Revision ID: c4e6a8b0d2f4
Revises: f3a5c7e9b1d4
Create Date: 2026-04-30 17:00:00.000000

NOTE — constraint rename (2026-05-01): the
``llm_prompt_template_revision`` ``version >= 1`` check was originally
named ``ck_llm_prompt_template_revision_llm_prompt_template_revision_version``
(68 chars) which exceeds Postgres' 63-byte identifier limit. The
constraint name was shortened to ``ck_llm_prompt_template_revision_version_min``
in-place. Pre-prod (no live DB at this revision yet), so editing the
migration file in place is acceptable. **If you have a dev DB already
upgraded to ``c4e6a8b0d2f4`` from before this rename**, reset that DB
(per AGENTS.md "Dev verification helpers") so the new constraint name
matches the model — ``alembic upgrade head`` is a no-op against an
already-applied revision and will not rename in place.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e6a8b0d2f4"
down_revision: str | Sequence[str] | None = "f3a5c7e9b1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "llm_prompt_template",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("capability", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("template", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("default_hash", sa.String(length=16), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_llm_prompt_template_llm_prompt_template_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_prompt_template")),
    )
    with op.batch_alter_table("llm_prompt_template", schema=None) as batch_op:
        batch_op.create_index(
            "uq_llm_prompt_template_active_capability",
            ["capability"],
            unique=True,
            sqlite_where=sa.text("is_active = 1"),
            postgresql_where=sa.text("is_active = true"),
        )

    op.create_table(
        "llm_prompt_template_revision",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.String(), nullable=True),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_llm_prompt_template_revision_version_min"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_llm_prompt_template_revision_created_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["llm_prompt_template.id"],
            name=op.f(
                "fk_llm_prompt_template_revision_template_id_llm_prompt_template"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_prompt_template_revision")),
        sa.UniqueConstraint(
            "template_id",
            "version",
            name="uq_llm_prompt_template_revision_version",
        ),
    )
    with op.batch_alter_table("llm_prompt_template_revision", schema=None) as batch_op:
        batch_op.create_index(
            "ix_llm_prompt_template_revision_template_created",
            ["template_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("llm_prompt_template_revision", schema=None) as batch_op:
        batch_op.drop_index("ix_llm_prompt_template_revision_template_created")
    op.drop_table("llm_prompt_template_revision")

    with op.batch_alter_table("llm_prompt_template", schema=None) as batch_op:
        batch_op.drop_index("uq_llm_prompt_template_active_capability")
    op.drop_table("llm_prompt_template")
