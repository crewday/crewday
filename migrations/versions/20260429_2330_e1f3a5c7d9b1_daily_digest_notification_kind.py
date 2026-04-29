"""daily_digest_notification_kind

Revision ID: e1f3a5c7d9b1
Revises: d0e2f4a6b8c0
Create Date: 2026-04-29 23:30:00.000000

Allow NotificationService to fan out daily digest notifications.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f3a5c7d9b1"
down_revision: str | Sequence[str] | None = "d0e2f4a6b8c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KIND_CHECK = (
    "kind IN ('task_assigned', 'task_overdue', 'expense_approved', "
    "'expense_rejected', 'expense_submitted', 'approval_needed', "
    "'approval_decided', 'issue_reported', 'issue_resolved', "
    "'comment_mention', 'payslip_issued', 'stay_upcoming', "
    "'anomaly_detected', 'agent_message')"
)

_NEW_KIND_CHECK = (
    "kind IN ('task_assigned', 'task_overdue', 'expense_approved', "
    "'expense_rejected', 'expense_submitted', 'approval_needed', "
    "'approval_decided', 'issue_reported', 'issue_resolved', "
    "'comment_mention', 'payslip_issued', 'stay_upcoming', "
    "'anomaly_detected', 'agent_message', 'daily_digest')"
)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("notification", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_notification_kind"), type_="check")
        batch_op.create_check_constraint("kind", _NEW_KIND_CHECK)
    with op.batch_alter_table("digest_record", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_digest_record_workspace_recipient_period_kind",
            ["workspace_id", "recipient_user_id", "period_start", "kind"],
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("digest_record", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_digest_record_workspace_recipient_period_kind",
            type_="unique",
        )
    with op.batch_alter_table("notification", schema=None) as batch_op:
        batch_op.drop_constraint("kind", type_="check")
        batch_op.create_check_constraint("kind", _OLD_KIND_CHECK)
