"""occurrence property + template indexes cd-1hgd4

Revision ID: cd1hgd4occidx
Revises: cddznzkmultiprop
Create Date: 2026-07-03 12:00:00.000000

Adds two indexes to the hot ``occurrence`` table:

* ``ix_occurrence_workspace_property`` — property filters power the
  occurrences list, approvals, issues, and assignment hot paths, and
  the ``property_id`` FK is ``ON DELETE CASCADE`` (property purge scans
  it). ``id`` trails so the index stays ordered and covers the common
  ``(workspace_id, property_id)`` predicate.
* ``ix_occurrence_template`` — the ``template_id`` ``RESTRICT`` FK was
  unindexed, unlike sibling ``ix_schedule_template``; template
  soft-delete scans occurrences by this column.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "cd1hgd4occidx"
down_revision: str | Sequence[str] | None = "cddznzkmultiprop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        "ix_occurrence_workspace_property",
        "occurrence",
        ["workspace_id", "property_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_occurrence_template",
        "occurrence",
        ["template_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_occurrence_template", table_name="occurrence")
    op.drop_index("ix_occurrence_workspace_property", table_name="occurrence")
