"""Per-job failure tracking + killswitch state for ``wrap_job`` (cd-8euz).

Every scheduler tick wrapped by :func:`app.worker.scheduler.wrap_job`
goes through one of three transitions, each landing in the same row
of :class:`~app.adapters.db.ops.models.WorkerHeartbeat`:

* **Success**: :func:`record_success` advances ``heartbeat_at``,
  resets ``consecutive_failures`` to 0, and clears ``dead_at`` so a
  job that recovered without an operator reset (e.g. the upstream
  dependency came back) leaves the dead state on the next clean
  tick.
* **Failure**: :func:`record_failure` increments
  ``consecutive_failures``. The third consecutive failure writes one
  deployment-scope ``worker.job.repeated_failure`` audit row; the
  fifth flips ``dead_at`` to ``now`` and writes one
  ``worker.job.killed`` audit row. The audit volume is bounded by
  these two thresholds — a job that keeps failing past the kill is
  short-circuited by the wrapper before any further audit fires.
* **Reset**: :func:`reset_job` clears both columns and writes one
  ``worker.job.reset`` audit row. Driven by the
  ``crewday admin worker reset-job`` host CLI verb.

Each writer opens its own :class:`~app.adapters.db.session.UnitOfWorkImpl`
so the failure-state advance commits independently of any work-body
session — a body that failed mid-transaction must not roll back the
killswitch counter (it would let a flapping job loop forever).

See cd-8euz, ``docs/specs/16-deployment-operations.md`` §"Worker
process" §"Healthchecks", and the §02 "audit_log" entry on the
deployment-scope partition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import Insert, case, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.adapters.db.ops.models import WorkerHeartbeat
from app.adapters.db.session import make_uow
from app.audit import write_deployment_audit
from app.tenancy import tenant_agnostic
from app.util.clock import Clock
from app.util.ulid import new_ulid

__all__ = [
    "DEAD_THRESHOLD",
    "REPEATED_FAILURE_THRESHOLD",
    "FailureOutcome",
    "is_dead",
    "record_failure",
    "record_success",
    "reset_job",
]

_log = logging.getLogger(__name__)

# Number of consecutive failures that triggers the
# ``worker.job.repeated_failure`` audit row. Exactly one audit row
# fires when the counter transitions from 2 -> 3; subsequent failures
# under the kill threshold log only.
REPEATED_FAILURE_THRESHOLD: Final[int] = 3

# Number of consecutive failures that flips ``dead_at`` to ``now``.
# The wrapper short-circuits subsequent ticks until an operator runs
# ``crewday admin worker reset-job``. Five is comfortably past the
# repeated-failure audit (3) so an operator who notices the audit
# row in the activity feed has a window to investigate before the
# killswitch fires.
DEAD_THRESHOLD: Final[int] = 5

# Deployment-scope audit identity for cd-8euz worker-state writes.
# Mirrors the convention used by :mod:`app.admin.init` and
# :mod:`app.worker.jobs.common` — a zero-ULID sentinel pinned with
# ``actor_kind="system"`` so every operator dashboard pattern-matches
# on the same provenance shape.
_SYSTEM_ACTOR_ID: Final[str] = "00000000000000000000000000"
_AUDIT_ENTITY_KIND: Final[str] = "worker_job"


@dataclass(frozen=True, slots=True)
class FailureOutcome:
    """Return shape for :func:`record_failure`.

    Carries the ``consecutive_failures`` count the row now holds and
    the two boolean transition flags the caller logs alongside the
    metric increment. Both transitions are one-shot — the row tips
    into ``dead`` exactly once per ``DEAD_THRESHOLD`` crossing, and
    the audit row at the repeated-failure boundary fires exactly once
    per crossing of ``REPEATED_FAILURE_THRESHOLD``.
    """

    consecutive_failures: int
    repeated_failure_audit_emitted: bool
    killed: bool


def is_dead(*, job_id: str) -> bool:
    """Return True if ``job_id``'s row is currently in the ``dead`` state.

    Opens a fresh UoW so the read is independent of any work-body
    session. Reading a dead state is a hot-path call (one read per
    tick, every 30 s in the worst case), but the SELECT is keyed on
    the unique ``worker_name`` index — constant-time on every
    supported backend.
    """
    with make_uow() as session:
        assert isinstance(session, Session)
        return _is_dead_in_session(session, job_id=job_id)


def _is_dead_in_session(session: Session, *, job_id: str) -> bool:
    """Inner helper — read the ``dead_at`` column under the caller's session.

    Splits the read out so :func:`record_success` and
    :func:`record_failure` can reuse the lookup inside their existing
    UoW without opening a second one (the worker_heartbeat row is
    keyed on a unique index but a re-open would still cost a
    round-trip per tick).
    """
    with tenant_agnostic():
        stmt = select(WorkerHeartbeat.dead_at).where(
            WorkerHeartbeat.worker_name == job_id
        )
        dead_at = session.scalar(stmt)
    return dead_at is not None


def record_success(*, job_id: str, clock: Clock) -> None:
    """Advance ``heartbeat_at`` + clear failure state for ``job_id``.

    Called on a clean tick. The single dialect-native ``INSERT ...
    ON CONFLICT(worker_name) DO UPDATE`` statement keeps one row per
    ``worker_name`` for the deployment lifetime without racing the
    first-ever write. On an existing row, ``heartbeat_at`` advances
    to ``now``, ``consecutive_failures`` resets to 0, and ``dead_at``
    clears so a job that recovered without operator intervention
    (its upstream came back, the migration finished, etc.) leaves
    the dead state on the next clean tick.

    Does not commit — the caller's UoW owns the transaction
    boundary, mirroring :func:`upsert_heartbeat`.
    """
    now = clock.now()
    with make_uow() as session:
        assert isinstance(session, Session)
        _record_success_in_session(session, job_id=job_id, now=now)


def record_failure(*, job_id: str, clock: Clock) -> FailureOutcome:
    """Increment ``consecutive_failures`` + write threshold audits for ``job_id``.

    Returns the post-increment count and which boundary transitions
    fired. The audit rows are written via
    :func:`app.audit.write_deployment_audit` because worker-state
    rows are deployment-wide ops plumbing, not workspace-scoped.
    Their ``via='worker'`` provenance lets the
    ``GET /admin/api/v1/audit`` feed filter the worker-driven slice
    cleanly.

    Does not advance ``heartbeat_at`` — the staleness window is the
    backstop signal for a job that's been failing long enough to
    matter (`docs/specs/16-deployment-operations.md` §"Healthchecks").
    """
    now = clock.now()
    with make_uow() as session:
        assert isinstance(session, Session)
        return _record_failure_in_session(session, job_id=job_id, clock=clock, now=now)


def _record_success_in_session(
    session: Session,
    *,
    job_id: str,
    now: datetime,
) -> None:
    with tenant_agnostic():
        values = {
            "id": new_ulid(),
            "worker_name": job_id,
            "heartbeat_at": now,
            "consecutive_failures": 0,
            "dead_at": None,
        }
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            pg_stmt = pg_insert(WorkerHeartbeat).values(**values)
            stmt: Insert = pg_stmt.on_conflict_do_update(
                index_elements=["worker_name"],
                set_={
                    "heartbeat_at": pg_stmt.excluded.heartbeat_at,
                    "consecutive_failures": 0,
                    "dead_at": None,
                },
            )
        else:
            sqlite_stmt = sqlite_insert(WorkerHeartbeat).values(**values)
            stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=["worker_name"],
                set_={
                    "heartbeat_at": sqlite_stmt.excluded.heartbeat_at,
                    "consecutive_failures": 0,
                    "dead_at": None,
                },
            )
        session.execute(stmt)
        session.flush()


def _record_failure_in_session(
    session: Session,
    *,
    job_id: str,
    clock: Clock,
    now: datetime,
) -> FailureOutcome:
    with tenant_agnostic():
        values = {
            "id": new_ulid(),
            "worker_name": job_id,
            # heartbeat_at on a never-successful job is the tick
            # instant. Conflict updates deliberately leave it alone:
            # failed ticks must not keep the readiness probe fresh.
            "heartbeat_at": now,
            "consecutive_failures": 1,
            "dead_at": None,
        }
        incremented_count = WorkerHeartbeat.consecutive_failures + 1
        set_ = {
            "consecutive_failures": incremented_count,
            "dead_at": case(
                (
                    (WorkerHeartbeat.dead_at.is_(None))
                    & (incremented_count >= DEAD_THRESHOLD),
                    now,
                ),
                else_=WorkerHeartbeat.dead_at,
            ),
        }
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            pg_stmt = pg_insert(WorkerHeartbeat).values(**values)
            stmt: Insert = pg_stmt.on_conflict_do_update(
                index_elements=["worker_name"],
                set_=set_,
            )
        else:
            sqlite_stmt = sqlite_insert(WorkerHeartbeat).values(**values)
            stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=["worker_name"],
                set_=set_,
            )
        stmt = stmt.returning(WorkerHeartbeat.consecutive_failures)

        count = session.execute(stmt).scalar_one()

        repeated_audit = count == REPEATED_FAILURE_THRESHOLD
        killed = count == DEAD_THRESHOLD

        if repeated_audit:
            _write_state_audit(
                session,
                job_id=job_id,
                action="worker.job.repeated_failure",
                diff={
                    "job_id": job_id,
                    "consecutive_failures": count,
                    "threshold": REPEATED_FAILURE_THRESHOLD,
                },
                clock=clock,
            )

        if killed:
            _write_state_audit(
                session,
                job_id=job_id,
                action="worker.job.killed",
                diff={
                    "job_id": job_id,
                    "consecutive_failures": count,
                    "threshold": DEAD_THRESHOLD,
                },
                clock=clock,
            )

        session.flush()
        return FailureOutcome(
            consecutive_failures=count,
            repeated_failure_audit_emitted=repeated_audit,
            killed=killed,
        )


def reset_job(*, job_id: str, clock: Clock) -> bool:
    """Clear the ``dead_at`` flag + reset ``consecutive_failures`` for ``job_id``.

    Driven by ``crewday admin worker reset-job <job_id>``. Returns
    ``True`` when a row was found and reset; ``False`` when the
    ``worker_heartbeat`` row does not exist yet (a job that has
    never run cannot be dead, so the reset is a no-op).

    Writes a ``worker.job.reset`` deployment audit row whenever a
    real reset happened so an operator can grep the audit feed for
    the manual intervention.
    """
    now = clock.now()
    with make_uow() as session:
        assert isinstance(session, Session)
        with tenant_agnostic():
            stmt = select(WorkerHeartbeat).where(WorkerHeartbeat.worker_name == job_id)
            existing = session.scalars(stmt).one_or_none()
            if existing is None:
                return False

            previous_failures = existing.consecutive_failures
            previous_dead_at = existing.dead_at
            if previous_failures == 0 and previous_dead_at is None:
                # Nothing to reset; do not emit an audit row for a
                # no-op reset (operators run reset to clear a state
                # the row no longer holds).
                return True

            existing.consecutive_failures = 0
            existing.dead_at = None

            _write_state_audit(
                session,
                job_id=job_id,
                action="worker.job.reset",
                diff={
                    "job_id": job_id,
                    "previous_consecutive_failures": previous_failures,
                    "previous_dead": previous_dead_at is not None,
                },
                clock=clock,
            )
            session.flush()
    del now  # ``now`` is folded into the audit row's ``created_at``.
    return True


def _write_state_audit(
    session: Session,
    *,
    job_id: str,
    action: str,
    diff: dict[str, object],
    clock: Clock,
) -> None:
    """Append one deployment-scope audit row for a worker-state transition.

    Pins ``via='worker'`` (matching the §02 "audit_log" enum) so the
    admin audit feed can filter the worker-driven slice cleanly. The
    correlation id is a fresh ULID per write — there is no inbound
    request to pivot through, and a per-row id keeps the §15
    "Tamper detection" hash chain self-consistent.
    """
    write_deployment_audit(
        session,
        actor_id=_SYSTEM_ACTOR_ID,
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        correlation_id=new_ulid(clock=clock),
        entity_kind=_AUDIT_ENTITY_KIND,
        entity_id=job_id,
        action=action,
        diff=diff,
        via="worker",
        clock=clock,
    )
    _log.info(
        "worker job state audit written",
        extra={
            "event": "worker.job.state_audit",
            "job_id": job_id,
            "action": action,
        },
    )
