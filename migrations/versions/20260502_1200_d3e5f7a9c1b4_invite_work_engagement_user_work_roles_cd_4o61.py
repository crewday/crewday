"""invite work_engagement + user_work_roles cd-4o61

Revision ID: d3e5f7a9c1b4
Revises: c2d4e6f8a1b3
Create Date: 2026-05-02 12:00:00.000000

Extends the ``invite`` row with two new pending payload columns:

* ``work_engagement_json`` — single optional engagement seed
  ({engagement_kind, supplier_org_id?}) captured at invite time and
  consumed by ``_activate_invite`` to override the default
  ``payroll`` engagement.
* ``user_work_roles_json`` — list of {work_role_id} entries; each
  inserts a fresh ``user_work_role`` row inside the same UoW that
  activates the membership.

Both columns are nullable / empty-default so existing pending invites
backfill cleanly without rewriting them. See spec §03 "Additional
users (invite → click-to-accept)" and the cd-4o61 acceptance criteria.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3e5f7a9c1b4"
down_revision: str | Sequence[str] | None = "c2d4e6f8a1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``work_engagement_json`` + ``user_work_roles_json`` to ``invite``."""
    with op.batch_alter_table("invite", schema=None) as batch_op:
        batch_op.add_column(sa.Column("work_engagement_json", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "user_work_roles_json",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )

    with op.batch_alter_table("invite", schema=None) as batch_op:
        batch_op.alter_column("user_work_roles_json", server_default=None)


def downgrade() -> None:
    """Drop the two cd-4o61 columns from ``invite``."""
    with op.batch_alter_table("invite", schema=None) as batch_op:
        batch_op.drop_column("user_work_roles_json")
        batch_op.drop_column("work_engagement_json")
