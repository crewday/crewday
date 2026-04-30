"""LLM budget scheduler job body."""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.adapters.db.session import make_uow
from app.util.clock import Clock
from app.worker.jobs.common import _system_actor_context

_log = logging.getLogger("app.worker.scheduler")


def _make_llm_budget_refresh_body(clock: Clock) -> Callable[[], None]:
    """Build the 60 s LLM-budget refresh body (cd-ca1k).

    Factory rather than a bare module-level function so the body
    closes over the scheduler's injected :class:`Clock` — the
    ``refresh_aggregate`` window bounds are derived from
    ``clock.now() - 30d``, which MUST be driven by the same clock
    the heartbeat uses. A :class:`~app.util.clock.FrozenClock` under
    test otherwise would have a deterministic heartbeat timestamp
    and a non-deterministic refresh window; trivially cheap closure,
    no reason to tolerate the mismatch.

    The returned body:

    1. Opens its own UoW (one per tick) via
       :func:`app.adapters.db.session.make_uow`. ``wrap_job`` does
       not hand in a session — every sibling body opens its own
       (``_make_idempotency_sweep_body`` does the same). This keeps
       the per-tick transaction boundary explicit: a broken workspace
       does not poison the session for its siblings because the
       outer UoW rolls back only on an un-caught exception (this
       body catches per-workspace).
    2. Queries every :class:`~app.adapters.db.workspace.models.Workspace`
       row. No ``archived_at`` column exists yet (tracked as a
       Beads follow-up); for now "active" == "row exists".
    3. For each workspace, constructs a system-actor
       :class:`WorkspaceContext` and calls
       :func:`~app.domain.llm.budget.refresh_aggregate`. Wrapped in
       ``try / except Exception`` so a single broken workspace
       doesn't starve the fan-out.
    4. Emits structured log events the operator dashboard keys on:

       * ``event="llm.budget.refresh.no_ledger"`` (DEBUG) — per
         workspace, when :func:`refresh_aggregate` returns 0 and no
         ledger row exists. Logged at DEBUG because the workspace-
         create handler seeds the ledger (cd-tubi); the DEBUG line
         lets an operator trace the skip without alerting.
       * ``event="llm.budget.refresh.workspace_failed"`` (WARNING) —
         per workspace, with the exception class name. The exception
         is swallowed here; the outer tick continues.
       * ``event="llm.budget.refresh.tick"`` (INFO) — once per tick,
         with ``workspaces`` (count attempted), ``failures`` (count
         raising), and ``total_cents`` (sum of freshly-computed
         aggregates across every workspace that returned a value).
         Health metric for operator dashboards — NOT a cap check.

    The import of :func:`refresh_aggregate` is deferred into the
    closure body so module import order stays robust: the budget
    module drags in :mod:`app.adapters.db.llm.models`, which is
    unnecessary for the standalone ``python -m app.worker``
    entrypoint that only needs the scheduler seam.
    """

    def _body() -> None:
        # Deferred imports — see factory docstring rationale. Keep
        # them narrow so a worker process that fails to start the
        # budget job still boots (the heartbeat + idempotency sweep
        # ride a separate import path). ``make_uow`` is already
        # imported at module scope (the heartbeat writer uses it);
        # the budget / workspace / tenancy imports are the ones we
        # defer to keep the standalone worker's import surface lean.
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from app.adapters.db.llm.models import BudgetLedger
        from app.adapters.db.workspace.models import Workspace
        from app.domain.llm.budget import refresh_aggregate
        from app.tenancy import tenant_agnostic
        from app.tenancy.current import reset_current, set_current

        workspaces_attempted = 0
        failures = 0
        # ``total_cents`` is the sum of freshly-computed aggregates
        # across every workspace the tick actually refreshed. Python
        # ``int`` is arbitrary-precision so overflow is impossible;
        # downstream log serializers see a plain integer.
        total_cents = 0

        with make_uow() as session:
            # ``UnitOfWorkImpl.__enter__`` returns the concrete
            # :class:`~sqlalchemy.orm.Session` under a :class:`DbSession`
            # protocol annotation; :func:`refresh_aggregate` wants the
            # concrete class. Narrow with an ``isinstance`` assertion
            # — same pattern as
            # :func:`app.api.middleware.idempotency.prune_expired_idempotency_keys`.
            assert isinstance(session, Session)

            # ``workspace`` is NOT in the tenant-filter registry
            # (it is the tenancy anchor; see
            # ``app/adapters/db/workspace/__init__.py``) — no
            # ``workspace_id`` predicate is injected, so a plain
            # SELECT returns every tenant row. Wrap in
            # ``tenant_agnostic()`` as belt-and-braces in case a
            # future migration registers the table.
            # justification: scheduler fan-out must enumerate every
            # tenant's workspace row before binding a per-workspace
            # ctx; the ``workspace`` table is the tenancy anchor and
            # carries no ``workspace_id`` column of its own.
            with tenant_agnostic():
                rows = list(session.execute(select(Workspace.id, Workspace.slug)).all())

            for row in rows:
                workspace_id = row.id
                workspace_slug = row.slug
                workspaces_attempted += 1
                ctx = _system_actor_context(
                    workspace_id=workspace_id,
                    workspace_slug=workspace_slug,
                )
                # Tenant filter reads the ``current`` ContextVar on
                # every scoped SELECT; :func:`refresh_aggregate`'s
                # ``_sum_usage_cents`` hits the workspace-scoped
                # ``llm_usage`` table and would raise
                # :class:`~app.tenancy.orm_filter.TenantFilterMissing`
                # without an active context. Scope the bind to the
                # single ``refresh_aggregate`` call so a later fan-out
                # step (or the tick's INFO log emit) never runs with
                # a lingering per-workspace ctx. ``try / finally``
                # guarantees the token is reset even if the body
                # below raises before the SAVEPOINT catches — without
                # it the ContextVar would leak into the next iteration
                # and the next workspace's refresh would see a stale
                # ctx.
                token = set_current(ctx)
                try:
                    # Per-workspace SAVEPOINT wraps BOTH the ledger
                    # pre-check AND the refresh call. Two reasons:
                    #
                    # 1. A crashing workspace must not roll back a
                    #    sibling's UPDATE. The outer UoW owns the
                    #    top-level transaction (commit on clean exit,
                    #    rollback on exception); the nested
                    #    ``session.begin_nested()`` lets us roll back
                    #    only this workspace's work on failure while
                    #    keeping the preceding workspaces' updates
                    #    live. Without this scope, a single poisoned
                    #    refresh would take down the entire fan-out's
                    #    progress at commit time.
                    # 2. If the pre-check SELECT itself raises (bad
                    #    connection, malformed row, tenant-filter
                    #    misconfiguration), we MUST treat it as a
                    #    per-workspace failure — not an unhandled
                    #    exception that skips the tick-summary INFO
                    #    emit and leaves the ``/readyz`` heartbeat
                    #    silently disconnected from actual progress.
                    try:
                        with session.begin_nested():
                            # Pre-check the ledger row existence BEFORE
                            # calling :func:`refresh_aggregate`. The
                            # domain function returns ``0`` for two
                            # distinct shapes:
                            #   (a) no ledger row — the seeding bug
                            #       cd-tubi tracks; the workspace-
                            #       create handler has not yet run
                            #       (or has a bug).
                            #   (b) a ledger row whose in-window usage
                            #       sums to zero — a perfectly healthy
                            #       zero-spend workspace.
                            # Conflating the two at the DEBUG log level
                            # makes ``event=llm.budget.refresh.no_ledger``
                            # useless for the seeding-bug dashboard
                            # cd-tubi is meant to drive — a fleet with
                            # ten healthy zero-spend tenants would page
                            # the same signal as a single broken seed
                            # path. Pre-checking disambiguates the two
                            # signals and skips the domain call (and
                            # its redundant
                            # ``llm.budget.ledger_missing_on_refresh``
                            # WARNING) entirely on path (a).
                            #
                            # The ledger probe is itself a workspace-
                            # scoped SELECT — ``budget_ledger`` is in
                            # the tenancy registry — so it runs INSIDE
                            # the ``set_current`` / ``reset_current``
                            # bracket, not before.
                            ledger_exists = (
                                session.scalar(
                                    select(BudgetLedger.id)
                                    .where(BudgetLedger.workspace_id == workspace_id)
                                    .limit(1)
                                )
                                is not None
                            )
                            if not ledger_exists:
                                result = None
                            else:
                                result = refresh_aggregate(session, ctx, clock=clock)
                    except Exception as exc:
                        # SAVEPOINT already rolled back by the context
                        # manager; the outer transaction is still
                        # usable. Log at WARNING with the exception
                        # class name (full traceback would be noisy
                        # at 60 s cadence; operators can ``grep`` for
                        # the event and re-run with DEBUG logging if
                        # the root cause needs deeper plumbing).
                        failures += 1
                        _log.warning(
                            "llm.budget.refresh.workspace_failed",
                            extra={
                                "event": "llm.budget.refresh.workspace_failed",
                                "workspace_id": workspace_id,
                                "error": type(exc).__name__,
                            },
                        )
                        continue
                finally:
                    reset_current(token)

                if result is None:
                    # Pre-check saw a missing ledger — the
                    # seeding-bug signal (cd-tubi). DEBUG so the
                    # fan-out trace stays complete without paging
                    # on a fleet of healthy tenants.
                    _log.debug(
                        "llm.budget.refresh.no_ledger",
                        extra={
                            "event": "llm.budget.refresh.no_ledger",
                            "workspace_id": workspace_id,
                        },
                    )
                    continue

                total_cents += result

        _log.info(
            "llm.budget.refresh.tick",
            extra={
                "event": "llm.budget.refresh.tick",
                "workspaces": workspaces_attempted,
                "failures": failures,
                "total_cents": total_cents,
            },
        )

    return _body
