"""Worker heartbeat upsert — one row per named job, read by ``/readyz``.

Every scheduled job wrapped by :func:`app.worker.scheduler.wrap_job`
upserts a :class:`~app.adapters.db.ops.models.WorkerHeartbeat` row
keyed by ``worker_name`` on every successful run. The health probe at
``/readyz`` (see :mod:`app.api.health`) reads
``MAX(heartbeat_at)`` across every row; a fresh tick from any job
flips readiness green.

**Not workspace-scoped.** ``worker_heartbeat`` is deployment-wide
ops plumbing (see the ORM model docstring); writes run inside
:func:`app.tenancy.tenant_agnostic` with an explicit justification
so the tenancy filter leaves the query alone.

**Idempotency model.** One row per ``worker_name`` for the lifetime
of the deployment: SELECT-then-INSERT on first call, UPDATE on every
subsequent call. The ``UniqueConstraint`` on ``worker_name`` is the
backstop — a concurrent INSERT race would trip the constraint and
surface as :class:`sqlalchemy.exc.IntegrityError`; the scheduler is
single-process so the SELECT-then-INSERT window is narrow in
practice, but we still wrap the write in an explicit transaction so
the probe never sees a partially-written row.

See ``docs/specs/16-deployment-operations.md`` §"Healthchecks" and
``docs/specs/01-architecture.md`` §"Worker".
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.adapters.db.ops.models import WorkerHeartbeat
from app.adapters.db.ports import DbSession
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid

__all__ = ["upsert_heartbeat"]


def upsert_heartbeat(
    session: DbSession,
    *,
    worker_name: str,
    now: datetime,
) -> None:
    """Upsert ``worker_heartbeat`` keyed by ``worker_name``.

    Inserts a fresh row on first call for the name; updates
    ``heartbeat_at`` on every subsequent call. Runs inside
    :func:`tenant_agnostic` because the table has no
    ``workspace_id`` column and the read would otherwise be
    filtered out by the tenancy ORM hook.

    ``now`` is passed in (never read from the OS clock here) so the
    caller's :class:`~app.util.clock.Clock` port stays the single
    source of truth and tests drive the upsert deterministically.

    Does **not** commit — the caller's unit-of-work owns the
    transaction boundary, matching §01 "Key runtime invariants" #3.
    The scheduler wrapper opens a fresh UoW per tick and commits on
    clean exit.
    """
    # justification: worker_heartbeat is deployment-wide ops plumbing
    # (not workspace-scoped); scheduler ticks run outside any
    # WorkspaceContext so the tenancy filter has nothing to inject.
    with tenant_agnostic():
        stmt = select(WorkerHeartbeat).where(WorkerHeartbeat.worker_name == worker_name)
        existing = session.scalars(stmt).one_or_none()
        if existing is None:
            session.add(
                WorkerHeartbeat(
                    id=new_ulid(),
                    worker_name=worker_name,
                    heartbeat_at=now,
                )
            )
        else:
            existing.heartbeat_at = now
        session.flush()
