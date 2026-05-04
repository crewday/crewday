"""email_delivery_complaint_state_cd_qmo1m

Revision ID: c6e8f0a2d4b6
Revises: b5d7f9a1c3e6
Create Date: 2026-05-04 12:00:00.000000

Widen ``email_delivery.delivery_state`` so provider complaint
webhooks can land in the same delivery ledger as delivered, bounced,
and failed events.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c6e8f0a2d4b6"
down_revision: str | Sequence[str] | None = "b5d7f9a1c3e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_STATE_CHECK = (
    "delivery_state IN "
    "('queued', 'sent', 'delivered', 'bounced', 'complaint', 'failed')"
)
_OLD_STATE_CHECK = (
    "delivery_state IN ('queued', 'sent', 'delivered', 'bounced', 'failed')"
)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("email_delivery", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("ck_email_delivery_delivery_state"), type_="check"
        )
        batch_op.create_check_constraint("delivery_state", _NEW_STATE_CHECK)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "UPDATE email_delivery "
        "SET delivery_state = 'failed' "
        "WHERE delivery_state = 'complaint'"
    )
    with op.batch_alter_table("email_delivery", schema=None) as batch_op:
        batch_op.drop_constraint("delivery_state", type_="check")
        batch_op.create_check_constraint("delivery_state", _OLD_STATE_CHECK)
