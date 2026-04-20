"""``worker_heartbeat`` — liveness row per background worker.

One row per named worker (``"scheduler"``, ``"llm_usage"``, ...). The
worker bumps ``heartbeat_at`` every 30 s; the ``/readyz`` probe
(:mod:`app.api.health`) reads the most-recent ``heartbeat_at`` and
fails readiness if no row is newer than 60 s (spec §16
"Healthchecks"). A stale or absent heartbeat keeps the reverse proxy
draining traffic until the worker catches up.

**Not workspace-scoped.** The table is deployment-wide: a worker runs
once per process regardless of tenant count, and the readyz probe
looks at it before any :class:`~app.tenancy.WorkspaceContext` is
available. Writers (the worker task itself) wrap their ``UPDATE`` in
:func:`app.tenancy.tenant_agnostic` with an explicit justification.

See ``docs/specs/16-deployment-operations.md`` §"Healthchecks" and
``docs/specs/01-architecture.md`` §"Key runtime invariants".
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = ["WorkerHeartbeat"]


class WorkerHeartbeat(Base):
    """One liveness row per named background worker.

    ``worker_name`` is a stable short identifier (``"scheduler"``,
    ``"llm_usage"``, ``"email_outbox"``, ...) — kept as free-form
    ``String`` rather than an enum so adding a new worker is a code
    diff, not a migration. ``heartbeat_at`` is aware UTC (§01 "Time is
    UTC at rest").

    The row is upserted — one row per worker for the lifetime of the
    deployment, not one row per tick; the table stays constant-sized
    and cleanup is unnecessary.
    """

    __tablename__ = "worker_heartbeat"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    worker_name: Mapped[str] = mapped_column(String, nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("worker_name", name="uq_worker_heartbeat_worker_name"),
    )
