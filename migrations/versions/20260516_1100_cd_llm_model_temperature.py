"""move llm temperature default to model

Revision ID: cdmodeltemperature
Revises: cdlocalembedbge
Create Date: 2026-05-16 11:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cdmodeltemperature"
down_revision: str | Sequence[str] | None = "cdlocalembedbge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    with op.batch_alter_table("llm_model", schema=None) as batch_op:
        batch_op.add_column(sa.Column("temperature", sa.Float(), nullable=True))

    llm_model = sa.table(
        "llm_model",
        sa.column("id", sa.String()),
        sa.column("temperature", sa.Float()),
    )
    llm_provider_model = sa.table(
        "llm_provider_model",
        sa.column("model_id", sa.String()),
        sa.column("temperature_override", sa.Float()),
    )
    rows = conn.execute(
        sa.select(
            llm_provider_model.c.model_id,
            llm_provider_model.c.temperature_override,
        ).where(llm_provider_model.c.temperature_override.is_not(None))
    ).all()
    temperatures_by_model: dict[str, set[float]] = {}
    for model_id, temperature in rows:
        if temperature is None:
            continue
        temperatures_by_model.setdefault(model_id, set()).add(float(temperature))
    for model_id, temperatures in temperatures_by_model.items():
        if len(temperatures) == 1:
            conn.execute(
                llm_model.update()
                .where(llm_model.c.id == model_id)
                .values(temperature=next(iter(temperatures)))
            )

    with op.batch_alter_table("llm_provider_model", schema=None) as batch_op:
        batch_op.drop_column("temperature_override")

    _rename_local_provider("Local FastEmbed", "Local")


def downgrade() -> None:
    conn = op.get_bind()
    with op.batch_alter_table("llm_provider_model", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("temperature_override", sa.Float(), nullable=True)
        )

    llm_model = sa.table(
        "llm_model",
        sa.column("id", sa.String()),
        sa.column("temperature", sa.Float()),
    )
    llm_provider_model = sa.table(
        "llm_provider_model",
        sa.column("model_id", sa.String()),
        sa.column("temperature_override", sa.Float()),
    )
    rows = conn.execute(
        sa.select(llm_model.c.id, llm_model.c.temperature).where(
            llm_model.c.temperature.is_not(None)
        )
    ).all()
    for model_id, temperature in rows:
        conn.execute(
            llm_provider_model.update()
            .where(llm_provider_model.c.model_id == model_id)
            .values(temperature_override=temperature)
        )

    with op.batch_alter_table("llm_model", schema=None) as batch_op:
        batch_op.drop_column("temperature")

    _rename_local_provider("Local", "Local FastEmbed")


def _rename_local_provider(old_name: str, new_name: str) -> None:
    conn = op.get_bind()
    provider = sa.table(
        "llm_provider",
        sa.column("name", sa.String()),
    )
    assignment = sa.table(
        "llm_assignment",
        sa.column("provider", sa.String()),
    )
    existing = conn.scalar(
        sa.select(provider.c.name).where(provider.c.name == new_name).limit(1)
    )
    if existing is None:
        conn.execute(
            provider.update().where(provider.c.name == old_name).values(name=new_name)
        )
        conn.execute(
            assignment.update()
            .where(assignment.c.provider == old_name)
            .values(provider=new_name)
        )
