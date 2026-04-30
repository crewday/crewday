"""Web-push delivery worker tick (cd-y60x).

Consumes the ``notification_push_queue`` staging table populated by
:class:`~app.domain.messaging.notifications.NotificationService` at
notify time. One row per ``(notification, push_token)`` pair; the
worker walks rows whose ``status='pending'`` and ``next_attempt_at <=
now``, atomically claims each one (CAS update keyed on the prior
status + ``attempt`` counter), and fires a :func:`pywebpush.webpush`
send signed with the workspace's VAPID private key from
``workspace.settings_json``.

Spec: ``docs/specs/10-messaging-notifications.md`` §"Channels"
(web push), §"Delivery tracking", and §"Agent-message delivery".

Behaviour matrix:

* **2xx response** — flip to ``status='sent'`` and bump
  ``push_token.last_used_at`` so the freshness sweep keeps the row
  alive past the §10 60-day window.
* **404 / 410 Gone** — the provider has cancelled the subscription;
  hard-delete the matching :class:`PushToken` row, audit
  ``messaging.push.token_purged``, and flip the queue row to
  ``sent`` (terminal-clean — there's nothing left to retry against
  this token).
* **Other 4xx** — log + dead-letter; the receiver said "no", not
  "try again later". A 401 / 403 on a misconfigured VAPID keypair is
  the operator's problem and does not gain anything from a retry.
* **5xx / network error / timeout** — bump ``attempt`` and reschedule
  per the backoff table; once ``attempt >= 5`` the row dead-letters.

Backoff schedule per the spec brief: ``[30s, 2m, 10m, 1h]``. The
first send fires immediately when the row enqueues at notify time;
attempts 2..5 ride the schedule above. ``attempt=0`` rows execute
on attempt 1, then ``attempt=1`` waits 30 s, ``attempt=2`` waits
2 min, ``attempt=3`` 10 min, ``attempt=4`` 1 h. The fifth attempt
exhausts the budget — a sixth would fall off the table.

**Concurrency.** The tick fans out per workspace (up to 10 concurrent
HTTP calls per workspace per tick) so a single tenant with thousands
of devices does not block deliveries for every other tenant. Across
the deployment, the semaphore caps the total in-flight HTTP calls
so a worker process does not open hundreds of sockets at once. The
fan-out uses :func:`asyncio.to_thread` because :mod:`pywebpush` is
synchronous (it wraps :mod:`requests`); the semaphore lives in the
asyncio loop.

**Restart safety.** The claim CAS update sets the row's
``next_attempt_at`` forward by the in-flight visibility window
(default 10 min). A worker that crashes mid-send does not leave the
row stuck — the next tick after the visibility window expires
re-selects the row and another worker picks it up. A peer worker
that runs in the *same* tick window already lost the CAS via the
``status='pending'`` predicate, so two workers never double-send.

**Cross-tenant.** Like the webhook dispatcher and approval-TTL
sweep, this tick is deployment-scope. Each row carries its own
``workspace_id``; the per-row audit (token purge) keys off that
field through a system-actor :class:`~app.tenancy.WorkspaceContext`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.orm import Session

from app.adapters.db.messaging.repositories import (
    SqlAlchemyPushDeliveryRepository,
)
from app.adapters.db.session import make_uow
from app.audit import write_audit
from app.domain.messaging.ports import PushDeliveryRepository, PushDeliveryRow
from app.domain.messaging.push_tokens import (
    SETTINGS_KEY_VAPID_PRIVATE,
    SETTINGS_KEY_VAPID_SUBJECT,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.worker.jobs.common import _system_actor_context

__all__ = [
    "BACKOFF_SCHEDULE_SECONDS",
    "DEFAULT_VAPID_SUBJECT_FALLBACK",
    "IN_FLIGHT_VISIBILITY_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_BATCH_SIZE",
    "MAX_CONCURRENT_PER_WORKSPACE",
    "PushDeliverFn",
    "PushSendOutcome",
    "WebPushReport",
    "_make_web_push_dispatch_body",
    "deliver_push",
    "dispatch_due_pushes",
]

_log = logging.getLogger(__name__)


# Backoff schedule between attempts. ``attempt=N`` (after the N-th
# failed try) waits ``BACKOFF_SCHEDULE_SECONDS[N - 1]`` before the
# next retry. The first send fires immediately on enqueue; the
# schedule covers attempts 2..5.
BACKOFF_SCHEDULE_SECONDS: Final[tuple[int, ...]] = (
    30,  # after attempt 1 → wait 30 s before attempt 2
    120,  # after attempt 2 → wait 2 min before attempt 3
    600,  # after attempt 3 → wait 10 min before attempt 4
    3600,  # after attempt 4 → wait 1 h before attempt 5
)

# Hard cap on attempts — past this the row dead-letters.
MAX_ATTEMPTS: Final[int] = 5

# How many rows the tick claims at most per invocation. Keeps a
# single tick bounded so a fleet returning from a long pause does
# not stall the heartbeat. The dispatcher tick interval (60 s by
# default — see :data:`app.worker.scheduler.WEB_PUSH_DISPATCH_\
# INTERVAL_SECONDS`) means up to ``MAX_BATCH_SIZE`` rows clear per
# minute per worker; a backlog drains across multiple ticks.
MAX_BATCH_SIZE: Final[int] = 200

# Per-workspace bound on concurrent in-flight HTTP calls. Spec brief:
# "max 10 concurrent HTTP calls per worker tick" to avoid thundering
# herd. Each workspace has its own semaphore so one tenant with a
# huge fan-out does not starve another's deliveries.
MAX_CONCURRENT_PER_WORKSPACE: Final[int] = 10

# Visibility window: how long the claim CAS pushes the row's
# ``next_attempt_at`` forward. A worker that crashes mid-send leaves
# the row in ``status='in_flight'`` but with a future ``next_attempt
# _at``; the next tick's ``select_due`` query treats the row as
# "claimed but not done" and the per-tick selection won't pick it up
# until the visibility window expires. Keep it generous enough that a
# slow upstream provider (FCM has been seen at ~30 s p99) does not
# trip a peer worker into a double-send, but tight enough that a
# crashed worker's row recovers within an operator-meaningful window.
IN_FLIGHT_VISIBILITY_SECONDS: Final[int] = 600

# Fallback subject claim for the VAPID JWT. Some providers (notably
# FCM) reject pushes whose JWT lacks a ``sub`` claim. The operator
# can override per-workspace via :data:`SETTINGS_KEY_VAPID_SUBJECT`;
# otherwise the worker uses this RFC 8292 §"Application Server
# Subject Information" placeholder. The placeholder URL points at
# the project — a real deployment should set the per-workspace
# subject to a contact ``mailto:`` address.
DEFAULT_VAPID_SUBJECT_FALLBACK: Final[str] = "mailto:no-reply@crew.day"


@dataclass(frozen=True, slots=True)
class PushSendOutcome:
    """Result of one ``pywebpush`` call.

    ``status_code`` is the provider's HTTP response code, or ``None``
    when the send raised before reaching a response (timeout, DNS
    failure). ``error`` is a short label suitable for stamping on
    the queue row's ``last_error`` column.

    Outcome classification:

    * ``status_code in (200, 201, 202, 204)`` → success.
    * ``status_code in (404, 410)`` → token gone; purge.
    * ``status_code`` is a 4xx other than 404 / 410 → terminal failure.
    * Otherwise (5xx / ``None``) → transient.
    """

    status_code: int | None
    error: str | None


# Pluggable seam — tests inject a fake to avoid hitting the real
# provider. Production wires :func:`deliver_push` (below).
PushDeliverFn = Callable[
    [PushDeliveryRow, "_TokenContext", "_VapidConfig"],
    PushSendOutcome,
]


@dataclass(frozen=True, slots=True)
class WebPushReport:
    """Summary of one :func:`dispatch_due_pushes` invocation."""

    processed_count: int
    successes: int
    retries: int
    dead_lettered: int
    tokens_purged: int


@dataclass(frozen=True, slots=True)
class _TokenContext:
    """Subset of :class:`PushTokenRow` the sender + audit need."""

    token_id: str
    user_id: str
    endpoint: str
    p256dh: str
    auth: str


@dataclass(frozen=True, slots=True)
class _VapidConfig:
    """VAPID material loaded from the workspace settings."""

    private_key: str
    subject: str


# ---------------------------------------------------------------------------
# Production sender (uses pywebpush)
# ---------------------------------------------------------------------------


def deliver_push(
    delivery: PushDeliveryRow,
    token: _TokenContext,
    vapid: _VapidConfig,
) -> PushSendOutcome:
    """Fire one ``pywebpush.webpush`` call and classify the outcome.

    Synchronous — the worker tick wraps this in
    :func:`asyncio.to_thread` so the event loop stays responsive
    while the call walks the wire. Local imports keep
    :mod:`pywebpush` (and its ``requests`` transitive) off the
    import path of unit tests that never reach this seam.
    """
    from pywebpush import WebPushException, webpush

    subscription_info = {
        "endpoint": token.endpoint,
        "keys": {"p256dh": token.p256dh, "auth": token.auth},
    }
    try:
        response = webpush(
            subscription_info=subscription_info,
            data=delivery.body,
            vapid_private_key=vapid.private_key,
            vapid_claims={"sub": vapid.subject},
            ttl=60,
        )
    except WebPushException as exc:
        # ``WebPushException`` carries the underlying ``requests``
        # response when the provider returned a non-2xx. A network /
        # TLS error has ``response is None``.
        provider_response = getattr(exc, "response", None)
        if provider_response is not None:
            code = getattr(provider_response, "status_code", None)
            if isinstance(code, int):
                return PushSendOutcome(status_code=code, error=f"http_{code}")
        return PushSendOutcome(status_code=None, error=f"webpush:{type(exc).__name__}")
    except (TimeoutError, ConnectionError) as exc:
        return PushSendOutcome(status_code=None, error=f"network:{type(exc).__name__}")

    # ``webpush`` returns the underlying ``requests.Response`` on a
    # success path; older versions returned the response unconditionally
    # and the str path is hit only when ``curl=True`` (we never set
    # that flag). Fall through defensively if a future release widens
    # the union further.
    status_code = getattr(response, "status_code", None)
    if not isinstance(status_code, int):
        return PushSendOutcome(status_code=None, error="webpush:unknown_response")
    return PushSendOutcome(status_code=status_code, error=None)


# ---------------------------------------------------------------------------
# Dispatcher entrypoint
# ---------------------------------------------------------------------------


def dispatch_due_pushes(
    *,
    clock: Clock | None = None,
    sender: PushDeliverFn | None = None,
) -> WebPushReport:
    """Run one web-push dispatch sweep across the deployment.

    Loads the due rows in one short UoW, then processes them
    concurrently — bounded by the per-workspace semaphore — through
    fresh per-row UoWs so a single misbehaving send cannot roll back
    the rest of the sweep.

    The ``sender`` is injectable for tests; production passes
    ``None`` and the body falls back to :func:`deliver_push`. The
    test suite wires a fake that honours the same protocol but
    answers from a fixture rather than hitting the wire.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_sender: PushDeliverFn = sender if sender is not None else deliver_push
    now = resolved_clock.now()

    with make_uow() as session:
        assert isinstance(session, Session)
        repo = SqlAlchemyPushDeliveryRepository(session)
        due = list(repo.select_due(now=now, limit=MAX_BATCH_SIZE))

    if not due:
        _log.info(
            "web push dispatch tick — no due rows",
            extra={
                "event": "messaging.push.tick.empty",
                "now": now.isoformat(),
            },
        )
        return WebPushReport(
            processed_count=0,
            successes=0,
            retries=0,
            dead_lettered=0,
            tokens_purged=0,
        )

    # Bucket by workspace so the per-workspace semaphore is cheap to
    # build and the per-tick fan-out keeps tenant fairness.
    by_workspace: dict[str, list[PushDeliveryRow]] = defaultdict(list)
    for row in due:
        by_workspace[row.workspace_id].append(row)

    report = asyncio.run(
        _run_dispatch(
            by_workspace=by_workspace,
            clock=resolved_clock,
            sender=resolved_sender,
            now=now,
        )
    )
    _log.info(
        "web push dispatch tick completed",
        extra={
            "event": "messaging.push.tick.summary",
            "processed_count": report.processed_count,
            "successes": report.successes,
            "retries": report.retries,
            "dead_lettered": report.dead_lettered,
            "tokens_purged": report.tokens_purged,
        },
    )
    return report


