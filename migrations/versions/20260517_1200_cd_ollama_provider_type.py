"""add ollama llm provider type

Revision ID: cdollamaprovider
Revises: cd3601gmediatx
Create Date: 2026-05-17 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "cdollamaprovider"
down_revision: str | Sequence[str] | None = "cd3601gmediatx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("llm_provider", schema=None) as batch_op:
        batch_op.drop_constraint("provider_type", type_="check")
        batch_op.create_check_constraint(
            "provider_type",
            "provider_type IN "
            "('openrouter', 'openai_compatible', 'ollama', 'fake', "
            "'local_embedding')",
        )


def downgrade() -> None:
    with op.batch_alter_table("llm_provider", schema=None) as batch_op:
        batch_op.drop_constraint("provider_type", type_="check")
        batch_op.create_check_constraint(
            "provider_type",
            "provider_type IN "
            "('openrouter', 'openai_compatible', 'fake', 'local_embedding')",
        )
