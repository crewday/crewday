"""work engagement FK promotion cd-0ro4

Revision ID: d1f3a5c7e9b1
Revises: c0e2f4a6b8d0
Create Date: 2026-05-01 05:00:00.000000

Promotes work_engagement's organization and payout destination pointers
from historical soft refs into real foreign keys now that both parent
tables exist.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d1f3a5c7e9b1"
down_revision: str | Sequence[str] | None = "c0e2f4a6b8d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        UPDATE work_engagement
        SET pay_destination_id = NULL
        WHERE pay_destination_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM payout_destination
              WHERE payout_destination.id = work_engagement.pay_destination_id
          )
        """
    )
    op.execute(
        """
        UPDATE work_engagement
        SET reimbursement_destination_id = NULL
        WHERE reimbursement_destination_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM payout_destination
              WHERE payout_destination.id =
                    work_engagement.reimbursement_destination_id
          )
        """
    )
    op.execute(
        """
        DELETE FROM work_engagement
        WHERE supplier_org_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM organization
              WHERE organization.id = work_engagement.supplier_org_id
          )
        """
    )

    with op.batch_alter_table("work_engagement", schema=None) as batch_op:
        batch_op.create_foreign_key(
            op.f("fk_work_engagement_supplier_org_id_organization"),
            "organization",
            ["supplier_org_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            op.f("fk_work_engagement_pay_destination_id_payout_destination"),
            "payout_destination",
            ["pay_destination_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            op.f(
                "fk_work_engagement_reimbursement_destination_id_payout_destination"
            ),
            "payout_destination",
            ["reimbursement_destination_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("work_engagement", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("fk_work_engagement_reimbursement_destination_id_payout_destination"),
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            op.f("fk_work_engagement_pay_destination_id_payout_destination"),
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            op.f("fk_work_engagement_supplier_org_id_organization"),
            type_="foreignkey",
        )