async def _run_dispatch(
    *,
    by_workspace: dict[str, list[PushDeliveryRow]],
    clock: Clock,
    sender: PushDeliverFn,
    now: datetime,
) -> WebPushReport:
    """Per-workspace fan-out under bounded concurrency."""
    counters = _Counters()
    workspace_tasks: list[Awaitable[None]] = []
    for workspace_id, rows in by_workspace.items():
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_PER_WORKSPACE)
        workspace_tasks.append(
            _process_workspace(
                workspace_id=workspace_id,
                rows=rows,
                semaphore=semaphore,
                clock=clock,
                sender=sender,
                now=now,
                counters=counters,
            )
        )
    await asyncio.gather(*workspace_tasks)
    return WebPushReport(
        processed_count=counters.processed,
        successes=counters.successes,
        retries=counters.retries,
        dead_lettered=counters.dead_lettered,
        tokens_purged=counters.tokens_purged,
    )


@dataclass(slots=True)
class _Counters:
    """Mutable counters threaded through the per-row dispatch path."""

    processed: int = 0
    successes: int = 0
    retries: int = 0
    dead_lettered: int = 0
    tokens_purged: int = 0


async def _process_workspace(
    *,
    workspace_id: str,
    rows: list[PushDeliveryRow],
    semaphore: asyncio.Semaphore,
    clock: Clock,
    sender: PushDeliverFn,
    now: datetime,
    counters: _Counters,
) -> None:
    async def _bounded_one(row: PushDeliveryRow) -> None:
        async with semaphore:
            await asyncio.to_thread(
                _dispatch_one,
                row,
                clock,
                sender,
                now,
                counters,
            )

    await asyncio.gather(*[_bounded_one(row) for row in rows])


