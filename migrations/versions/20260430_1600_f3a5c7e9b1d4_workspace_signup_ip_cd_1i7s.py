"""workspace signup IP metadata cd-1i7s

Revision ID: f3a5c7e9b1d4
Revises: e2f4a6c8d0b2
Create Date: 2026-04-30 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a5c7e9b1d4"
down_revision: str | Sequence[str] | None = "e2f4a6c8d0b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("signup_attempt", schema=None) as batch_op:
        batch_op.add_column(sa.Column("signup_ip", sa.String(), nullable=True))

    with op.batch_alter_table("workspace", schema=None) as batch_op:
        batch_op.add_column(sa.Column("signup_ip", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("signup_ip_key", sa.String(), nullable=True))
        batch_op.create_index(
            "ix_workspace_signup_ip_key_verification_state",
            ["signup_ip_key", "verification_state"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("workspace", schema=None) as batch_op:
        batch_op.drop_index("ix_workspace_signup_ip_key_verification_state")
        batch_op.drop_column("signup_ip_key")
        batch_op.drop_column("signup_ip")

    with op.batch_alter_table("signup_attempt", schema=None) as batch_op:
        batch_op.drop_column("signup_ip")
