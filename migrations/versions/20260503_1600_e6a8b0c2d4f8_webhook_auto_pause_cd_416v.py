"""webhook auto-pause metadata cd-416v

Revision ID: e6a8b0c2d4f8
Revises: d5f7a9c1b3e6
Create Date: 2026-05-03 16:00:00.000000

Adds pause metadata to outbound webhook subscriptions and widens the
notification kind CHECK so the auto-pause worker can alert managers
through the standard notification path.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6a8b0c2d4f8"
down_revision: str | Sequence[str] | None = "d5f7a9c1b3e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KIND_CHECK = (
    "kind IN ('task_assigned', 'task_overdue', 'expense_approved', "
    "'expense_rejected', 'expense_submitted', 'approval_needed', "
    "'approval_decided', 'issue_reported', 'issue_resolved', "
    "'comment_mention', 'payslip_issued', 'stay_upcoming', "
    "'anomaly_detected', 'agent_message', 'daily_digest', "
    "'privacy_export_ready')"
)

_NEW_KIND_CHECK = (
    "kind IN ('task_assigned', 'task_overdue', 'expense_approved', "
    "'expense_rejected', 'expense_submitted', 'approval_needed', "
    "'approval_decided', 'issue_reported', 'issue_resolved', "
    "'comment_mention', 'payslip_issued', 'stay_upcoming', "
    "'anomaly_detected', 'agent_message', 'daily_digest', "
    "'privacy_export_ready', 'webhook_auto_paused')"
)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("webhook_subscription", schema=None) as batch_op:
        batch_op.add_column(sa.Column("paused_reason", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_check_constraint(
            "paused_reason",
            "paused_reason IS NULL OR paused_reason IN ('auto_unhealthy')",
        )

    with op.batch_alter_table("notification", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_notification_kind"), type_="check")
        batch_op.create_check_constraint("kind", _NEW_KIND_CHECK)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("notification", schema=None) as batch_op:
        batch_op.drop_constraint("kind", type_="check")
        batch_op.create_check_constraint("kind", _OLD_KIND_CHECK)

    with op.batch_alter_table("webhook_subscription", schema=None) as batch_op:
        batch_op.drop_constraint("paused_reason", type_="check")
        batch_op.drop_column("paused_at")
        batch_op.drop_column("paused_reason")