def _dispatch_one(
    row: PushDeliveryRow,
    clock: Clock,
    sender: PushDeliverFn,
    now: datetime,
    counters: _Counters,
) -> None:
    """Open a fresh UoW and process one queue row.

    Each row's UoW commits independently — a misbehaving send
    cannot roll back the sweep's progress on its peers.
    """
    counters.processed += 1
    try:
        with make_uow() as session:
            assert isinstance(session, Session)
            repo = SqlAlchemyPushDeliveryRepository(session)
            outcome = _process_row(
                row=row,
                repo=repo,
                clock=clock,
                sender=sender,
                now=now,
            )
            if outcome == "sent":
                counters.successes += 1
            elif outcome == "retry":
                counters.retries += 1
            elif outcome == "dead_lettered":
                counters.dead_lettered += 1
            elif outcome == "token_purged":
                counters.successes += 1
                counters.tokens_purged += 1
            # ``no_op`` (already terminal) does not bump any counter.
    except Exception:
        # The row's UoW rolled back. Log + count as a retry so the
        # operator dashboard surfaces the recurring error; the next
        # tick after the visibility window expires picks the row up
        # again because the claim flipped ``next_attempt_at`` forward.
        counters.retries += 1
        _log.exception(
            "web push dispatch row failed",
            extra={
                "event": "messaging.push.row_error",
                "delivery_id": row.id,
                "workspace_id": row.workspace_id,
            },
        )


