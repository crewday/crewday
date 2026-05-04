"""Identity scheduler job bodies."""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.adapters.db.session import make_uow
from app.util.clock import Clock

_log = logging.getLogger("app.worker.scheduler")


def _make_user_workspace_refresh_body(clock: Clock) -> Callable[[], None]:
    """Build the user_workspace derive-refresh body (cd-yqm4).

    Factory rather than a bare module-level function so the body
    closes over the scheduler's injected :class:`Clock` — same
    rationale the sibling :func:`_make_llm_budget_refresh_body` and
    :func:`_make_idempotency_sweep_body` cite. The reconciler stamps
    new ``user_workspace.added_at`` rows with ``clock.now()``; under
    a :class:`~app.util.clock.FrozenClock` the heartbeat timestamp
    and the freshly-inserted ``added_at`` MUST line up so test
    fixtures can assert on the exact pair.

    The returned body:

    1. Opens its own UoW (one per tick) via
       :func:`app.adapters.db.session.make_uow`. Sibling bodies do
       the same (idempotency sweep, LLM-budget refresh, generator
       fan-out). The UoW commits on clean exit.
    2. Calls :func:`reconcile_user_workspace`, which runs under
       :func:`tenant_agnostic` internally — the reconciler operates
       across every workspace in a single pass, not per-workspace,
       so there is no fan-out loop here.
    3. Emits one structured-log event per tick:
       ``event="worker.identity.user_workspace.tick.summary"`` (INFO),
       carrying ``rows_inserted`` / ``rows_deleted`` /
       ``rows_source_flipped`` / ``upstream_pairs_seen``. Operator
       dashboards plot insert + delete rates separately so a sudden
       spike in deletes (mass revoke) is distinguishable from a
       backfill.

    Per-tenant SAVEPOINT isolation is unnecessary here because the
    reconciler is a single SQL pass — there is no per-workspace work
    that could fail in isolation. A SQL error fails the whole tick
    (the outer UoW rolls back), the next tick retries, and the
    heartbeat staleness window catches a permanently-stuck
    reconcile.

    The :func:`reconcile_user_workspace` import is deferred into the
    closure body so module import order stays robust: the
    reconciler drags in :mod:`app.adapters.db.places.models` and
    :mod:`app.adapters.db.workspace.models`, neither of which the
    standalone ``python -m app.worker`` entrypoint otherwise needs
    to start the heartbeat-only deployment.
    """

    def _body() -> None:
        # Deferred imports — see factory docstring rationale.
        from sqlalchemy.orm import Session as _Session

        from app.domain.identity.user_workspace_refresh import (
            reconcile_user_workspace,
        )

        now = clock.now()

        with make_uow() as session:
            # ``UnitOfWorkImpl.__enter__`` returns a ``DbSession``
            # protocol; the reconciler wants the concrete ``Session``.
            # Same isinstance narrowing the LLM-budget and generator
            # fan-out bodies use.
            assert isinstance(session, _Session)
            report = reconcile_user_workspace(session, now=now)

        _log.info(
            "user workspace identity reconciliation tick summary",
            extra={
                "event": "worker.identity.user_workspace.tick.summary",
                "rows_inserted": report.rows_inserted,
                "rows_deleted": report.rows_deleted,
                "rows_source_flipped": report.rows_source_flipped,
                "upstream_pairs_seen": report.upstream_pairs_seen,
            },
        )

    return _body
