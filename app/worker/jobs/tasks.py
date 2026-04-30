"""Task-domain worker fan-out bodies."""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import select

from app.adapters.db.session import make_uow
from app.util.clock import Clock
from app.worker.jobs.common import _demo_expired_workspace_ids, _system_actor_context

_log = logging.getLogger("app.worker.scheduler")


def _make_generator_fanout_body(clock: Clock) -> Callable[[], None]:
    """Build the hourly occurrence-generator fan-out body (cd-dcl2).

    Factory rather than a bare module-level function so the body
    closes over the scheduler's injected :class:`Clock` — the same
    rationale the sibling :func:`_make_llm_budget_refresh_body` and
    :func:`_make_idempotency_sweep_body` cite. The generator's
    ``now`` is derived from ``clock.now()`` inside each per-workspace
    call; reusing the scheduler's clock keeps the heartbeat timestamp
    and the generation horizon aligned under :class:`FrozenClock`.

    The returned body:

    1. Opens its own UoW (one per tick) via
       :func:`app.adapters.db.session.make_uow`. Sibling bodies do
       the same (``_make_idempotency_sweep_body``,
       ``_make_llm_budget_refresh_body``). The outer UoW commits on
       clean exit and rolls back on any uncaught exception; the
       per-workspace ``begin_nested`` SAVEPOINT below scopes the
       rollback so a broken tenant does not lose successful sibling
       writes.
    2. Enumerates every :class:`~app.adapters.db.workspace.models.Workspace`
       under :func:`tenant_agnostic`; the ``workspace`` table is the
       tenancy anchor and carries no ``workspace_id`` of its own.
       Demo-expired tenants (§24 "Garbage collection") are filtered
       upfront via :func:`_demo_expired_workspace_ids` so the per-
       workspace loop only touches live tenants.
    3. For each live workspace, binds a system-actor
       :class:`WorkspaceContext` to the ``current`` ContextVar and
       calls :func:`~app.worker.tasks.generator.generate_task_occurrences`.
       Wrapped in ``try / except Exception`` + ``begin_nested`` so a
       single raising workspace logs at WARNING and the loop
       continues — the spec's "per-workspace errors must not abort
       the tick" invariant.
    4. Emits structured log events:

       * ``event="worker.generator.workspace.tick"`` (INFO) — per
         workspace, with ``workspace_id``, ``workspace_slug``,
         ``schedules_walked``, ``tasks_created``,
         ``skipped_duplicate``, ``skipped_for_closure``. The
         per-workspace payload the cd-dcl2 acceptance criteria
         pin for log-based attribution.
       * ``event="worker.generator.workspace.failed"`` (WARNING) —
         per workspace, with ``workspace_id`` + the exception class
         name. Full traceback would be noisy at hourly cadence;
         operators can re-run with DEBUG logging if the root cause
         needs deeper plumbing.
       * ``event="worker.generator.tick.summary"`` (INFO) — once per
         tick, with ``total_workspaces`` (live + demo-expired
         enumerated), ``total_workspaces_skipped`` (demo-expired,
         not walked), ``total_workspaces_failed``,
         ``total_schedules_walked`` (sum of
         :attr:`GenerationReport.schedules_walked`),
         ``total_tasks_created`` (sum of
         :attr:`GenerationReport.tasks_created`),
         ``total_skipped_duplicate`` and ``total_skipped_for_closure``
         (sums of the matching :class:`GenerationReport` fields). The
         per-component split is the reason the per-workspace event
         pins the same shape — operator dashboards plot rate of
         duplicate skips (idempotency proof) separately from closure
         skips (suppression proof).

    The import of :func:`generate_task_occurrences` is deferred into
    the closure body so module import order stays robust: the
    generator drags in :mod:`dateutil.rrule`,
    :mod:`app.adapters.db.tasks.models`, and the audit writer, none
    of which the standalone ``python -m app.worker`` entrypoint
    needs to start the heartbeat-only deployment.
    """

    def _body() -> None:
        # Deferred imports — see factory docstring rationale. Keep
        # them narrow so a worker process whose generator path fails
        # to import still boots the heartbeat + idempotency-sweep
        # ticks (the standalone worker's import surface stays lean).
        from sqlalchemy.orm import Session as _Session

        from app.adapters.db.workspace.models import Workspace
        from app.tenancy import tenant_agnostic
        from app.tenancy.current import reset_current, set_current
        from app.worker.tasks.generator import generate_task_occurrences

        now = clock.now()

        total_workspaces = 0
        total_workspaces_skipped = 0
        total_workspaces_failed = 0
        total_schedules_walked = 0
        total_tasks_created = 0
        total_skipped_duplicate = 0
        total_skipped_for_closure = 0

        with make_uow() as session:
            # Same isinstance narrowing the LLM-budget body uses —
            # ``UnitOfWorkImpl.__enter__`` returns a ``DbSession``
            # protocol; the fan-out hands the concrete ``Session``
            # to :func:`generate_task_occurrences`.
            assert isinstance(session, _Session)

            with tenant_agnostic():
                # ``workspace`` is NOT in the tenant-filter registry
                # (it is the tenancy anchor; see
                # ``app/adapters/db/workspace/__init__.py``); the
                # ``tenant_agnostic`` block is belt-and-braces in
                # case a future migration registers the table.
                rows = list(session.execute(select(Workspace.id, Workspace.slug)).all())
                workspace_ids = [row.id for row in rows]
                expired_ids = _demo_expired_workspace_ids(
                    session, workspace_ids, now=now
                )

            for row in rows:
                workspace_id = row.id
                workspace_slug = row.slug
                total_workspaces += 1

                if workspace_id in expired_ids:
                    # §24 "Garbage collection" — workspaces past
                    # ``expires_at`` are awaiting the ``demo_gc``
                    # sweep; running materialisation on them is
                    # wasted work and would race the GC. Counted
                    # toward ``total_workspaces_skipped`` so the
                    # tick summary keeps the demo-fleet attrition
                    # observable.
                    total_workspaces_skipped += 1
                    continue

                ctx = _system_actor_context(
                    workspace_id=workspace_id,
                    workspace_slug=workspace_slug,
                )
                # Tenant filter reads the ``current`` ContextVar on
                # every scoped SELECT; the generator's ``Schedule``
                # / ``TaskTemplate`` / ``Occurrence`` reads + writes
                # would raise
                # :class:`~app.tenancy.orm_filter.TenantFilterMissing`
                # without an active context. ``try / finally``
                # guarantees the token is reset even if the body
                # raises before the SAVEPOINT catches — without it
                # the ContextVar would leak into the next iteration
                # and the next workspace's run would see a stale
                # ctx.
                token = set_current(ctx)
                try:
                    try:
                        # Per-workspace SAVEPOINT scopes the rollback
                        # of any partial occurrence inserts to this
                        # tenant — sibling workspaces' successful
                        # writes ride the outer transaction unharmed.
                        # The same pattern the LLM-budget refresh
                        # body uses.
                        with session.begin_nested():
                            report = generate_task_occurrences(
                                ctx,
                                session=session,
                                clock=clock,
                            )
                    except Exception as exc:
                        # SAVEPOINT already rolled back by the context
                        # manager; the outer transaction is still
                        # usable. Log at WARNING with the exception
                        # class name — full traceback would be noisy
                        # at hourly cadence.
                        total_workspaces_failed += 1
                        _log.warning(
                            "worker.generator.workspace.failed",
                            extra={
                                "event": "worker.generator.workspace.failed",
                                "workspace_id": workspace_id,
                                "workspace_slug": workspace_slug,
                                "error": type(exc).__name__,
                            },
                        )
                        continue
                finally:
                    reset_current(token)

                total_schedules_walked += report.schedules_walked
                total_tasks_created += report.tasks_created
                total_skipped_duplicate += report.skipped_duplicate
                total_skipped_for_closure += report.skipped_for_closure

                _log.info(
                    "worker.generator.workspace.tick",
                    extra={
                        "event": "worker.generator.workspace.tick",
                        "workspace_id": workspace_id,
                        "workspace_slug": workspace_slug,
                        "schedules_walked": report.schedules_walked,
                        "tasks_created": report.tasks_created,
                        "skipped_duplicate": report.skipped_duplicate,
                        "skipped_for_closure": report.skipped_for_closure,
                    },
                )

        _log.info(
            "worker.generator.tick.summary",
            extra={
                "event": "worker.generator.tick.summary",
                "total_workspaces": total_workspaces,
                "total_workspaces_skipped": total_workspaces_skipped,
                "total_workspaces_failed": total_workspaces_failed,
                "total_schedules_walked": total_schedules_walked,
                "total_tasks_created": total_tasks_created,
                "total_skipped_duplicate": total_skipped_duplicate,
                "total_skipped_for_closure": total_skipped_for_closure,
            },
        )

    return _body