def _process_row(
    *,
    row: PushDeliveryRow,
    repo: PushDeliveryRepository,
    clock: Clock,
    sender: PushDeliverFn,
    now: datetime,
) -> str:
    """Drive one row through claim → send → stamp.

    Returns one of ``"sent"`` / ``"retry"`` / ``"dead_lettered"`` /
    ``"token_purged"`` / ``"no_op"`` so the caller can update its
    counters without re-reading the row.
    """
    # Re-load through the worker's own UoW — the snapshot from
    # ``select_due`` is from a different session.
    fresh = repo.get(delivery_id=row.id)
    if fresh is None:
        # Row was deleted (CASCADE from a workspace / notification
        # purge between select and process). Nothing to do.
        return "no_op"
    if fresh.status != "pending":
        # Lost a race to a peer worker, or already terminal. Skip.
        return "no_op"

    in_flight_until = now + timedelta(seconds=IN_FLIGHT_VISIBILITY_SECONDS)
    claimed = repo.claim(
        delivery_id=fresh.id,
        expected_attempt=fresh.attempt,
        now=now,
        in_flight_until=in_flight_until,
    )
    if not claimed:
        # Peer worker beat us to the CAS — drop quietly.
        return "no_op"

    attempt_number = fresh.attempt + 1
    token = repo.get_token(push_token_id=fresh.push_token_id)
    if token is None:
        # Token deleted between enqueue and send. Nothing to push;
        # mark sent (terminal-clean) so the row drops off the queue.
        repo.mark_sent(
            delivery_id=fresh.id,
            attempt=attempt_number,
            now=now,
            last_status_code=None,
        )
        return "sent"

    vapid = _load_vapid_config(repo, workspace_id=fresh.workspace_id)
    if vapid is None:
        # Operator hasn't provisioned a private key for this workspace.
        # Bump attempts so a misconfigured workspace eventually drops
        # off the queue rather than spinning forever; surface via
        # ``last_error`` for the operator dashboard.
        return _stamp_failure(
            repo=repo,
            row=fresh,
            attempt=attempt_number,
            now=now,
            outcome=PushSendOutcome(status_code=None, error="vapid_missing"),
            terminal=True,
            clock=clock,
        )

    token_ctx = _TokenContext(
        token_id=token.id,
        user_id=token.user_id,
        endpoint=token.endpoint,
        p256dh=token.p256dh,
        auth=token.auth,
    )
    outcome = sender(fresh, token_ctx, vapid)

    code = outcome.status_code
    if code is not None and 200 <= code < 300:
        repo.mark_sent(
            delivery_id=fresh.id,
            attempt=attempt_number,
            now=now,
            last_status_code=code,
        )
        repo.touch_token_last_used(push_token_id=token.id, now=now)
        return "sent"

    if code in (404, 410):
        _purge_token_and_mark_sent(
            repo=repo,
            row=fresh,
            token_user_id=token.user_id,
            token_id=token.id,
            attempt=attempt_number,
            now=now,
            last_status_code=code,
            clock=clock,
        )
        return "token_purged"

    # Other 4xx → terminal failure.
    if code is not None and 400 <= code < 500:
        return _stamp_failure(
            repo=repo,
            row=fresh,
            attempt=attempt_number,
            now=now,
            outcome=outcome,
            terminal=True,
            clock=clock,
        )

    # 5xx / network error / timeout / unknown → transient.
    return _stamp_failure(
        repo=repo,
        row=fresh,
        attempt=attempt_number,
        now=now,
        outcome=outcome,
        terminal=False,
        clock=clock,
    )


