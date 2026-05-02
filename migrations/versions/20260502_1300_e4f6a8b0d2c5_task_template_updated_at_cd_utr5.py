"""task_template.updated_at cd-utr5

Revision ID: e4f6a8b0d2c5
Revises: d3e5f7a9c1b4
Create Date: 2026-05-02 13:00:00.000000

Brings ``task_template`` into compliance with the §02 convention
"created_at, updated_at on every row". The cd-chd v1 slice landed
the table with ``created_at`` only; cd-utr5 adds the missing
``updated_at`` column.

Backfill strategy. Existing rows have no recorded mutation time, so
``updated_at`` is seeded equal to ``created_at`` (every row "was last
updated when it was created"). The column is added nullable first so
the seed UPDATE has somewhere to write, then tightened to NOT NULL
once every row carries a value. ``with op.batch_alter_table(...)``
wraps both the add and the alter so SQLite's table-rebuild path
applies cleanly; Postgres handles the same statements as plain
``ALTER TABLE``.

Reversibility. ``downgrade()`` drops the column. The recorded
``updated_at`` values are discarded — acceptable for a feature-
extension rollback on a dev DB.

See ``docs/specs/02-domain-model.md`` §"Timestamps" and
``docs/specs/06-tasks-and-scheduling.md`` §"Task template".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4f6a8b0d2c5"
down_revision: str | Sequence[str] | None = "d3e5f7a9c1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``task_template.updated_at`` (NOT NULL after backfill)."""
    # 1. Add nullable so existing rows survive; the seed UPDATE
    #    below fills them, then step 3 tightens to NOT NULL.
    with op.batch_alter_table("task_template", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
        )

    # 2. Backfill — every existing row "was last updated when it
    #    was created". Portable SQL works on SQLite + Postgres.
    op.execute(
        "UPDATE task_template SET updated_at = created_at WHERE updated_at IS NULL"
    )

    # 3. Tighten to NOT NULL now that every row carries a value.
    with op.batch_alter_table("task_template", schema=None) as batch_op:
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )


def downgrade() -> None:
    """Drop ``task_template.updated_at``."""
    with op.batch_alter_table("task_template", schema=None) as batch_op:
        batch_op.drop_column("updated_at")
