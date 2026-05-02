"""``sweep_expired_invites`` — invite TTL expiry worker tick (cd-za45).

Walks every ``invite`` row in ``state='pending'`` whose ``expires_at``
has slipped below ``now`` and flips it to ``state='expired'``. The §03
"Additional users (invite → click-to-accept)" §"TTL" rule pins this
as the deployment-wide sweep that closes the loop on stale invites so
the manager workspace-members surface stops showing rows nobody can
act on.

Cross-tenant by design — the sweep runs once per tick at the
deployment level (NOT per workspace) because:

1. The TTL is a global policy, not a per-tenant one. The 24-hour
   ``grant_invite`` magic-link TTL is the same in workspace A as in
   workspace B, so one cutoff drives every row.
2. The domain-layer :func:`prune_stale_invites` already runs its load
   query under :func:`tenant_agnostic` and re-emits one
   :class:`~app.events.types.InviteExpired` per row keyed by the row's
   own ``workspace_id`` — the SSE transport's per-workspace + role
   filter routes the event to the right subscribers without the
   worker needing to know tenancy.
3. Per-workspace fan-out would scale linearly with workspace count
   and dominate a fleet's worker budget; one global sweep is O(n) in
   the count of expired rows, not in the count of workspaces.

The worker:

1. Opens a fresh UoW (its own transaction).
2. Calls :func:`app.domain.identity.membership.prune_stale_invites`
   with ``now=clock.now()``. The domain layer flips state and
   publishes :class:`~app.events.types.InviteExpired` per row.
3. Commits the UoW.
4. Logs the report at INFO with ``event=invite.ttl.sweep`` so
   operators can correlate the count + ids with the manager
   members surface depth on a dashboard.

A failed sweep raises out of the wrapper; the scheduler's
:func:`~app.worker.scheduler.wrap_job` catches + logs the
exception, the heartbeat row stops advancing, and ``/readyz``
goes red via the staleness window — the natural escalation
signal. The next tick (15 min later) retries the same set
because the rows are still in ``pending`` state.

See ``docs/specs/03-auth-and-tokens.md`` §"Additional users" §"TTL"
and ``docs/specs/16-deployment-operations.md`` §"Worker process".
"""

from __future__ import annotations

import logging
from typing import Final

from sqlalchemy.orm import Session

from app.adapters.db.session import make_uow
from app.domain.identity.membership import (
    PruneStaleInvitesReport,
    prune_stale_invites,
)
from app.util.clock import Clock, SystemClock

__all__ = [
    "sweep_expired_invites",
]


_log = logging.getLogger(__name__)


# Structured-log event name. Pinned as a constant so the operator
# dashboard's log filter and the worker entry-point reference one
# string — a rename here ripples to one filter line, not a regex.
_LOG_EVENT: Final[str] = "invite.ttl.sweep"


def sweep_expired_invites(*, clock: Clock | None = None) -> PruneStaleInvitesReport:
    """Run one invite TTL-expiry sweep across the deployment.

    Opens a fresh UoW (the worker has no ambient session) and calls
    :func:`prune_stale_invites` with ``now=clock.now()``. Commits on
    success; rolls back on any exception (the UoW's ``__exit__``
    handles both branches).

    Returns the :class:`PruneStaleInvitesReport` so the scheduler
    wrapper can log + emit metrics. An empty sweep is a no-op (no
    events) and returns a report with ``expired_count=0``.

    The clock is injectable so tests can drive the sweep
    deterministically; production passes ``None`` and falls back to
    :class:`SystemClock`. Matches the pattern in the sibling worker
    :func:`app.worker.tasks.approval_ttl.sweep_expired_approvals`.
    """
    resolved_clock: Clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    with make_uow() as session:
        # ``DbSession`` is the read-side Protocol; the concrete UoW
        # always yields a real :class:`Session`. The domain helper
        # signature is concrete (it issues writes), so narrow here at
        # the seam rather than widening the helper to the Protocol.
        assert isinstance(session, Session)
        report = prune_stale_invites(session=session, now=now, clock=resolved_clock)
        # ``prune_stale_invites`` mutates rows + publishes events but
        # does not commit; the UoW's ``__exit__`` does so on a clean
        # return. An empty sweep is the no-op path — no rows touched,
        # no events fired, the commit is still cheap (pure connection
        # release).

    _log.info(
        "invite TTL sweep completed",
        extra={
            "event": _LOG_EVENT,
            "expired_count": report.expired_count,
            # ``expired_ids`` is tuple-of-str at the dataclass layer;
            # the structured-log handler serialises tuples as JSON
            # arrays, so the operator dashboard sees the ids verbatim
            # without a per-id log line.
            "expired_ids": list(report.expired_ids),
        },
    )

    return report
