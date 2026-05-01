"""task completed state cd-7014

Revision ID: e6f8a0c2d4b6
Revises: d5f7a9c1e3b5
Create Date: 2026-05-01 00:00:00.000000

Rename the persisted ``occurrence.state`` completion value from
``done`` to ``completed`` so storage matches §06 / §02 and the
``task.completed`` event vocabulary. cd-7am7 shipped the completion
service against ``done`` because the then-current CHECK constraint
rejected ``completed``; this migration closes that spec drift.

SQLite enforces CHECK constraints row-by-row during UPDATE and uses a
table rebuild for CHECK changes, so both upgrade and downgrade use a
temporary compatibility constraint that accepts both values, rewrite
the rows, then narrow the constraint to the target enum.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6f8a0c2d4b6"
down_revision: str | Sequence[str] | None = "d5f7a9c1e3b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATE_CHECK_BOTH = (
    "state IN ('scheduled', 'pending', 'in_progress', 'done', 'completed', "
    "'skipped', 'approved', 'cancelled', 'overdue')"
)
_STATE_CHECK_COMPLETED = (
    "state IN ('scheduled', 'pending', 'in_progress', 'completed', "
    "'skipped', 'approved', 'cancelled', 'overdue')"
)
_STATE_CHECK_DONE = (
    "state IN ('scheduled', 'pending', 'in_progress', 'done', "
    "'skipped', 'approved', 'cancelled', 'overdue')"
)


def _replace_state_check(sql: str) -> None:
    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        batch_op.drop_constraint("state", type_="check")
        batch_op.create_check_constraint("state", sql)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    _replace_state_check(_STATE_CHECK_BOTH)
    bind.execute(
        sa.text("UPDATE occurrence SET state = 'completed' WHERE state = 'done'")
    )
    _replace_state_check(_STATE_CHECK_COMPLETED)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    _replace_state_check(_STATE_CHECK_BOTH)
    bind.execute(
        sa.text("UPDATE occurrence SET state = 'done' WHERE state = 'completed'")
    )
    _replace_state_check(_STATE_CHECK_DONE)
