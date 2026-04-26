"""``dispatch_due_webhooks`` — outbound delivery worker tick (cd-q885).

Walks every ``webhook_delivery`` row whose ``status='pending'`` and
``next_attempt_at <= now``, opens a fresh UoW per row, and fires one
HTTP POST attempt through :func:`app.domain.integrations.webhooks.deliver`.

Cross-tenant by design — like the approval-TTL sweep, the dispatcher
is deployment-scope (NOT per-workspace):

1. Every ``webhook_delivery`` row carries its own ``workspace_id``;
   the dispatcher reads under :func:`tenant_agnostic` and the
   per-row deliver call writes its audit (on dead-letter) keyed off
   that ``workspace_id``.
2. The retry schedule is a global policy. Per-workspace fan-out
   would scale linearly with workspace count and dominate a fleet's
   worker budget; one global sweep is O(n) in the count of due rows.

Per-row UoW so a single misbehaving subscription cannot roll back
the entire sweep: every attempt commits independently, and the audit
row on dead-letter rides the same UoW as the row's status flip.

A failed dispatch raises out of the wrapper; the scheduler's
:func:`~app.worker.scheduler.wrap_job` catches + logs the
exception, the heartbeat row stops advancing, and ``/readyz`` goes
red via the staleness window. The next tick (:data:`WEBHOOK_DISPATCH_INTERVAL_SECONDS`
later) retries.

See ``docs/specs/10-messaging-notifications.md`` §"Webhooks
(outbound)" and ``docs/specs/16-deployment-operations.md`` §"Worker
process".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.integrations.models import WebhookDelivery
from app.adapters.db.integrations.repositories import (
    SqlAlchemyWebhookRepository,
)
from app.adapters.db.secrets.repositories import (
    SqlAlchemySecretEnvelopeRepository,
)
from app.adapters.db.session import make_uow
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.config import get_settings
from app.domain.integrations.webhooks import (
    DELIVERY_PENDING,
    DeliveryReport,
    deliver,
)
from app.tenancy import tenant_agnostic
from app.util.clock import Clock, SystemClock

__all__ = [
    "DispatchReport",
    "dispatch_due_webhooks",
]


_log = logging.getLogger(__name__)

_LOG_EVENT: Final[str] = "webhook.dispatch.tick"


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """Summary of one :func:`dispatch_due_webhooks` call.

    ``successes`` / ``retries`` / ``dead_lettered`` are counts of
    per-row outcomes the tick produced. ``processed_ids`` is the
    full set of delivery ids touched — useful for tests pinning
    determinism on a fixture set.
    """

    processed_count: int
    successes: int
    retries: int
    dead_lettered: int
    processed_ids: tuple[str, ...]


def _select_due(session: Session, *, now: datetime) -> tuple[str, ...]:
    """Return delivery ids whose retry window has opened.

    Filters for ``status='pending'`` and ``next_attempt_at <= now``.
    Ordered by ``next_attempt_at`` ascending so older overdue rows
    fire first; a fleet returning from a long pause clears the
    backlog in time order.
    """
    with tenant_agnostic():
        stmt = (
            select(WebhookDelivery.id)
            .where(WebhookDelivery.status == DELIVERY_PENDING)
            .where(WebhookDelivery.next_attempt_at <= now)
            .order_by(WebhookDelivery.next_attempt_at.asc())
        )
        return tuple(session.scalars(stmt))


def dispatch_due_webhooks(*, clock: Clock | None = None) -> DispatchReport:
    """Run one dispatch sweep across the deployment.

    For every due delivery row, opens a fresh UoW (the worker has no
    ambient session) and calls :func:`deliver`. Each attempt commits
    independently — a misbehaving subscription cannot roll back the
    sweep's progress on its peers.

    The clock is injectable for tests; production passes ``None``
    and falls back to :class:`SystemClock`.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    settings = get_settings()
    if settings.root_key is None:
        # No root key → cannot decrypt subscription secrets. Bail
        # noisily so /readyz catches the misconfig; the heartbeat
        # stops advancing and the operator dashboard surfaces it.
        _log.warning(
            "webhook dispatcher: root key is unset; skipping tick",
            extra={"event": "webhook.dispatch.skipped_no_root_key"},
        )
        return DispatchReport(
            processed_count=0,
            successes=0,
            retries=0,
            dead_lettered=0,
            processed_ids=(),
        )

    # First read — gather the due ids in one short UoW; we then
    # process each in its own UoW so a long sweep doesn't pin a
    # transaction across every dispatch.
    with make_uow() as session:
        assert isinstance(session, Session)
        due_ids = _select_due(session, now=now)

    successes = 0
    retries = 0
    dead_lettered = 0
    processed: list[str] = []
    for delivery_id in due_ids:
        try:
            report = _dispatch_one(delivery_id, clock=resolved_clock)
        except Exception:
            # Per-row failure is logged + counted as a retry-side
            # event. We swallow here so the rest of the sweep proceeds.
            _log.exception(
                "webhook dispatch row failed",
                extra={
                    "event": "webhook.dispatch.row_error",
                    "delivery_id": delivery_id,
                },
            )
            retries += 1
            processed.append(delivery_id)
            continue

        processed.append(delivery_id)
        if report.dead_lettered:
            dead_lettered += 1
        elif report.status == "succeeded":
            successes += 1
        else:
            retries += 1

    _log.info(
        "webhook dispatch tick completed",
        extra={
            "event": _LOG_EVENT,
            "processed_count": len(processed),
            "successes": successes,
            "retries": retries,
            "dead_lettered": dead_lettered,
        },
    )
    return DispatchReport(
        processed_count=len(processed),
        successes=successes,
        retries=retries,
        dead_lettered=dead_lettered,
        processed_ids=tuple(processed),
    )


def _dispatch_one(delivery_id: str, *, clock: Clock) -> DeliveryReport:
    """Open a fresh UoW and dispatch one delivery.

    The cipher + repository are wired here so each row's commit is
    independent. Settings are resolved per call; the lookup is
    cached by :func:`get_settings`.
    """
    settings = get_settings()
    if settings.root_key is None:
        # The outer sweep already gates on root_key; defensive
        # re-check here would be redundant. Raise so a programming
        # bug (someone hot-reloaded settings between the gate and
        # this call) surfaces noisily.
        raise RuntimeError("root_key is unset; cannot dispatch")

    with make_uow() as session:
        assert isinstance(session, Session)
        secret_repo = SqlAlchemySecretEnvelopeRepository(session)
        envelope = Aes256GcmEnvelope(
            settings.root_key, repository=secret_repo, clock=clock
        )
        webhook_repo = SqlAlchemyWebhookRepository(session)
        return deliver(
            session,
            delivery_id=delivery_id,
            repo=webhook_repo,
            envelope=envelope,
            clock=clock,
        )
