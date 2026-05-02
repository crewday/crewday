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
of the deployment. The write is a single dialect-native
``INSERT ... ON CONFLICT(worker_name) DO UPDATE SET heartbeat_at =
excluded.heartbeat_at`` so two schedulers racing the first-ever
write of a given ``worker_name`` cannot trip the
``UniqueConstraint`` backstop with an :class:`~sqlalchemy.exc.IntegrityError`
— Recipe D self-hosted-SaaS deployments may horizontally scale the
worker container, and a misconfigured deployment that runs both an
in-process scheduler and a sibling worker container would otherwise
race on the same row. The conflict-update set deliberately touches
**only** ``heartbeat_at``: ``consecutive_failures`` and ``dead_at``
(cd-8euz) are owned by :mod:`app.worker.job_state` and must survive
the upsert untouched.

See ``docs/specs/16-deployment-operations.md`` §"Healthchecks" and
``docs/specs/01-architecture.md`` §"Worker".
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.adapters.db.ops.models import WorkerHeartbeat
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid

__all__ = ["upsert_heartbeat"]


def upsert_heartbeat(
    session: Session,
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

    Implemented as one dialect-native ``INSERT ... ON CONFLICT DO
    UPDATE`` so two processes racing the first-ever write of
    ``worker_name`` cannot hit ``IntegrityError`` on the
    ``UniqueConstraint`` backstop. The conflict-update set is
    deliberately narrow: only ``heartbeat_at`` is touched on
    update. The cd-8euz columns (``consecutive_failures``,
    ``dead_at``) are owned by :mod:`app.worker.job_state` and must
    not be reset by a successful liveness tick.
    """
    # justification: worker_heartbeat is deployment-wide ops plumbing
    # (not workspace-scoped); scheduler ticks run outside any
    # WorkspaceContext so the tenancy filter has nothing to inject.
    with tenant_agnostic():
        values = {
            "id": new_ulid(),
            "worker_name": worker_name,
            "heartbeat_at": now,
        }
        # ``get_bind()`` is the canonical public API and matches the
        # other dialect-dispatch sites in the repo (see e.g.
        # ``app.worker.tasks.generator._build_occurrence_insert``,
        # ``app.domain.identity._owner_guard``, ``app.domain.llm.budget``).
        # It raises ``UnboundExecutionError`` if no bind is reachable
        # — the right loud failure for a worker tick that can't tell
        # its dialect.
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            pg_stmt = pg_insert(WorkerHeartbeat).values(**values)
            stmt: Insert = pg_stmt.on_conflict_do_update(
                index_elements=["worker_name"],
                # ``excluded`` is the would-have-been-inserted row;
                # mirroring the SQL syntax keeps the statement readable
                # and guarantees the UPDATE picks up the freshly-passed
                # ``now`` (not whatever ``heartbeat_at`` happened to be
                # in the existing row).
                set_={"heartbeat_at": pg_stmt.excluded.heartbeat_at},
            )
        else:
            sqlite_stmt = sqlite_insert(WorkerHeartbeat).values(**values)
            stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=["worker_name"],
                set_={"heartbeat_at": sqlite_stmt.excluded.heartbeat_at},
            )
        session.execute(stmt)
        session.flush()
