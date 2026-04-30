"""``sweep_undispatched_messages`` — chat-gateway dispatch safety net (cd-0gaa).

The webhook ingest path commits a ``chat_message`` row in
``chat_gateway`` channels and synchronously publishes
``chat.message.received`` so the in-process dispatcher can hand the
row off to the agent runtime. A worker restart between webhook commit
and dispatch (or a crash inside the dispatcher's background task)
leaves the row stamped with ``dispatched_to_agent_at = NULL``. Without
a periodic safety net those rows would never reach the agent runtime.

This sweep — registered as the 30 s ``agent_dispatch_sweep`` APScheduler
job per §23 "Routing > Inbound" and §16 "Worker process" — walks every
``chat_message`` row whose channel kind is ``chat_gateway``,
``dispatched_to_agent_at`` is ``NULL``, and ``created_at`` is older
than 30 s. For each row it re-publishes ``chat.message.received`` so
the in-process dispatcher catches up. The dispatcher's existing CAS on
``dispatched_to_agent_at`` keeps re-publication idempotent: a row the
original handler eventually claimed first is skipped on the sweep's
re-publish path.

Cross-tenant by design — chat-gateway messages carry their own
``workspace_id`` and the dispatcher already operates under
``tenant_agnostic`` while loading the row. The sweep mirrors the
sibling :mod:`app.worker.tasks.webhook_dispatch` and
:mod:`app.worker.tasks.approval_ttl` patterns.

Per-row failures are logged + audited under
``chat_gateway.sweep.requeue.failed`` and the sweep continues on to
the next row. Bounded at :data:`SWEEP_BATCH_SIZE` rows per tick so a
backlog cannot pin one tick on the DB.

See ``docs/specs/23-chat-gateway.md`` §"Routing > Inbound" and
``docs/specs/16-deployment-operations.md`` §"Worker process".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.messaging.models import ChatChannel, ChatMessage
from app.adapters.db.session import make_uow
from app.audit import write_audit
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import ChatMessageReceived
from app.observability.metrics import WORKER_SWEEP_REQUEUED_TOTAL
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "SWEEP_BATCH_SIZE",
    "SWEEP_GRACE_SECONDS",
    "SweepResult",
    "sweep_undispatched_messages",
]


_log = logging.getLogger(__name__)

_LOG_EVENT: Final[str] = "chat_gateway.sweep.tick"

# Pinned ``job`` label value for the
# :data:`WORKER_SWEEP_REQUEUED_TOTAL` counter. Mirrors
# :data:`app.worker.scheduler.CHAT_GATEWAY_SWEEP_JOB_ID` so dashboards
# can join the metric to the scheduler heartbeat without a separate
# alias. Duplicated as a literal (rather than imported) to keep the
# task module free of an import on the scheduler module — that import
# would create a cycle (the scheduler already imports the
# ``_make_chat_gateway_sweep_body`` factory from the maintenance
# module, which then defers to this task module).
_METRIC_JOB_LABEL: Final[str] = "chat_gateway.agent_dispatch_sweep"

# Maximum number of stragglers reprocessed per sweep tick. The
# §23/§16 cadence is 30 s; a single tick that touches many more rows
# than this would dominate the worker's budget on a fleet returning
# from a long pause. The next tick picks up the leftover rows.
SWEEP_BATCH_SIZE: Final[int] = 200

# Age threshold for a row to be considered "stuck". Anything younger
# is still inside the grace window the in-process dispatcher's
# background task is allowed to take. §23 "Routing > Inbound" pins
# 60 s for the ``pending`` grace; we mirror the §16 sweep cadence
# (30 s) so a row left orphaned by a webhook-process crash gets
# picked up on the next tick after the sweep cadence elapses, and
# the spec's "60 s grace" surfaces as roughly two ticks of latency
# between commit and re-fire — comfortably inside the §23 budget
# while keeping the cadence stable across deployments.
SWEEP_GRACE_SECONDS: Final[int] = 30


@dataclass(frozen=True, slots=True)
class SweepResult:
    """Summary of one :func:`sweep_undispatched_messages` call.

    ``requeued_count`` counts rows successfully re-published on the
    bus; ``failed_count`` counts rows whose re-publish raised. The
    union is the bounded batch the tick processed.
    ``processed_ids`` is the full set of message ids touched —
    useful for tests asserting bounded-batch and order semantics.
    """

    requeued_count: int
    failed_count: int
    processed_ids: tuple[str, ...]


def sweep_undispatched_messages(
    *,
    event_bus: EventBus | None = None,
    clock: Clock | None = None,
) -> SweepResult:
    """Run one chat-gateway dispatch safety-net sweep.

    Opens a fresh UoW (the worker has no ambient session), enumerates
    stragglers (older than :data:`SWEEP_GRACE_SECONDS`, not yet
    dispatched, gateway-inbound) bounded at :data:`SWEEP_BATCH_SIZE`,
    and re-publishes ``chat.message.received`` for each. Per-row
    failures are caught + audited so one bad row cannot kill the
    sweep.

    The bus + clock are injectable for tests; production passes
    ``None`` and falls back to the module-level singleton bus and
    :class:`SystemClock`.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    now = resolved_clock.now()
    cutoff = now - timedelta(seconds=SWEEP_GRACE_SECONDS)

    requeued = 0
    failed = 0
    processed: list[str] = []

    with make_uow() as session:
        assert isinstance(session, Session)
        stragglers = _select_stragglers(session, cutoff=cutoff)
        for row in stragglers:
            processed.append(row.id)
            try:
                _requeue_one(
                    session,
                    message=row,
                    event_bus=resolved_bus,
                    now=now,
                    clock=resolved_clock,
                )
            except Exception:
                # Per-row failure: log + audit + count, then keep
                # going. A swallowed BaseException would mask a
                # ``KeyboardInterrupt`` / ``SystemExit`` so the
                # narrow ``Exception`` boundary is deliberate.
                _log.exception(
                    "chat gateway sweep requeue failed",
                    extra={
                        "event": "chat_gateway.sweep.requeue.failed",
                        "message_id": row.id,
                        "workspace_id": row.workspace_id,
                    },
                )
                # Defensive — a per-row audit-write failure must not
                # rollback the whole tick (the UoW commits on a
                # clean ``__exit__``; an escaping exception here
                # would lose every prior successful re-publish's
                # audit row). The audit write is best-effort; the
                # log line above is the durable signal.
                try:
                    _audit_failure(
                        session,
                        message=row,
                        reason="event_bus_publish_failed",
                        clock=resolved_clock,
                    )
                except Exception:
                    _log.exception(
                        "chat gateway sweep failure-audit write failed",
                        extra={
                            "event": "chat_gateway.sweep.audit.failed",
                            "message_id": row.id,
                            "workspace_id": row.workspace_id,
                        },
                    )
                failed += 1
                _bump_counter("failed")
                continue

            requeued += 1
            _bump_counter("requeued")

    _log.info(
        "chat gateway sweep tick completed",
        extra={
            "event": _LOG_EVENT,
            "processed_count": len(processed),
            "requeued": requeued,
            "failed": failed,
        },
    )
    return SweepResult(
        requeued_count=requeued,
        failed_count=failed,
        processed_ids=tuple(processed),
    )


