"""issue reports cd-h4il

Revision ID: a3c5e7f9b1d4
Revises: f2a4c6e8b0d2
Create Date: 2026-04-30 01:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3c5e7f9b1d4"
down_revision: str | Sequence[str] | None = "f2a4c6e8b0d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "issue_report",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("reported_by_user_id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=False),
        sa.Column("area_id", sa.String(), nullable=True),
        sa.Column("area_label", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description_md", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), server_default="normal", nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("state", sa.String(), server_default="open", nullable=False),
        sa.Column(
            "attachment_file_ids_json",
            sa.JSON(),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("converted_to_task_id", sa.String(), nullable=True),
        sa.Column("resolution_note", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "category IN ('damage', 'broken', 'supplies', 'safety', 'other')",
            name=op.f("ck_issue_report_category"),
        ),
        sa.CheckConstraint(
            "severity IN ('low', 'normal', 'high', 'urgent')",
            name=op.f("ck_issue_report_severity"),
        ),
        sa.CheckConstraint(
            "state IN ('open', 'in_progress', 'resolved', 'wont_fix')",
            name=op.f("ck_issue_report_state"),
        ),
        sa.ForeignKeyConstraint(
            ["area_id"],
            ["area.id"],
            name=op.f("fk_issue_report_area_id_area"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["converted_to_task_id"],
            ["occurrence.id"],
            name=op.f("fk_issue_report_converted_to_task_id_occurrence"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_issue_report_property_id_property"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reported_by_user_id"],
            ["user.id"],
            name=op.f("fk_issue_report_reported_by_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["user.id"],
            name=op.f("fk_issue_report_resolved_by_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["occurrence.id"],
            name=op.f("fk_issue_report_task_id_occurrence"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_issue_report_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_issue_report")),
    )
    with op.batch_alter_table("issue_report", schema=None) as batch_op:
        batch_op.create_index("ix_issue_report_workspace", ["workspace_id"])
        batch_op.create_index(
            "ix_issue_report_workspace_created",
            ["workspace_id", "created_at"],
        )
        batch_op.create_index(
            "ix_issue_report_workspace_property",
            ["workspace_id", "property_id"],
        )
        batch_op.create_index(
            "ix_issue_report_workspace_state_created",
            ["workspace_id", "state", "created_at"],
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("issue_report", schema=None) as batch_op:
        batch_op.drop_index("ix_issue_report_workspace_state_created")
        batch_op.drop_index("ix_issue_report_workspace_property")
        batch_op.drop_index("ix_issue_report_workspace_created")
        batch_op.drop_index("ix_issue_report_workspace")
    op.drop_table("issue_report")