def _make_overdue_fanout_body(clock: Clock) -> Callable[[], None]:
    """Build the 5-minute soft-overdue sweeper fan-out body (cd-hurw).

    Mirror of :func:`_make_generator_fanout_body` for the overdue
    sweeper: enumerate every live workspace, bind a system-actor
    :class:`WorkspaceContext`, run
    :func:`~app.worker.tasks.overdue.detect_overdue` per tenant inside
    a SAVEPOINT so a single broken workspace does not roll back its
    siblings' updates. Demo-expired workspaces are skipped (same §24
    rationale the generator fan-out cites).

    Structured-log emission:

    * ``event="worker.overdue.workspace.tick"`` (INFO) — per workspace,
      with ``workspace_id``, ``workspace_slug``, ``flipped_count``,
      ``skipped_already_overdue``, ``skipped_manual_transition``. The
      per-workspace payload operator dashboards key on for "which
      tenants are stacking overdue tasks?".
    * ``event="worker.overdue.workspace.failed"`` (WARNING) — per
      workspace, with ``workspace_id`` + the exception class name.
    * ``event="worker.overdue.tick.summary"`` (INFO) — once per tick,
      with ``total_workspaces``, ``total_workspaces_skipped`` (demo-
      expired), ``total_workspaces_failed``, ``total_flipped``,
      ``total_skipped_manual_transition``. Sums of the matching
      :class:`OverdueReport` fields.

    The :func:`detect_overdue` import is deferred into the closure
    body so module import order stays robust — same pattern the
    sibling generator fan-out uses.
    """

    def _body() -> None:
        from sqlalchemy.orm import Session as _Session

        from app.adapters.db.workspace.models import Workspace
        from app.tenancy import tenant_agnostic
        from app.tenancy.current import reset_current, set_current
        from app.worker.tasks.overdue import detect_overdue

        now = clock.now()

        total_workspaces = 0
        total_workspaces_skipped = 0
        total_workspaces_failed = 0
        total_flipped = 0
        total_skipped_manual_transition = 0

        with make_uow() as session:
            assert isinstance(session, _Session)

            with tenant_agnostic():
                rows = list(session.execute(select(Workspace.id, Workspace.slug)).all())
                workspace_ids = [row.id for row in rows]
                expired_ids = _demo_expired_workspace_ids(
                    session, workspace_ids, now=now
                )

            for row in rows:
                workspace_id = row.id
                workspace_slug = row.slug
                total_workspaces += 1

                if workspace_id in expired_ids:
                    total_workspaces_skipped += 1
                    continue

                ctx = _system_actor_context(
                    workspace_id=workspace_id,
                    workspace_slug=workspace_slug,
                )
                token = set_current(ctx)
                try:
                    try:
                        with session.begin_nested():
                            report = detect_overdue(
                                ctx,
                                session=session,
                                clock=clock,
                            )
                    except Exception as exc:
                        total_workspaces_failed += 1
                        _log.warning(
                            "worker.overdue.workspace.failed",
                            extra={
                                "event": "worker.overdue.workspace.failed",
                                "workspace_id": workspace_id,
                                "workspace_slug": workspace_slug,
                                "error": type(exc).__name__,
                            },
                        )
                        continue
                finally:
                    reset_current(token)

                total_flipped += report.flipped_count
                total_skipped_manual_transition += report.skipped_manual_transition

                _log.info(
                    "worker.overdue.workspace.tick",
                    extra={
                        "event": "worker.overdue.workspace.tick",
                        "workspace_id": workspace_id,
                        "workspace_slug": workspace_slug,
                        "flipped_count": report.flipped_count,
                        "skipped_already_overdue": report.skipped_already_overdue,
                        "skipped_manual_transition": (report.skipped_manual_transition),
                    },
                )

        _log.info(
            "worker.overdue.tick.summary",
            extra={
                "event": "worker.overdue.tick.summary",
                "total_workspaces": total_workspaces,
                "total_workspaces_skipped": total_workspaces_skipped,
                "total_workspaces_failed": total_workspaces_failed,
                "total_flipped": total_flipped,
                "total_skipped_manual_transition": total_skipped_manual_transition,
            },
        )

    return _body