@dataclass(frozen=True, slots=True)
class _StragglerRow:
    """Minimal projection of a stuck ``chat_message`` row.

    Carrying the small tuple of fields the sweep needs (rather than
    the full ORM row) keeps the per-row work simple and the
    re-publish payload trivially testable.

    ``gateway_binding_id`` is non-optional because the SELECT
    predicate filters on ``gateway_binding_id IS NOT NULL`` — a
    legitimate gateway-inbound row always carries the binding.
    """

    id: str
    workspace_id: str
    channel_id: str
    gateway_binding_id: str
    source: str
    created_at: datetime


def _select_stragglers(
    session: Session, *, cutoff: datetime
) -> tuple[_StragglerRow, ...]:
    """Return undispatched chat-gateway *inbound* rows older than ``cutoff``.

    Cross-tenant read — the sweep is deployment-scope and each row
    carries its own ``workspace_id``. Ordered by ``created_at``
    ascending so a backlog clears in commit order.

    The predicate intentionally narrows past ``ChatChannel.kind ==
    'chat_gateway'``: an outbound agent reply on a chat-gateway
    channel is also inserted with ``dispatched_to_agent_at = NULL``
    (see ``app.domain.agent.runtime._write_chat_reply`` and
    ``app.api.v1.agent``), and a sweep that re-published those rows
    would loop the agent on its own replies. The two extra filters
    pin the row to the gateway-inbound shape:

    * ``gateway_binding_id IS NOT NULL`` — only inbound rows ride a
      binding (``insert_inbound_message`` in
      ``app.adapters.db.messaging.repositories``); the agent's
      outbound writes leave it ``NULL``.
    * ``author_user_id IS NULL`` — gateway-inbound rows have no
      :class:`User` row for the external sender; in-app authored
      rows and the agent's own replies always carry a ``User`` id.

    Either predicate alone is enough today, but we ride both so a
    future code path that flips one invariant (e.g. an outbound row
    that copies the inbound binding) doesn't quietly fall back into
    the sweep's claws.
    """
    with tenant_agnostic():
        stmt = (
            select(
                ChatMessage.id,
                ChatMessage.workspace_id,
                ChatMessage.channel_id,
                ChatMessage.gateway_binding_id,
                ChatMessage.source,
                ChatMessage.created_at,
            )
            .join(ChatChannel, ChatChannel.id == ChatMessage.channel_id)
            .where(ChatChannel.kind == "chat_gateway")
            .where(ChatMessage.dispatched_to_agent_at.is_(None))
            .where(ChatMessage.gateway_binding_id.is_not(None))
            .where(ChatMessage.author_user_id.is_(None))
            .where(ChatMessage.created_at < cutoff)
            .order_by(ChatMessage.created_at.asc())
            .limit(SWEEP_BATCH_SIZE)
        )
        rows = session.execute(stmt).all()
    return tuple(
        _StragglerRow(
            id=row[0],
            workspace_id=row[1],
            channel_id=row[2],
            # mypy: the SELECT predicate above narrows the column to
            # NOT NULL, but the ORM column is still typed as
            # ``str | None`` — the assertion documents the invariant
            # without reaching for ``cast()``.
            gateway_binding_id=_require_binding_id(row[3]),
            source=row[4],
            created_at=row[5],
        )
        for row in rows
    )