def _stamp_failure(
    *,
    repo: PushDeliveryRepository,
    row: PushDeliveryRow,
    attempt: int,
    now: datetime,
    outcome: PushSendOutcome,
    terminal: bool,
    clock: Clock,
) -> str:
    """Stamp a transient retry or a dead-letter; return outcome label."""
    error = outcome.error or "unknown"
    if terminal or attempt >= MAX_ATTEMPTS:
        repo.mark_dead_lettered(
            delivery_id=row.id,
            attempt=attempt,
            now=now,
            last_status_code=outcome.status_code,
            last_error=error,
        )
        _log.info(
            "web push delivery dead-lettered",
            extra={
                "event": "messaging.push.dead_lettered",
                "delivery_id": row.id,
                "workspace_id": row.workspace_id,
                "attempt": attempt,
                "status_code": outcome.status_code,
                "error": error,
            },
        )
        return "dead_lettered"

    delay_index = attempt - 1
    if delay_index < 0 or delay_index >= len(BACKOFF_SCHEDULE_SECONDS):
        # Defensive fallback — exhausted budget but the ``terminal``
        # branch above should have caught it. Treat as dead-letter.
        repo.mark_dead_lettered(
            delivery_id=row.id,
            attempt=attempt,
            now=now,
            last_status_code=outcome.status_code,
            last_error=error,
        )
        return "dead_lettered"

    delay_s = BACKOFF_SCHEDULE_SECONDS[delay_index]
    next_attempt_at = now + timedelta(seconds=delay_s)
    repo.mark_transient(
        delivery_id=row.id,
        attempt=attempt,
        next_attempt_at=next_attempt_at,
        now=now,
        last_status_code=outcome.status_code,
        last_error=error,
    )
    _log.info(
        "web push delivery scheduled for retry",
        extra={
            "event": "messaging.push.retry",
            "delivery_id": row.id,
            "workspace_id": row.workspace_id,
            "attempt": attempt,
            "next_attempt_at": next_attempt_at.isoformat(),
            "status_code": outcome.status_code,
            "error": error,
        },
    )
    return "retry"


