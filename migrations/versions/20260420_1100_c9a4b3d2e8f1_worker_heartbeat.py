"""worker_heartbeat

Revision ID: c9a4b3d2e8f1
Revises: 7f2c5a8e9bd3
Create Date: 2026-04-20 11:00:00.000000

Adds the ``worker_heartbeat`` table — one row per named background
worker, bumped every 30 s. Consumed by the readiness probe
(``app.api.health``) to decide whether the worker process is alive
(§16 "Healthchecks"). The table is **not** workspace-scoped: workers
run once per deployment regardless of tenant count and the readyz
probe must work before any :class:`~app.tenancy.WorkspaceContext` is
resolvable.

The unique constraint on ``worker_name`` pins one row per worker;
writers ``INSERT ... ON CONFLICT DO UPDATE`` (Postgres) or
``INSERT OR REPLACE`` (SQLite) to keep the table constant-sized
across ticks.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9a4b3d2e8f1"
down_revision: str | Sequence[str] | None = "7f2c5a8e9bd3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "worker_heartbeat",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("worker_name", sa.String(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_heartbeat")),
        sa.UniqueConstraint("worker_name", name="uq_worker_heartbeat_worker_name"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("worker_heartbeat")