def _require_binding_id(value: str | None) -> str:
    """Narrow the optional binding id to ``str``.

    Encodes the SELECT-side ``gateway_binding_id IS NOT NULL`` filter
    as a runtime invariant so the ``_StragglerRow`` field stays
    non-optional and downstream code (the ``ChatMessageReceived``
    payload, which requires ``binding_id: str``) doesn't need a
    second branch. A ``None`` here means the filter is broken — fail
    loud, not silently with an empty-string binding id.
    """
    if value is None:  # pragma: no cover - defensive
        raise RuntimeError(
            "chat gateway sweep: gateway_binding_id IS NOT NULL filter broken"
        )
    return value


def _requeue_one(
    session: Session,
    *,
    message: _StragglerRow,
    event_bus: EventBus,
    now: datetime,
    clock: Clock,
) -> None:
    """Publish ``chat.message.received`` for one straggler + audit success.

    The dispatcher's CAS on ``dispatched_to_agent_at`` keeps the
    re-publish idempotent against a primary handler that eventually
    catches up first.
    """
    ctx = _system_ctx(message.workspace_id)
    event_bus.publish(
        ChatMessageReceived(
            workspace_id=message.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=now,
            channel_id=message.channel_id,
            message_id=message.id,
            author_user_id=None,
            channel_kind="chat_gateway",
            binding_id=message.gateway_binding_id,
            source=message.source,
        )
    )
    write_audit(
        session,
        ctx,
        entity_kind="chat_message",
        entity_id=message.id,
        action="chat_gateway.sweep.requeued",
        diff={
            "binding_id": message.gateway_binding_id,
            "channel_id": message.channel_id,
            "source": message.source,
        },
        via="worker",
        clock=clock,
    )


def _audit_failure(
    session: Session,
    *,
    message: _StragglerRow,
    reason: str,
    clock: Clock,
) -> None:
    """Append the per-row failure audit row inside the sweep's UoW."""
    ctx = _system_ctx(message.workspace_id)
    write_audit(
        session,
        ctx,
        entity_kind="chat_message",
        entity_id=message.id,
        action="chat_gateway.sweep.requeue.failed",
        diff={
            "binding_id": message.gateway_binding_id,
            "channel_id": message.channel_id,
            "source": message.source,
            "reason": reason,
        },
        via="worker",
        clock=clock,
    )


def _system_ctx(workspace_id: str) -> WorkspaceContext:
    """Build the system actor context the sweep audits under.

    Mirrors :func:`app.domain.chat_gateway.dispatcher._system_ctx`
    so the audit row carries the same actor identity as the rest of
    the gateway path. Matches the §15 "system" actor convention.
    """
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="chat-gateway",
        actor_id="system:chat_gateway",
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
        principal_kind="system",
    )


def _bump_counter(outcome: str) -> None:
    """Increment :data:`WORKER_SWEEP_REQUEUED_TOTAL` for ``outcome``.

    Pulled out as a helper so the metric label set is the single
    string seam the rest of the file depends on; a future rename
    touches one place.
    """
    WORKER_SWEEP_REQUEUED_TOTAL.labels(
        job=_METRIC_JOB_LABEL,
        outcome=outcome,
    ).inc()
