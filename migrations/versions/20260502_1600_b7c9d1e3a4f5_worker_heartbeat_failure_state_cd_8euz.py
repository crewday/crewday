"""worker_heartbeat_failure_state cd-8euz

Revision ID: b7c9d1e3a4f5
Revises: a6b8c0d2e4f7
Create Date: 2026-05-02 16:00:00.000000

Adds the cd-8euz job-failure tracking columns to ``worker_heartbeat``
so :func:`app.worker.scheduler.wrap_job` can durably count consecutive
failed ticks per job and tip a job into a ``dead`` state once the
killswitch threshold is crossed.

Shape additions on ``worker_heartbeat`` (both backfill-free):

* ``consecutive_failures INT NOT NULL DEFAULT 0`` — how many consecutive
  failed ticks have landed since the last successful run for this
  ``worker_name``. Resets to 0 on the next successful tick. The third
  consecutive failure emits a deployment-scope ``worker.job.repeated_failure``
  audit row; the fifth tips the row into ``dead`` and emits
  ``worker.job.killed``. Server-default 0 keeps the existing rows
  consistent without a one-off backfill on upgrade.
* ``dead_at TIMESTAMPTZ NULL`` — non-NULL marks the job as the killswitch
  has fired. The wrapper short-circuits the next tick (no body, no
  heartbeat advance, ``crewday_worker_jobs_total{status="dead"}``
  incremented) until an operator runs ``crewday admin worker reset-job
  <job_id>`` — which clears both columns and writes a
  ``worker.job.reset`` deployment audit. NULL is the live state; the
  column is nullable so every existing row stays live without a
  backfill.

The single :class:`WorkerHeartbeat` row per ``worker_name`` already
exists from cd-7c0p; this migration just widens the row with the new
state. Rolling back drops the columns, restoring the cd-7c0p shape.

See ``docs/specs/16-deployment-operations.md`` §"Worker process",
§"Healthchecks" (the ``MAX(heartbeat_at)`` readiness probe is unaffected
because the new columns sit alongside ``heartbeat_at``, not on top of
it).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c9d1e3a4f5"
down_revision: str | Sequence[str] | None = "a6b8c0d2e4f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``consecutive_failures`` + ``dead_at`` to ``worker_heartbeat``."""
    with op.batch_alter_table("worker_heartbeat", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "consecutive_failures",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "dead_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    """Drop the cd-8euz job-failure tracking columns."""
    with op.batch_alter_table("worker_heartbeat", schema=None) as batch_op:
        batch_op.drop_column("dead_at")
        batch_op.drop_column("consecutive_failures")
