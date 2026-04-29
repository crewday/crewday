"""booking payroll ledger cd-n0t4

Revision ID: e5f7a9c1d3e6
Revises: d4f6a8c0e2b4
Create Date: 2026-04-29 19:00:00.000000

Add the pay-bearing booking atom and daily pay-period entries used by
the payroll close pipeline.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f7a9c1d3e6"
down_revision: str | Sequence[str] | None = "d4f6a8c0e2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "booking",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("work_engagement_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=True),
        sa.Column("client_org_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("pay_basis", sa.String(), nullable=False),
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actual_minutes", sa.Integer(), nullable=True),
        sa.Column("actual_minutes_paid", sa.Integer(), nullable=False),
        sa.Column("break_seconds", sa.Integer(), nullable=False),
        sa.Column("notes_md", sa.String(), nullable=True),
        sa.Column("adjusted", sa.Boolean(), nullable=False),
        sa.Column("adjustment_reason", sa.String(), nullable=True),
        sa.Column("pending_amend_minutes", sa.Integer(), nullable=True),
        sa.Column("pending_amend_reason", sa.String(), nullable=True),
        sa.Column("declined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("declined_reason", sa.String(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_window_hours", sa.Integer(), nullable=False),
        sa.Column("cancellation_pay_to_worker", sa.Boolean(), nullable=False),
        sa.Column("created_by_actor_kind", sa.String(), nullable=True),
        sa.Column("created_by_actor_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "actual_minutes IS NULL OR actual_minutes >= 0",
            name=op.f("ck_booking_actual_minutes_nonneg"),
        ),
        sa.CheckConstraint(
            "actual_minutes_paid >= 0",
            name=op.f("ck_booking_actual_minutes_paid_nonneg"),
        ),
        sa.CheckConstraint(
            "break_seconds >= 0",
            name=op.f("ck_booking_break_seconds_nonneg"),
        ),
        sa.CheckConstraint(
            "cancellation_window_hours >= 0",
            name=op.f("ck_booking_cancellation_window_hours_nonneg"),
        ),
        sa.CheckConstraint(
            "kind IN ('work', 'travel')",
            name=op.f("ck_booking_kind"),
        ),
        sa.CheckConstraint(
            "NOT adjusted OR adjustment_reason IS NOT NULL",
            name=op.f("ck_booking_adjusted_reason_required"),
        ),
        sa.CheckConstraint(
            "pay_basis IN ('scheduled', 'actual')",
            name=op.f("ck_booking_pay_basis"),
        ),
        sa.CheckConstraint(
            "pending_amend_minutes IS NULL OR pending_amend_minutes >= 0",
            name=op.f("ck_booking_pending_amend_minutes_nonneg"),
        ),
        sa.CheckConstraint(
            "(pending_amend_minutes IS NULL AND pending_amend_reason IS NULL) "
            "OR (pending_amend_minutes IS NOT NULL "
            "AND pending_amend_reason IS NOT NULL)",
            name=op.f("ck_booking_pending_amend_pairing"),
        ),
        sa.CheckConstraint(
            "scheduled_end > scheduled_start",
            name=op.f("ck_booking_scheduled_window"),
        ),
        sa.CheckConstraint(
            "status IN ('pending_approval', 'scheduled', 'completed', "
            "'cancelled_by_client', 'cancelled_by_agency', 'no_show_worker', "
            "'adjusted')",
            name=op.f("ck_booking_status"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_booking_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["work_engagement_id"],
            ["work_engagement.id"],
            name=op.f("fk_booking_work_engagement_id_work_engagement"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_booking_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_booking")),
    )
    op.create_index(
        "ix_booking_workspace_engagement_start",
        "booking",
        ["workspace_id", "work_engagement_id", "scheduled_start"],
        unique=False,
    )
    op.create_index(
        "ix_booking_workspace_status_start",
        "booking",
        ["workspace_id", "status", "scheduled_start"],
        unique=False,
    )
    op.create_index(
        "ix_booking_workspace_user_start",
        "booking",
        ["workspace_id", "user_id", "scheduled_start"],
        unique=False,
    )

    op.create_table(
        "pay_period_entry",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("pay_period_id", sa.String(), nullable=False),
        sa.Column("work_engagement_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("source_booking_ids_json", sa.JSON(), nullable=False),
        sa.Column("source_details_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "minutes >= 0", name=op.f("ck_pay_period_entry_minutes_nonneg")
        ),
        sa.ForeignKeyConstraint(
            ["pay_period_id"],
            ["pay_period.id"],
            name=op.f("fk_pay_period_entry_pay_period_id_pay_period"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_pay_period_entry_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["work_engagement_id"],
            ["work_engagement.id"],
            name=op.f("fk_pay_period_entry_work_engagement_id_work_engagement"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_pay_period_entry_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pay_period_entry")),
        sa.UniqueConstraint(
            "pay_period_id",
            "work_engagement_id",
            "entry_date",
            name="uq_pay_period_entry_period_engagement_day",
        ),
    )
    op.create_index(
        "ix_pay_period_entry_workspace_period",
        "pay_period_entry",
        ["workspace_id", "pay_period_id"],
        unique=False,
    )
    op.create_index(
        "ix_pay_period_entry_workspace_user_day",
        "pay_period_entry",
        ["workspace_id", "user_id", "entry_date"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_pay_period_entry_workspace_user_day",
        table_name="pay_period_entry",
    )
    op.drop_index(
        "ix_pay_period_entry_workspace_period",
        table_name="pay_period_entry",
    )
    op.drop_table("pay_period_entry")
    op.drop_index("ix_booking_workspace_user_start", table_name="booking")
    op.drop_index("ix_booking_workspace_status_start", table_name="booking")
    op.drop_index("ix_booking_workspace_engagement_start", table_name="booking")
    op.drop_table("booking")
