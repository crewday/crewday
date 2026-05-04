"""api token request audit log cd-ocdg7

Revision ID: b5d7f9a1c3e6
Revises: a4c6e8f0b2d4
Create Date: 2026-05-04 11:00:00.000000

Adds ``api_token_request_log`` for one per-request audit row per
Bearer-presented token request. Raw client IPs are not stored; callers
persist the spec-truncated prefix only.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.adapters.db._columns import UtcDateTime

revision: str = "b5d7f9a1c3e6"
down_revision: str | Sequence[str] | None = "a4c6e8f0b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``api_token_request_log`` table."""
    op.create_table(
        "api_token_request_log",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column(
            "token_id",
            sa.String(),
            sa.ForeignKey("api_token.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=256), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("ip_prefix", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("correlation_id", sa.String(), nullable=False),
        sa.Column("at", UtcDateTime(), nullable=False),
    )
    op.create_index(
        "ix_api_token_request_log_token_at",
        "api_token_request_log",
        ["token_id", "at"],
    )


def downgrade() -> None:
    """Drop the ``api_token_request_log`` table."""
    op.drop_index(
        "ix_api_token_request_log_token_at",
        table_name="api_token_request_log",
    )
    op.drop_table("api_token_request_log")
