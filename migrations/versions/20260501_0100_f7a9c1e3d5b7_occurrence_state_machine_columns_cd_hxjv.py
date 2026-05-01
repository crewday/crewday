"""occurrence state-machine columns cd-hxjv

Revision ID: f7a9c1e3d5b7
Revises: e6f8a0c2d4b6
Create Date: 2026-05-01 01:00:00.000000

Adds the remaining §06 task occurrence state-machine columns:

* ``completion_note_md TEXT NULL`` — completion-time note on the task
  row, not bridged through ``evidence(kind='note')``.
* ``skipped_reason TEXT NULL`` — skip reason distinct from
  ``cancellation_reason``.
* ``due_by_utc TIMESTAMP WITH TIME ZONE NULL`` — SLA deadline. Existing
  rows stay null; generation/update services fill it when they know the
  task duration.

``overdue_since`` and the ``overdue`` state CHECK value landed earlier
in cd-hurw / cd-7014, so this migration does not recreate them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7a9c1e3d5b7"
down_revision: str | Sequence[str] | None = "e6f8a0c2d4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        batch_op.add_column(sa.Column("completion_note_md", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("skipped_reason", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("due_by_utc", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        batch_op.drop_column("due_by_utc")
        batch_op.drop_column("skipped_reason")
        batch_op.drop_column("completion_note_md")
