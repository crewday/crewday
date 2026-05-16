"""add llm provider model media transform settings

Revision ID: cd3601gmediatx
Revises: cd6y6s5audsecs
Create Date: 2026-05-16 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cd3601gmediatx"
down_revision: str | Sequence[str] | None = "cd6y6s5audsecs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUDIO_INPUT_TRANSFORMS = "'passthrough', 'wav_16khz_mono'"
_IMAGE_INPUT_FORMATS = "'preserve', 'jpeg', 'png', 'webp'"


def upgrade() -> None:
    with op.batch_alter_table("llm_provider_model", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "audio_input_transform",
                sa.String(),
                nullable=False,
                server_default="passthrough",
            )
        )
        batch_op.add_column(
            sa.Column(
                "image_input_format",
                sa.String(),
                nullable=False,
                server_default="preserve",
            )
        )
        batch_op.add_column(
            sa.Column("image_input_max_edge_px", sa.Integer(), nullable=True)
        )
        batch_op.create_check_constraint(
            op.f("ck_llm_provider_model_audio_input_transform"),
            f"audio_input_transform IN ({_AUDIO_INPUT_TRANSFORMS})",
        )
        batch_op.create_check_constraint(
            op.f("ck_llm_provider_model_image_input_format"),
            f"image_input_format IN ({_IMAGE_INPUT_FORMATS})",
        )
        batch_op.create_check_constraint(
            op.f("ck_llm_provider_model_image_input_max_edge_px_positive"),
            "image_input_max_edge_px IS NULL OR image_input_max_edge_px > 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("llm_provider_model", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("ck_llm_provider_model_image_input_max_edge_px_positive"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_llm_provider_model_image_input_format"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_llm_provider_model_audio_input_transform"),
            type_="check",
        )
        batch_op.drop_column("image_input_max_edge_px")
        batch_op.drop_column("image_input_format")
        batch_op.drop_column("audio_input_transform")
