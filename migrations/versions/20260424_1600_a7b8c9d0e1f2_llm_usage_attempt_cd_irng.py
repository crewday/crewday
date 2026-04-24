"""llm_usage_attempt_cd_irng

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-04-24 16:00:00.000000

Adds the idempotency key to ``llm_usage`` that cd-irng's
``record_usage`` needs to make retried post-flight writes a no-op
instead of a double-count on the rolling 30-day workspace envelope
(§11 "Workspace usage budget").

Shape additions on ``llm_usage``:

* ``attempt INT NOT NULL DEFAULT 0`` — retry index within a single
  ``(workspace_id, correlation_id)`` operation. 0 = first attempt; the
  fallback-chain walker (§11 "Failure modes") bumps this on every
  rung. The server default keeps the column backward-compatible with
  pre-cd-irng rows the cd-cm5 slice already wrote.
* Unique index
  ``uq_llm_usage_workspace_correlation_attempt`` on
  ``(workspace_id, correlation_id, attempt)`` — ``record_usage``
  relies on this unique to turn a concurrent retry into a single row:
  the second ``INSERT`` fails with a ``UniqueViolationError`` the
  service layer catches and treats as "already recorded", so the
  budget aggregate is never double-bumped.

Why not a compound PK? The existing PK is a ULID; callers (the
``/admin/usage`` feed, the router observability seam, cd-wjpl's
``llm_call`` follow-up) reach rows by that id, and switching to a
composite would break every downstream reader. A unique *secondary*
index carries the idempotency contract without rewiring primary-key
semantics.

**Reversibility.** ``downgrade()`` drops the unique index first and
then the column. No backfill concern: ``attempt`` defaults to ``0``
on every existing row, and callers pre-cd-irng never wrote the
column, so rolling back is a clean structural reverse.

See ``docs/specs/02-domain-model.md`` §"LLM" §"llm_usage",
``docs/specs/11-llm-and-agents.md`` §"Workspace usage budget"
§"At-cap behaviour" (idempotent post-flight writes).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add the column + unique index in one ``batch_alter_table`` so
    # SQLite materialises both changes through a single table-copy
    # rather than one per op. On PG this renders as plain
    # ``ALTER TABLE`` statements in sequence.
    with op.batch_alter_table("llm_usage", schema=None) as batch_op:
        # ``attempt`` — retry index within one logical call. Default
        # 0 = first attempt, matching the pre-cd-irng implicit shape
        # the cd-cm5 slice already wrote. Non-null so the unique
        # index below partitions cleanly (``NULL`` would let multiple
        # rows share the "same" key on PG, which would defeat the
        # idempotency guard).
        batch_op.add_column(
            sa.Column(
                "attempt",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )

        # Idempotency guard: ``record_usage`` catches the integrity
        # error this unique raises on a retry and treats the second
        # write as a no-op. The leading ``workspace_id`` carries the
        # tenant filter; ``correlation_id`` ties the rung to its
        # logical operation; ``attempt`` disambiguates rungs that
        # share a correlation id.
        batch_op.create_index(
            "uq_llm_usage_workspace_correlation_attempt",
            ["workspace_id", "correlation_id", "attempt"],
            unique=True,
        )


def downgrade() -> None:
    """Downgrade schema.

    FK-safe order: drop the unique index first so the subsequent
    column drop doesn't trip an orphaned index reference on SQLite's
    batch rebuild path.
    """
    with op.batch_alter_table("llm_usage", schema=None) as batch_op:
        batch_op.drop_index("uq_llm_usage_workspace_correlation_attempt")
        batch_op.drop_column("attempt")
