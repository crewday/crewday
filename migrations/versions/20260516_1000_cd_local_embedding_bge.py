"""add local embedding llm seed

Revision ID: cdlocalembedbge
Revises: cddropmodelvendor
Create Date: 2026-05-16 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "cdlocalembedbge"
down_revision: str | Sequence[str] | None = "cddropmodelvendor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCAL_PROVIDER_ID = "seed-local-fastembed"
_LOCAL_MODEL_ID = "seed-bge-small-en-v15"
_LOCAL_PROVIDER_MODEL_ID = "seed-pm-bge-small-en-v15"
_LOCAL_ASSIGNMENT_ID = "seed-feedback-embed-bge"
_LOCAL_PROVIDER_NAME = "Local FastEmbed"
_LOCAL_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_LOCAL_MODEL_DISPLAY = "BGE Small EN v1.5"


def upgrade() -> None:
    with op.batch_alter_table("llm_provider", schema=None) as batch_op:
        batch_op.drop_constraint("provider_type", type_="check")
        batch_op.create_check_constraint(
            "provider_type",
            "provider_type IN "
            "('openrouter', 'openai_compatible', 'fake', 'local_embedding')",
        )
    with op.batch_alter_table("llm_model", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("embedding_dimensions", sa.Integer(), nullable=True)
        )

    _seed_local_embedding()


def downgrade() -> None:
    conn = op.get_bind()
    llm_assignment = sa.table(
        "llm_assignment",
        sa.column("id", sa.String()),
        sa.column("capability", sa.String()),
        sa.column("model_id", sa.String()),
    )
    llm_provider_model = sa.table(
        "llm_provider_model",
        sa.column("id", sa.String()),
        sa.column("provider_id", sa.String()),
        sa.column("model_id", sa.String()),
    )
    llm_provider = sa.table(
        "llm_provider",
        sa.column("id", sa.String()),
        sa.column("provider_type", sa.String()),
    )
    llm_model = sa.table(
        "llm_model",
        sa.column("id", sa.String()),
        sa.column("canonical_name", sa.String()),
    )
    conn.execute(
        llm_assignment.delete().where(llm_assignment.c.id == _LOCAL_ASSIGNMENT_ID)
    )
    conn.execute(
        llm_provider_model.delete().where(
            llm_provider_model.c.id == _LOCAL_PROVIDER_MODEL_ID
        )
    )
    conn.execute(llm_provider.delete().where(llm_provider.c.id == _LOCAL_PROVIDER_ID))
    conn.execute(llm_model.delete().where(llm_model.c.id == _LOCAL_MODEL_ID))

    with op.batch_alter_table("llm_model", schema=None) as batch_op:
        batch_op.drop_column("embedding_dimensions")
    with op.batch_alter_table("llm_provider", schema=None) as batch_op:
        batch_op.drop_constraint("provider_type", type_="check")
        batch_op.create_check_constraint(
            "provider_type",
            "provider_type IN ('openrouter', 'openai_compatible', 'fake')",
        )


def _seed_local_embedding() -> None:
    conn = op.get_bind()
    now = datetime.now(UTC)
    llm_provider = sa.table(
        "llm_provider",
        sa.column("id", sa.String()),
        sa.column("name", sa.String()),
        sa.column("provider_type", sa.String()),
        sa.column("timeout_s", sa.Integer()),
        sa.column("requests_per_minute", sa.Integer()),
        sa.column("is_enabled", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    llm_model = sa.table(
        "llm_model",
        sa.column("id", sa.String()),
        sa.column("canonical_name", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("capabilities", sa.JSON()),
        sa.column("context_window", sa.Integer()),
        sa.column("max_output_tokens", sa.Integer()),
        sa.column("embedding_dimensions", sa.Integer()),
        sa.column("thinking_level", sa.String()),
        sa.column("thinking_strategy", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("price_source", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    llm_provider_model = sa.table(
        "llm_provider_model",
        sa.column("id", sa.String()),
        sa.column("provider_id", sa.String()),
        sa.column("model_id", sa.String()),
        sa.column("api_model_id", sa.String()),
        sa.column("input_cost_per_million", sa.Numeric(10, 4)),
        sa.column("output_cost_per_million", sa.Numeric(10, 4)),
        sa.column("fixed_cost_per_call_usd", sa.Numeric(10, 6)),
        sa.column("supports_system_prompt", sa.Boolean()),
        sa.column("supports_temperature", sa.Boolean()),
        sa.column("extra_api_params", sa.JSON()),
        sa.column("price_source_override", sa.String()),
        sa.column("is_enabled", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    llm_assignment = sa.table(
        "llm_assignment",
        sa.column("id", sa.String()),
        sa.column("workspace_id", sa.String()),
        sa.column("capability", sa.String()),
        sa.column("model_id", sa.String()),
        sa.column("provider", sa.String()),
        sa.column("priority", sa.Integer()),
        sa.column("enabled", sa.Boolean()),
        sa.column("max_tokens", sa.Integer()),
        sa.column("temperature", sa.Float()),
        sa.column("extra_api_params", sa.JSON()),
        sa.column("required_capabilities", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )

    provider_id = conn.scalar(
        sa.select(llm_provider.c.id).where(llm_provider.c.name == _LOCAL_PROVIDER_NAME)
    )
    if provider_id is None:
        provider_id = _LOCAL_PROVIDER_ID
        conn.execute(
            llm_provider.insert().values(
                id=provider_id,
                name=_LOCAL_PROVIDER_NAME,
                provider_type="local_embedding",
                timeout_s=60,
                requests_per_minute=60,
                is_enabled=True,
                created_at=now,
                updated_at=now,
            )
        )

    model_id = conn.scalar(
        sa.select(llm_model.c.id).where(llm_model.c.canonical_name == _LOCAL_MODEL_NAME)
    )
    if model_id is None:
        model_id = _LOCAL_MODEL_ID
        conn.execute(
            llm_model.insert().values(
                id=model_id,
                canonical_name=_LOCAL_MODEL_NAME,
                display_name=_LOCAL_MODEL_DISPLAY,
                capabilities=["embeddings"],
                context_window=None,
                max_output_tokens=None,
                embedding_dimensions=384,
                thinking_level="disabled",
                thinking_strategy="none",
                is_active=True,
                price_source="manual",
                created_at=now,
                updated_at=now,
            )
        )

    provider_model_id = conn.scalar(
        sa.select(llm_provider_model.c.id).where(
            llm_provider_model.c.provider_id == provider_id,
            llm_provider_model.c.model_id == model_id,
        )
    )
    if provider_model_id is None:
        provider_model_id = _LOCAL_PROVIDER_MODEL_ID
        conn.execute(
            llm_provider_model.insert().values(
                id=provider_model_id,
                provider_id=provider_id,
                model_id=model_id,
                api_model_id=_LOCAL_MODEL_NAME,
                input_cost_per_million=0,
                output_cost_per_million=0,
                fixed_cost_per_call_usd=0,
                supports_system_prompt=False,
                supports_temperature=False,
                extra_api_params={},
                price_source_override="none",
                is_enabled=True,
                created_at=now,
                updated_at=now,
            )
        )

    assignment_id = conn.scalar(
        sa.select(llm_assignment.c.id).where(
            llm_assignment.c.workspace_id.is_(None),
            llm_assignment.c.capability == "feedback.embed",
        )
    )
    if assignment_id is None:
        conn.execute(
            llm_assignment.insert().values(
                id=_LOCAL_ASSIGNMENT_ID,
                workspace_id=None,
                capability="feedback.embed",
                model_id=provider_model_id,
                provider=_LOCAL_PROVIDER_NAME,
                priority=0,
                enabled=True,
                max_tokens=None,
                temperature=None,
                extra_api_params={},
                required_capabilities=["embeddings"],
                created_at=now,
            )
        )
