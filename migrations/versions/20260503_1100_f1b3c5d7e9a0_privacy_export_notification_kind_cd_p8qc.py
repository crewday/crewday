"""privacy_export_notification_kind_cd_p8qc

Revision ID: f1b3c5d7e9a0
Revises: e0a2b3c4d5f7
Create Date: 2026-05-03 11:00:00.000000

Allow ``NotificationService`` to fan out the ``privacy_export_ready``
kind so the §15 privacy access export can deliver the bundle's
download URL via the standard email path.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1b3c5d7e9a0"
down_revision: str | Sequence[str] | None = "e0a2b3c4d5f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KIND_CHECK = (
    "kind IN ('task_assigned', 'task_overdue', 'expense_approved', "
    "'expense_rejected', 'expense_submitted', 'approval_needed', "
    "'approval_decided', 'issue_reported', 'issue_resolved', "
    "'comment_mention', 'payslip_issued', 'stay_upcoming', "
    "'anomaly_detected', 'agent_message', 'daily_digest')"
)

_NEW_KIND_CHECK = (
    "kind IN ('task_assigned', 'task_overdue', 'expense_approved', "
    "'expense_rejected', 'expense_submitted', 'approval_needed', "
    "'approval_decided', 'issue_reported', 'issue_resolved', "
    "'comment_mention', 'payslip_issued', 'stay_upcoming', "
    "'anomaly_detected', 'agent_message', 'daily_digest', "
    "'privacy_export_ready')"
)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("notification", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_notification_kind"), type_="check")
        batch_op.create_check_constraint("kind", _NEW_KIND_CHECK)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("notification", schema=None) as batch_op:
        batch_op.drop_constraint("kind", type_="check")
        batch_op.create_check_constraint("kind", _OLD_KIND_CHECK)
