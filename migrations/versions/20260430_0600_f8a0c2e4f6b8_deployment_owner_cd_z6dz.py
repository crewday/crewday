"""deployment owner

Revision ID: f8a0c2e4f6b8
Revises: e7a9c1d3f5b8
Create Date: 2026-04-30 06:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8a0c2e4f6b8"
down_revision: str | Sequence[str] | None = "e7a9c1d3f5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deployment_owner",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("added_by_user_id", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["added_by_user_id"],
            ["user.id"],
            name=op.f("fk_deployment_owner_added_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_deployment_owner_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_deployment_owner")),
    )
    bind = op.get_bind()
    existing_admin = bind.execute(
        sa.text(
            """
            SELECT user_id
            FROM role_grant
            WHERE scope_kind = :scope_kind
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """
        ),
        {"scope_kind": "deployment"},
    ).first()
    if existing_admin is not None:
        bind.execute(
            sa.text(
                """
                INSERT INTO deployment_owner (user_id, added_at, added_by_user_id)
                VALUES (:user_id, :added_at, NULL)
                """
            ),
            {"user_id": existing_admin.user_id, "added_at": datetime.now(UTC)},
        )


def downgrade() -> None:
    op.drop_table("deployment_owner")