def _purge_token_and_mark_sent(
    *,
    repo: PushDeliveryRepository,
    row: PushDeliveryRow,
    token_user_id: str,
    token_id: str,
    attempt: int,
    now: datetime,
    last_status_code: int,
    clock: Clock,
) -> None:
    """410 / 404 — mark queue row sent, delete the token row, audit.

    The notification has already been delivered to the inbox + SSE +
    email at notify time; the push tier is best-effort. A 410 / 404
    means the browser uninstalled the push subscription and the
    queued row has nowhere to land — there's nothing to retry. Mark
    sent (terminal-clean) so the row drops off the worker's due-set.

    The order matters: ``push_token`` is the FK target of the queue
    row with ``ondelete='CASCADE'``, so deleting the token before the
    queue update would sweep the row away mid-update. We flip the
    queue row first, then drop the token, then audit on the
    surviving session.
    """
    repo.mark_sent(
        delivery_id=row.id,
        attempt=attempt,
        now=now,
        last_status_code=last_status_code,
    )

    deleted_user_id = repo.delete_token(push_token_id=token_id)
    if deleted_user_id is None:
        deleted_user_id = token_user_id

    ctx = _audit_context_for_workspace(workspace_id=row.workspace_id)
    write_audit(
        repo.session,
        ctx,
        entity_kind="push_token",
        entity_id=token_id,
        action="messaging.push.token_purged",
        diff={
            "user_id": deleted_user_id,
            "reason": f"http_{last_status_code}",
            "delivery_id": row.id,
        },
        via="worker",
        clock=clock,
    )
    _log.info(
        "web push token purged",
        extra={
            "event": "messaging.push.token_purged",
            "delivery_id": row.id,
            "workspace_id": row.workspace_id,
            "push_token_id": token_id,
            "status_code": last_status_code,
        },
    )


def _audit_context_for_workspace(*, workspace_id: str) -> WorkspaceContext:
    """Mint a system-actor context anchored on ``workspace_id``."""
    # The workspace slug is not a worker-accessible field through the
    # delivery row alone; the audit row only needs a workspace id +
    # the system actor. ``_system_actor_context`` is the canonical
    # builder used by every other cross-workspace worker tick (daily
    # digest, generator, overdue) so we follow its shape — passing a
    # placeholder slug keeps the value-equality contract on
    # :class:`WorkspaceContext` honest without an extra DB read.
    return _system_actor_context(
        workspace_id=workspace_id,
        workspace_slug="",
    )


def _load_vapid_config(
    repo: PushDeliveryRepository,
    *,
    workspace_id: str,
) -> _VapidConfig | None:
    """Read the per-workspace VAPID material; ``None`` when unconfigured."""
    private_key = repo.get_workspace_setting(
        workspace_id=workspace_id,
        settings_key=SETTINGS_KEY_VAPID_PRIVATE,
    )
    if private_key is None:
        return None
    subject = repo.get_workspace_setting(
        workspace_id=workspace_id,
        settings_key=SETTINGS_KEY_VAPID_SUBJECT,
    )
    if subject is None:
        subject = DEFAULT_VAPID_SUBJECT_FALLBACK
    return _VapidConfig(private_key=private_key, subject=subject)


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


def _make_web_push_dispatch_body(clock: Clock) -> Callable[[], None]:
    """Build the closure registered on APScheduler.

    Mirrors the shape of every sibling :file:`app/worker/jobs/*` body
    so :func:`app.worker.scheduler.register_jobs` can wrap it without
    a new code path. The body opens its own UoW per row through
    :func:`dispatch_due_pushes`; the wrapper handles heartbeat /
    metrics.
    """

    def _body() -> None:
        dispatch_due_pushes(clock=clock)

    return _body
