"""reservation unit mapping cd-z23ji

Revision ID: a0c2e4f6a8b1
Revises: f9b1c3d5e7a9
Create Date: 2026-05-05 12:00:00.000000

Adds nullable unit mapping to reservations so manual stay creation can
round-trip the unit chosen by the manager. Also widens the reservation
status CHECK so manual rows can store the §04/UI status values while
legacy iCal rows remain readable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a0c2e4f6a8b1"
down_revision: str | Sequence[str] | None = "f9b1c3d5e7a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable reservation-to-unit mapping."""
    with op.batch_alter_table("reservation", schema=None) as batch_op:
        batch_op.add_column(sa.Column("unit_id", sa.String(), nullable=True))
        batch_op.drop_constraint(op.f("ck_reservation_status"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_reservation_status"),
            (
                "status IN ('scheduled', 'checked_in', 'completed', "
                "'tentative', 'confirmed', 'in_house', 'checked_out', 'cancelled')"
            ),
        )
        batch_op.create_foreign_key(
            op.f("fk_reservation_unit_id_unit"),
            "unit",
            ["unit_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.execute(
        sa.text(
            """
            UPDATE reservation
            SET unit_id = (
                SELECT ical_feed.unit_id
                FROM ical_feed
                WHERE ical_feed.id = reservation.ical_feed_id
            )
            WHERE unit_id IS NULL
              AND ical_feed_id IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM ical_feed
                  WHERE ical_feed.id = reservation.ical_feed_id
                    AND ical_feed.unit_id IS NOT NULL
              )
            """
        )
    )
    op.create_index(
        op.f("ix_reservation_unit_check_in"),
        "reservation",
        ["unit_id", "check_in"],
        unique=False,
    )


def downgrade() -> None:
    """Drop nullable reservation-to-unit mapping."""
    op.drop_index(op.f("ix_reservation_unit_check_in"), table_name="reservation")
    op.execute(
        sa.text(
            """
            UPDATE reservation
            SET status = CASE status
                WHEN 'tentative' THEN 'scheduled'
                WHEN 'confirmed' THEN 'scheduled'
                WHEN 'in_house' THEN 'checked_in'
                WHEN 'checked_out' THEN 'completed'
                ELSE status
            END
            WHERE status IN ('tentative', 'confirmed', 'in_house', 'checked_out')
            """
        )
    )
    with op.batch_alter_table("reservation", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("fk_reservation_unit_id_unit"), type_="foreignkey"
        )
        batch_op.drop_constraint(op.f("ck_reservation_status"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_reservation_status"),
            "status IN ('scheduled', 'checked_in', 'completed', 'cancelled')",
        )
        batch_op.drop_column("unit_id")
