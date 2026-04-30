"""demo webhook suppressed status cd-xuk2

Revision ID: a6c8e0f2b4d6
Revises: f5a7c9e1b3d6
Create Date: 2026-04-30 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a6c8e0f2b4d6"
down_revision: str | Sequence[str] | None = "f5a7c9e1b3d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "'pending', 'in_flight', 'succeeded', 'dead_lettered'"
_NEW = "'pending', 'in_flight', 'succeeded', 'dead_lettered', 'suppressed_demo'"


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("webhook_delivery", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_webhook_delivery_status"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_webhook_delivery_status"),
            f"status IN ({_NEW})",
        )


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE webhook_delivery SET status = 'dead_lettered' "
            "WHERE status = 'suppressed_demo'"
        )
    )
    with op.batch_alter_table("webhook_delivery", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_webhook_delivery_status"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_webhook_delivery_status"),
            f"status IN ({_OLD})",
        )
