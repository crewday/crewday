"""APScheduler bootstrap + ``register_jobs`` hook.

The single seam through which downstream tasks (cd-j9l7 idempotency
sweep, cd-ca1k LLM-budget refresh, cd-dcl2 occurrence-generator fan-out,
cd-yqm4 user_workspace derive-refresh — all live) plug into the shared
scheduler.
Two entry-points exercise the same ``register_jobs`` call:

* **Inline (default)** — the FastAPI factory's lifespan hook starts
  the scheduler inside the web process when
  ``settings.worker == "internal"`` (§16 "Worker process"). No extra
  container is needed for single-VPS deployments (Recipe A of §16).
* **Standalone** — ``python -m app.worker`` boots an AsyncIO loop,
  registers the same job set, and handles SIGTERM / SIGINT for a
  graceful shutdown. The external Recipe B / D compose files
  (``worker:`` service) invoke this entrypoint.

**Scheduler class.** :class:`~apscheduler.schedulers.asyncio.AsyncIOScheduler`
— FastAPI's request loop is asyncio, and a sibling
:class:`~apscheduler.schedulers.background.BackgroundScheduler`
would create a second event loop in the same process with no
coordinated lifecycle. AsyncIO-native keeps start / stop consistent
across both entry-points.

**Job wrapping.** Every registered job goes through
:func:`wrap_job`, which:

1. Opens a fresh :class:`~app.adapters.db.session.UnitOfWorkImpl` per
   tick (never share a session across ticks — SQLAlchemy sessions
   are not safe for concurrent use and APScheduler may schedule
   overlapping runs if a tick overshoots its interval).
2. Logs ``worker.tick.start`` / ``worker.tick.end`` with the job id.
3. Catches every :class:`Exception` (never :class:`BaseException` —
   ``KeyboardInterrupt`` / ``SystemExit`` must propagate so the
   process can shut down cleanly). A crashing job logs at ERROR with
   the traceback and the next tick still runs.
4. On success, upserts the deployment-wide
   :class:`~app.adapters.db.ops.models.WorkerHeartbeat` row keyed by
   the job id — ``/readyz`` reads ``MAX(heartbeat_at)``, so any
   healthy tick is enough to flip readiness green.

**Idempotent start / stop.** Calling :func:`start` on an already-
running scheduler is a no-op (not a crash); :func:`stop` on a
stopped scheduler is likewise a no-op. Both paths short-circuit on
:attr:`AsyncIOScheduler.running` so a lifespan hook that double-
fires (process supervisor restarts, test fixtures) does not raise.

See ``docs/specs/01-architecture.md`` §"Worker" and
``docs/specs/16-deployment-operations.md`` §"Worker process",
§"Healthchecks".
"""

from __future__ import annotations

import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Settings
from app.observability.metrics import (
    WORKER_JOB_DURATION_SECONDS,
    WORKER_JOBS_TOTAL,
    sanitize_label,
)
from app.util.clock import Clock, SystemClock
from app.util.logging import new_request_id, reset_request_id, set_request_id
from app.worker import job_state
from app.worker.jobs import common as _common_jobs
from app.worker.jobs.agent import _make_agent_compaction_body
from app.worker.jobs.demo import _make_demo_gc_body, _make_demo_usage_rollup_body
from app.worker.jobs.identity import _make_user_workspace_refresh_body
from app.worker.jobs.llm_budget import _make_llm_budget_refresh_body
from app.worker.jobs.maintenance import (
    _heartbeat_only_body,
    _make_approval_ttl_body,
    _make_chat_gateway_sweep_body,
    _make_extract_document_body,
    _make_idempotency_sweep_body,
    _make_inventory_reorder_body,
    _make_invite_ttl_body,
    _make_retention_rotation_body,
    _make_webhook_dispatch_body,
)
from app.worker.jobs.messaging import _make_daily_digest_fanout_body
from app.worker.jobs.messaging_web_push import _make_web_push_dispatch_body
from app.worker.jobs.stays import _make_poll_ical_fanout_body
from app.worker.jobs.tasks import _make_generator_fanout_body, _make_overdue_fanout_body

_demo_expired_workspace_ids = _common_jobs._demo_expired_workspace_ids
_system_actor_context = _common_jobs._system_actor_context

__all__ = [
    "AGENT_COMPACTION_INTERVAL_SECONDS",
    "AGENT_COMPACTION_JOB_ID",
    "APPROVAL_TTL_INTERVAL_SECONDS",
    "APPROVAL_TTL_JOB_ID",
    "CHAT_GATEWAY_SWEEP_INTERVAL_SECONDS",
    "CHAT_GATEWAY_SWEEP_JOB_ID",
    "DAILY_DIGEST_JOB_ID",
    "DAILY_DIGEST_MISFIRE_GRACE_SECONDS",
    "DEMO_GC_INTERVAL_SECONDS",
    "DEMO_GC_JOB_ID",
    "DEMO_USAGE_ROLLUP_INTERVAL_SECONDS",
    "DEMO_USAGE_ROLLUP_JOB_ID",
    "EXTRACT_DOCUMENT_INTERVAL_SECONDS",
    "EXTRACT_DOCUMENT_JOB_ID",
    "GENERATOR_JOB_ID",
    "HEARTBEAT_JOB_ID",
    "HEARTBEAT_JOB_INTERVAL_SECONDS",
    "IDEMPOTENCY_SWEEP_JOB_ID",
    "INVENTORY_REORDER_JOB_ID",
    "INVITE_TTL_INTERVAL_SECONDS",
    "INVITE_TTL_JOB_ID",
    "LLM_BUDGET_REFRESH_INTERVAL_SECONDS",
    "LLM_BUDGET_REFRESH_JOB_ID",
    "OVERDUE_DETECT_INTERVAL_SECONDS",
    "OVERDUE_DETECT_JOB_ID",
    "POLL_ICAL_INTERVAL_SECONDS",
    "POLL_ICAL_JOB_ID",
    "POLL_ICAL_MISFIRE_GRACE_SECONDS",
    "RETENTION_ROTATION_JOB_ID",
    "USER_WORKSPACE_REFRESH_INTERVAL_SECONDS",
    "USER_WORKSPACE_REFRESH_JOB_ID",
    "WEBHOOK_DISPATCH_INTERVAL_SECONDS",
    "WEBHOOK_DISPATCH_JOB_ID",
    "WEB_PUSH_DISPATCH_INTERVAL_SECONDS",
    "WEB_PUSH_DISPATCH_JOB_ID",
    "create_scheduler",
    "register_jobs",
    "start",
    "stop",
    "wrap_job",
]

_log = logging.getLogger(__name__)


# Stable job id for the always-on heartbeat tick. Matches the string
# ``/readyz``'s freshness window tolerates (it reads
# ``MAX(heartbeat_at)``, not a specific name — any registered job
# bumps the same table — so choosing a descriptive id is a clarity
# thing, not a correctness thing). Pinned so tests and operators can
# grep for the row: "the bare-minimum liveness proof is
# ``scheduler_heartbeat``."
HEARTBEAT_JOB_ID: str = "scheduler_heartbeat"

# The heartbeat tick runs every 30 s, giving ``/readyz``'s 60 s
# staleness window a 2x safety margin against one skipped tick
# (scheduler pause during migration, momentary DB reconnect). Aligned
# with :mod:`app.api.health`'s ``_HEARTBEAT_STALE_AFTER`` comment.
HEARTBEAT_JOB_INTERVAL_SECONDS: int = 30

# Stable job id for the hourly generator tick (cd-dcl2). The
# per-workspace fan-out is built by
# :func:`_make_generator_fanout_body` and registered on the cron
# cadence ``CronTrigger(minute=0)`` — every workspace gets a
# :func:`~app.worker.tasks.generator.generate_task_occurrences` call
# under a system-actor :class:`WorkspaceContext`.
GENERATOR_JOB_ID: str = "generate_task_occurrences"

# Stable job id for the daily ``idempotency_key`` TTL sweep (cd-j9l7).
# Spec §12 "Idempotency" pins the TTL at 24 h; the sweep callable
# (:func:`app.api.middleware.idempotency.prune_expired_idempotency_keys`)
# removes rows older than that so the table never grows unbounded.
IDEMPOTENCY_SWEEP_JOB_ID: str = "idempotency_sweep"

# Stable job id for the 60 s LLM-budget aggregate refresh (cd-ca1k).
# Spec §11 "Workspace usage budget" §"Meter" pins the cadence:
# ``workspace_usage.cost_30d_cents`` is re-summed from the last 30
# days of ``llm_usage`` every 60 s so the cached aggregate never
# trails the meter by more than that window. The fan-out body
# iterates every workspace with a ``budget_ledger`` row, building a
# system-actor :class:`~app.tenancy.WorkspaceContext` per workspace
# and calling :func:`~app.domain.llm.budget.refresh_aggregate`.
LLM_BUDGET_REFRESH_JOB_ID: str = "llm_budget_refresh_aggregate"

# Interval for the LLM-budget refresh. Spec §11 pins 60 s; also
# matches the 60 s freshness promise surfaced on the admin /
# settings usage tile ("a cap edit reflects in the cached aggregate
# within 60 s"). Pulled out as a module-level constant so tests can
# import it rather than re-derive the number from the spec.
LLM_BUDGET_REFRESH_INTERVAL_SECONDS: int = 60

# Stable job id for the soft-overdue sweeper tick (cd-hurw). The
# per-workspace fan-out built by :func:`_make_overdue_fanout_body`
# calls :func:`~app.worker.tasks.overdue.detect_overdue` once per
# workspace under a system-actor :class:`WorkspaceContext`.
OVERDUE_DETECT_JOB_ID: str = "detect_overdue"

# Interval for the overdue sweeper. Spec §06 + cd-hurw pin 5 minutes
# (and surface the cadence as a per-workspace setting
# ``tasks.overdue_tick_seconds`` for future tuning). Pulled out as a
# module-level constant so tests and the scheduler-wiring code share
# the same number without re-deriving it from the spec.
OVERDUE_DETECT_INTERVAL_SECONDS: int = 300


# Stable job id for the 15 min iCal poller fan-out (cd-d48). The
# per-tick fan-out across workspaces is built by
# :func:`_make_poll_ical_fanout_body` and registered on the interval
# cadence ``IntervalTrigger(seconds=900)`` — every workspace gets a
# :func:`~app.worker.tasks.poll_ical.poll_ical` call under a system-actor
# :class:`WorkspaceContext`.
POLL_ICAL_JOB_ID: str = "stays.poll_ical"

# Interval for the iCal poller. Spec §04 "iCal feed" §"Polling
# behavior" pins the default ``poll_cadence`` at ``*/15 * * * *``;
# the worker tick fires every 15 min and the per-feed cadence guard
# inside :func:`poll_ical` skips feeds whose ``last_polled_at`` has
# not yet aged past the cadence window. Pulled out as a module-level
# constant so tests can pin the cadence boundary without re-deriving
# it from the spec.
POLL_ICAL_INTERVAL_SECONDS: int = 900

# Misfire grace window for the iCal poller. Spec brief pins 600 s:
# a scheduler restart that misses the firing instant by up to 10 min
# still gets to run the catch-up tick (the poll body is idempotent —
# unchanged feeds are 304 short-circuits and Blocked / cancelled
# VEVENTs upsert in-place). A two-tick-late firing is the staleness
# signal an operator dashboard wants to alert on, not a tick the
# scheduler should backfill.
POLL_ICAL_MISFIRE_GRACE_SECONDS: int = 600


# Stable job id for the ``user_workspace`` derive-refresh tick (cd-yqm4).
# The reconciler in
# :mod:`app.domain.identity.user_workspace_refresh` walks every active
# upstream (workspace-scoped role_grants, property-scoped role_grants
# resolved through ``property_workspace``, work_engagements; plus the
# forward-compat seams for ``org_workspace``) and brings the derived
# junction in line. Spec §02 "user_workspace" pins the table as
# derived; this job is the canonical reconciler.
USER_WORKSPACE_REFRESH_JOB_ID: str = "user_workspace_refresh"

# Interval for the user_workspace derive-refresh tick. The §02 spec
# does not pin a specific cadence — only that the worker keeps the
# junction reconciled. Five minutes is the default: small enough that
# a worker tick is the next thing the user sees after a grant is
# minted in the API (login redirect lands within one tick), large
# enough that the fan-out's full-table scan does not dominate the
# workload on a fleet with thousands of workspaces. Tests can pin
# this constant if they want to exercise the cadence boundary.
USER_WORKSPACE_REFRESH_INTERVAL_SECONDS: int = 300


# Stable job id for the approval-request TTL expiry sweep (cd-9ghv).
# The sweep callable in
# :mod:`app.worker.tasks.approval_ttl.sweep_expired_approvals` flips
# every ``approval_request`` row past its ``expires_at`` from
# ``status='pending'`` to ``status='timed_out'`` and re-emits one
# :class:`~app.events.types.ApprovalDecided` per row. Cross-tenant by
# design — the sweep is deployment-scope, not per-workspace, so the
# domain layer reads under ``tenant_agnostic`` and the SSE transport
# routes the per-row event to the right subscribers.
APPROVAL_TTL_JOB_ID: str = "approval_ttl_sweep"

# Interval for the approval-request TTL sweep. Spec §11 "TTL" defaults
# the row's ``expires_at`` to ``created_at + 7 days``; once a row has
# slipped past that boundary the queue depth on the desk surface
# should converge within one tick. 15 min matches the cron cadence
# pinned in the §11 prose (``*/15 * * * *``). The current cd-9ghv
# slice rides the existing ``ix_approval_request_workspace_status_
# created`` index for the per-tenant queue path; the cross-tenant
# TTL sweep falls back to a status-filtered scan (acceptable for
# v1 — pending counts are workspace-bounded). A future
# ``(status, expires_at)`` covering index is a Beads follow-up
# (cd-approval-ttl-index) for fleets with sustained pending depth.
APPROVAL_TTL_INTERVAL_SECONDS: int = 900


# Stable job id for the invite TTL expiry sweep (cd-za45). The sweep
# callable in :mod:`app.worker.tasks.invite_ttl.sweep_expired_invites`
# flips every ``invite`` row past its ``expires_at`` from
# ``state='pending'`` to ``state='expired'`` and re-emits one
# :class:`~app.events.types.InviteExpired` per row. Cross-tenant by
# design — like the sibling approval-TTL sweep, the body is
# deployment-scope (NOT per-workspace) so the domain layer reads under
# ``tenant_agnostic`` and the SSE transport routes the per-row event
# to the right manager subscribers.
INVITE_TTL_JOB_ID: str = "invite_ttl_sweep"

# Interval for the invite TTL sweep. Spec §03 "Additional users
# (invite → click-to-accept)" pins the magic-link TTL at 24 h; once a
# row's ``expires_at`` lapses the manager workspace-members surface
# should converge within one tick. 15 min matches the cadence pinned
# for the sibling :data:`APPROVAL_TTL_INTERVAL_SECONDS` — the same
# rationale applies (an idle fleet's pending depth is workspace-bounded
# so a status-filtered scan rides the existing ``ix_invite_expires``
# index without a covering one).
INVITE_TTL_INTERVAL_SECONDS: int = 900


# Stable job id for the outbound webhook dispatcher tick (cd-q885).
# The tick callable in
# :mod:`app.worker.tasks.webhook_dispatch.dispatch_due_webhooks` walks
# every ``webhook_delivery`` row whose ``status='pending'`` and
# ``next_attempt_at <= now`` and fires one HTTP POST attempt. Cross-
# tenant by design — like the approval-TTL sweep, the dispatcher is
# deployment-scope (NOT per-workspace) because the retry schedule is
# a global policy and per-workspace fan-out would scale linearly with
# workspace count.
WEBHOOK_DISPATCH_JOB_ID: str = "webhook_dispatch"

# Interval for the webhook dispatcher. Spec §10 "Retries" pins the
# six-step retry schedule at ``[0s, 30s, 5m, 1h, 6h, 24h]``; the
# dispatcher tick must fire often enough that a 30 s retry slot is
# honoured without a long lag, but not so often that an idle fleet
# burns CPU on every wakeup. 30 s matches the smallest non-zero
# retry interval — a tick hitting at the right boundary picks up
# the row exactly when its retry window opens. The deliver call is
# itself a no-op on rows in terminal state, so a tick that fires
# while no rows are due is cheap.
WEBHOOK_DISPATCH_INTERVAL_SECONDS: int = 30

# Stable job id for the chat-gateway dispatch safety-net sweep (cd-0gaa).
# The tick callable in
# :mod:`app.worker.tasks.chat_gateway_sweep.sweep_undispatched_messages`
# enumerates ``chat_message`` rows whose channel kind is
# ``chat_gateway`` and whose ``dispatched_to_agent_at`` is still
# ``NULL`` past the grace window, then re-publishes
# ``chat.message.received`` so the in-process dispatcher catches up.
# Cross-tenant by design — like the webhook dispatcher and approval-
# TTL sweep, the tick is deployment-scope; each row carries its own
# ``workspace_id`` and the audit row keys off that field through a
# system-actor :class:`WorkspaceContext`.
CHAT_GATEWAY_SWEEP_JOB_ID: str = "chat_gateway.agent_dispatch_sweep"

# Interval for the chat-gateway dispatch safety net. Spec §16
# "Worker process" pins ``agent_dispatch_sweep`` at every 30 s and
# §23 "Routing > Inbound" pins the matching pending grace at 60 s
# — a row left orphaned by an app-process crash gets picked up
# within ~two ticks of the grace window. The sweep body is itself
# idempotent (the dispatcher's CAS on ``dispatched_to_agent_at``
# makes a re-published event a no-op against any row a primary
# handler eventually catches up first), so a misfire that runs late
# or a coalesced tick is strictly safe.
CHAT_GATEWAY_SWEEP_INTERVAL_SECONDS: int = 30

# Stable job id for the hourly inventory reorder-point check (§08).
INVENTORY_REORDER_JOB_ID: str = "inventory.check_reorder_points"

# Stable job id for the daily digest fan-out (cd-f0ue). The job wakes
# hourly at minute zero and the worker body sends only recipients
# whose local clock is in the 07:00 hour, giving every timezone its
# per-recipient morning slot without registering one APScheduler job
# per timezone.
DAILY_DIGEST_JOB_ID: str = "messaging.daily_digest"
DAILY_DIGEST_MISFIRE_GRACE_SECONDS: int = 1800

# Stable job id for the web-push delivery worker (cd-y60x). The body
# walks the ``notification_push_queue`` staging table and fires
# :func:`pywebpush.webpush` calls bounded by a per-workspace
# semaphore. Tick fires every 60 s — the smallest non-zero entry on
# the §10 backoff schedule (``[30s, 2m, 10m, 1h]``) is 30 s, so a
# 60 s tick honours the schedule with at most one tick of lag for
# the first retry slot. ``misfire_grace_time`` matches the interval:
# one tick late is fine (the dispatcher is idempotent on rows in
# terminal state and re-attempts pending rows that were due);
# two-ticks-late is a signal the scheduler is stuck and a skip is
# preferable to a stacked catch-up.
WEB_PUSH_DISPATCH_JOB_ID: str = "messaging.web_push_dispatch"
WEB_PUSH_DISPATCH_INTERVAL_SECONDS: int = 60

# Stable job id for the agent conversation compaction worker (cd-cn7v).
# The body walks workspaces and lets :func:`compact_due_threads` decide
# which chat channels have crossed the §11 trigger bounds.
AGENT_COMPACTION_JOB_ID: str = "agent.conversation_compaction"
AGENT_COMPACTION_INTERVAL_SECONDS: int = 300

DEMO_GC_JOB_ID: str = "demo_gc"
DEMO_GC_INTERVAL_SECONDS: int = 900
DEMO_USAGE_ROLLUP_JOB_ID: str = "demo_usage_rollup"
DEMO_USAGE_ROLLUP_INTERVAL_SECONDS: int = 60

# Stable job id for §15 operational-log retention. The body archives
# rows past each workspace's configured retention window to
# ``$DATA_DIR/archive/<table>.jsonl.gz`` and deletes the originals.
RETENTION_ROTATION_JOB_ID: str = "rotate_operational_logs"

# Stable job id for the document text-extraction worker tick (cd-mo9e).
# The tick callable in
# :mod:`app.worker.tasks.extract_document.extract_pending_documents`
# walks every ``file_extraction`` row in ``status='pending'``, opens
# a fresh UoW per row, and runs the v1 passthrough rung (text/* ->
# extracted; everything else -> unsupported). Cross-tenant by design
# — like the webhook dispatcher and TTL sweeps, the tick is
# deployment-scope (NOT per-workspace) because the rung pipeline is a
# global policy and per-row UoWs already carry the row's own
# ``workspace_id`` for audit + SSE routing.
EXTRACT_DOCUMENT_JOB_ID: str = "extract_document"

# Interval for the document extraction sweep. 30 s matches the
# sibling :data:`WEBHOOK_DISPATCH_INTERVAL_SECONDS` cadence — small
# enough that an upload-then-tick round-trip lands inside one tick
# for the manager who just dropped a file, large enough that an idle
# fleet doesn't burn CPU on every wakeup. The body is itself
# idempotent (rows in terminal state fall out of the predicate, and
# the per-row ``start_extraction`` flips ``pending -> extracting``
# atomically) so a misfire that runs late or a coalesced tick is
# strictly safe.
EXTRACT_DOCUMENT_INTERVAL_SECONDS: int = 30


# Job-body type. Downstream tasks supply either a synchronous callable
# (most of today's jobs — pure SQL + logging) or an ``async def``
# coroutine function (for jobs that need to ``await`` an async client
# such as the future LLM fan-out). The wrapper below branches:
#
# * sync bodies run inside :func:`asyncio.to_thread` so a blocking DB
#   op never starves the event loop — :meth:`AsyncIOScheduler.add_job`
#   without an explicit executor would otherwise run ``def`` jobs on
#   the loop itself and block every other coroutine.
# * async bodies are awaited directly on the loop; running them through
#   :func:`asyncio.to_thread` would call the coroutine function, return
#   an un-awaited coroutine object, and silently skip the body (logged
#   only as a ``RuntimeWarning`` — the heartbeat upsert would still
#   succeed and ``/readyz`` would stay green while the work vanished).
JobBody = Callable[[], None] | Callable[[], Awaitable[None]]


def create_scheduler(*, clock: Clock | None = None) -> AsyncIOScheduler:
    """Return a fresh :class:`AsyncIOScheduler` — not yet started.

    No jobs are added here; call :func:`register_jobs` next. The
    ``clock`` is stashed on the scheduler instance (under a pinned
    attribute name, not the standard APScheduler timezone field) so
    the job wrappers can reach it for heartbeat timestamps. We do not
    override APScheduler's internal clock because the library's own
    scheduling math is driven off the OS clock regardless; injecting
    a :class:`~app.util.clock.FrozenClock` only affects our business
    logic (the heartbeat column and any job body that reads it), not
    APScheduler's internal "when should I fire next" decisions.

    The scheduler defaults to UTC so trigger times are unambiguous
    across deployments — matches §01's "Time is UTC at rest" rule.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    scheduler = AsyncIOScheduler(timezone="UTC")
    # Stash the clock on the instance so ``wrap_job`` can reach it
    # without every downstream caller having to thread it through.
    # ``_crewday_clock`` uses a leading underscore + crewday prefix
    # so it does not collide with APScheduler's own attributes if the
    # library adds a ``clock`` hook in a future release.
    scheduler._crewday_clock = resolved_clock
    return scheduler


def _clock_for(scheduler: AsyncIOScheduler) -> Clock:
    """Return the :class:`Clock` stashed on ``scheduler`` or the system clock.

    A scheduler built outside :func:`create_scheduler` (unexpected,
    but possible if a caller wires APScheduler directly) falls back
    to :class:`SystemClock` rather than raising — the heartbeat is
    still correct, it just can't be driven by a test fixture.
    """
    clock = getattr(scheduler, "_crewday_clock", None)
    if isinstance(clock, Clock):
        return clock
    return SystemClock()


def wrap_job(
    func: JobBody,
    *,
    job_id: str,
    clock: Clock,
    heartbeat: bool = True,
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Return an async wrapper APScheduler can register as a job.

    The wrapper:

    1. Logs ``worker.tick.start`` at INFO with the job id.
    2. Runs ``func`` — detected once at wrap time:

       * ``async def`` / returns an awaitable → awaited directly on
         the event loop.
       * plain ``def`` → run via :func:`asyncio.to_thread` so a
         blocking DB op never starves the event loop.

       Mixing the two in one scheduler is supported, but each job's
       shape is pinned at registration and doesn't change per tick.
    3. On success, opens a fresh UoW and upserts the heartbeat row
       keyed by ``job_id`` (unless ``heartbeat=False`` for jobs that
       opt out — e.g. future jobs that write a per-tenant heartbeat
       of their own).
    4. Swallows + logs every :class:`Exception`. A job that keeps
       raising does not take the whole scheduler down; its heartbeat
       stops advancing and ``/readyz`` goes red via the staleness
       window — the natural escalation signal.
    5. Logs ``worker.tick.end`` at INFO with ``ok: True|False`` so
       operators can grep for stuck jobs.

    ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``) is
    deliberately NOT caught — the process shutdown path needs those
    to propagate so the scheduler can run its own cleanup.
    """
    import asyncio  # local import — asyncio is only needed when called
    import time as _time

    is_coroutine = inspect.iscoroutinefunction(func)
    job_label = sanitize_label(job_id)

    async def _runner() -> None:
        # Bind a fresh request_id per tick so structured-log records
        # the body emits get correlated end-to-end (the §16
        # "Observability / Logs" key contract). Worker ticks are the
        # subprocess-equivalent of an HTTP request — they need the
        # same id discipline so an operator scraping the JSON stream
        # can isolate one tick's lines from another's.
        request_id_token = set_request_id(new_request_id())
        start = _time.perf_counter()
        _log.info(
            "worker tick starting",
            extra={"event": "worker.tick.start", "job_id": job_id},
        )
        try:
            # Killswitch (cd-8euz). A previous burst of failures has
            # tipped this job's ``worker_heartbeat`` row into the
            # ``dead`` state and an operator has not yet cleared it
            # via ``crewday admin worker reset-job``. Skip the body,
            # do not advance the heartbeat (so ``/readyz`` still
            # surfaces the staleness), and bump the
            # ``status="dead"`` counter so dashboards can distinguish
            # the killswitch-skip from a legitimate ok / error tick.
            #
            # The killswitch read opens its own UoW; a transient DB
            # outage here must NOT escape the wrapper (the original
            # cd-7c0p contract was that ``wrap_job`` swallows every
            # ``Exception`` so the broker stays alive). On a read
            # failure we fail-open — log and run the body — because
            # fail-closed would skip every tick on a momentary
            # connectivity blip and the staleness window already
            # escalates a persistent outage via ``/readyz``.
            killswitch_dead = False
            if heartbeat:
                try:
                    killswitch_dead = await asyncio.to_thread(_is_dead, job_id)
                except Exception:
                    _log.exception(
                        "worker killswitch read failed; running body",
                        extra={
                            "event": "worker.job.killswitch_read_error",
                            "job_id": job_id,
                        },
                    )
            if killswitch_dead:
                WORKER_JOBS_TOTAL.labels(job=job_label, status="dead").inc()
                _log.warning(
                    "worker tick skipped: job is dead",
                    extra={"event": "worker.tick.dead_skip", "job_id": job_id},
                )
                return

            ok = False
            try:
                if is_coroutine:
                    # ``func`` is ``async def`` — invoke and await on
                    # the event loop. ``asyncio.to_thread`` would call
                    # the coroutine function, hand back an un-awaited
                    # coroutine object, and silently skip the body.
                    result = func()
                    if inspect.isawaitable(result):
                        await result
                else:
                    # Sync body — offload to the default executor so a
                    # blocking DB op does not pin the event loop.
                    await asyncio.to_thread(func)
                ok = True
            except Exception:
                # The job body's own logging (if any) fires first;
                # this backstop guarantees a record even if the body
                # swallowed.
                _log.exception(
                    "worker tick failed",
                    extra={"event": "worker.tick.error", "job_id": job_id},
                )

            if ok and heartbeat:
                try:
                    await asyncio.to_thread(_write_heartbeat, job_id, clock)
                except Exception:
                    # A heartbeat write failure is itself a signal —
                    # log and move on. The next successful tick will
                    # try again; if every tick fails the heartbeat
                    # goes stale and ``/readyz`` catches it.
                    _log.exception(
                        "worker heartbeat write failed",
                        extra={
                            "event": "worker.heartbeat.error",
                            "job_id": job_id,
                        },
                    )
                    ok = False
            elif not ok and heartbeat:
                try:
                    await asyncio.to_thread(_record_failure, job_id, clock)
                except Exception:
                    # The failure-state writer raising would mask the
                    # original tick failure if we let it propagate.
                    # Log and move on — the next failing tick retries
                    # the increment, and a persistent DB outage
                    # surfaces via ``/readyz`` going red anyway.
                    _log.exception(
                        "worker failure-state write failed",
                        extra={
                            "event": "worker.job.state_error",
                            "job_id": job_id,
                        },
                    )

            duration = _time.perf_counter() - start
            WORKER_JOB_DURATION_SECONDS.labels(job=job_label).observe(duration)
            WORKER_JOBS_TOTAL.labels(
                job=job_label,
                status="ok" if ok else "error",
            ).inc()

            _log.info(
                "worker tick finished",
                extra={
                    "event": "worker.tick.end",
                    "job_id": job_id,
                    "ok": ok,
                },
            )
        finally:
            # Always restore — even if the heartbeat / metric path
            # raised — so the request id ContextVar does not leak
            # into the next tick scheduled on the same task.
            reset_request_id(request_id_token)

    return _runner


def _write_heartbeat(job_id: str, clock: Clock) -> None:
    """Advance the heartbeat + clear cd-8euz failure state for ``job_id``.

    Opens its own :class:`~app.adapters.db.session.UnitOfWorkImpl` so
    the heartbeat commit is independent of the job body's session —
    a job that failed halfway through its own transaction must not
    roll back the heartbeat row (and vice versa).

    Delegates to :func:`app.worker.job_state.record_success` so a
    successful tick also resets ``consecutive_failures`` and clears
    ``dead_at``; a job that recovered without operator intervention
    leaves the dead state automatically. The legacy
    :func:`upsert_heartbeat` helper stays exported for the cd-7c0p
    callers (tests + integration) that drive the row directly.
    """
    job_state.record_success(job_id=job_id, clock=clock)


def _is_dead(job_id: str) -> bool:
    """Read the ``dead_at`` flag for ``job_id``.

    Thin wrapper so unit tests can monkeypatch the seam without
    importing :mod:`app.worker.job_state`. The wrapper opens its own
    UoW under the covers — see :func:`app.worker.job_state.is_dead`.
    """
    return job_state.is_dead(job_id=job_id)


def _record_failure(job_id: str, clock: Clock) -> job_state.FailureOutcome:
    """Record a failed tick + emit threshold-crossing audits.

    Thin wrapper so unit tests can monkeypatch the seam without
    importing :mod:`app.worker.job_state`. Returns the outcome the
    underlying writer hands back; the wrapper itself only logs.
    """
    return job_state.record_failure(job_id=job_id, clock=clock)


def register_jobs(
    scheduler: AsyncIOScheduler,
    *,
    clock: Clock | None = None,
    settings: Settings | None = None,
) -> None:
    """Register the standard job set on ``scheduler``.

    Downstream tasks (cd-j9l7, cd-yqm4, the per-workspace occurrence
    generator fan-out) extend this function by adding one call to
    :meth:`scheduler.add_job` per job, each wrapped in
    :func:`wrap_job`. Keeping the registration in one function lets
    the lifespan hook and the ``__main__`` entrypoint share the same
    job set without copying the body.

    Idempotent — re-invoking ``register_jobs`` on the same scheduler
    (test fixtures, supervised restart, module reload) removes any
    existing job with the same id before the re-add. Note that
    APScheduler's ``replace_existing=True`` only deduplicates when
    the scheduler is actually running (started jobs live in the
    jobstore); on a not-yet-started scheduler the pending-jobs
    buffer is append-only, so an explicit :meth:`remove_job` is
    required. We do both so a started and a pending scheduler
    behave the same.
    """
    resolved_clock = clock if clock is not None else _clock_for(scheduler)
    demo_mode = settings.demo_mode if settings is not None else False

    # Drop any pre-existing entries for the ids we're about to add
    # so the registration is idempotent regardless of scheduler
    # state (see docstring). :class:`JobLookupError` is the expected
    # path when the id is not present (first register_jobs call);
    # we suppress it narrowly rather than swallowing ``Exception``
    # so a genuinely broken jobstore still surfaces.
    for pending_id in (
        HEARTBEAT_JOB_ID,
        GENERATOR_JOB_ID,
        IDEMPOTENCY_SWEEP_JOB_ID,
        LLM_BUDGET_REFRESH_JOB_ID,
        OVERDUE_DETECT_JOB_ID,
        POLL_ICAL_JOB_ID,
        USER_WORKSPACE_REFRESH_JOB_ID,
        APPROVAL_TTL_JOB_ID,
        INVITE_TTL_JOB_ID,
        WEBHOOK_DISPATCH_JOB_ID,
        CHAT_GATEWAY_SWEEP_JOB_ID,
        EXTRACT_DOCUMENT_JOB_ID,
        INVENTORY_REORDER_JOB_ID,
        DAILY_DIGEST_JOB_ID,
        RETENTION_ROTATION_JOB_ID,
        WEB_PUSH_DISPATCH_JOB_ID,
        AGENT_COMPACTION_JOB_ID,
        DEMO_GC_JOB_ID,
        DEMO_USAGE_ROLLUP_JOB_ID,
    ):
        with contextlib.suppress(JobLookupError):
            scheduler.remove_job(pending_id)

    # --- Always-on heartbeat ---
    # The simplest-possible job: write the heartbeat row and return.
    # ``/readyz`` reads ``MAX(heartbeat_at)`` across the whole table
    # so every wrapped job contributes to readiness, but this job
    # exists so the worker has SOMETHING to bump even when no
    # domain-level tick fires (e.g. during the window between the
    # scheduler starting and the hourly generator's first run).
    scheduler.add_job(
        wrap_job(_heartbeat_only_body, job_id=HEARTBEAT_JOB_ID, clock=resolved_clock),
        trigger=IntervalTrigger(seconds=HEARTBEAT_JOB_INTERVAL_SECONDS),
        id=HEARTBEAT_JOB_ID,
        name=HEARTBEAT_JOB_ID,
        replace_existing=True,
        # Fire immediately on scheduler start so ``/readyz`` flips
        # green within the first tick window rather than waiting the
        # full interval — important for container restart smoke tests
        # and for the integration suite in this change.
        next_run_time=None,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=HEARTBEAT_JOB_INTERVAL_SECONDS,
    )

    # --- Hourly occurrence generator fan-out (cd-dcl2) ---
    # Single-workspace callable in ``app/worker/tasks/generator.py``;
    # the per-tick fan-out across workspaces is built by
    # :func:`_make_generator_fanout_body`. Cron-anchored at the top
    # of every hour for the same operator-dashboard reasons cited in
    # the idempotency-sweep block (cron cadence is stable across
    # container restarts; an interval trigger would re-anchor on
    # every ``scheduler.start()``).
    scheduler.add_job(
        wrap_job(
            _make_generator_fanout_body(resolved_clock),
            job_id=GENERATOR_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=CronTrigger(minute=0),
        id=GENERATOR_JOB_ID,
        name=GENERATOR_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        # Tolerate a scheduler restart that misses the top-of-hour
        # firing by up to 10 min — running the tick late is strictly
        # better than skipping it, because the generator's own
        # idempotency (partial unique on ``(schedule_id,
        # scheduled_for_local)``) makes a late run safe.
        misfire_grace_time=600,
    )

    # --- Daily ``idempotency_key`` TTL sweep (cd-j9l7) ---
    # Spec §12 "Idempotency" pins the cache TTL at 24 h. Without a
    # periodic sweep the table grows unbounded — every retry adds a
    # row and nothing deletes them. We schedule a CRON trigger at
    # 03:00 UTC rather than an ``IntervalTrigger(hours=24)`` for two
    # reasons:
    #   1. Cron-based cadence is stable across container restarts; an
    #      interval trigger re-anchors on each ``scheduler.start()``
    #      so a deployment that restarts at noon would end up sweeping
    #      at noon every day — harmless, but harder to reason about
    #      from an operator dashboard that expects a fixed slot.
    #   2. 03:00 UTC lands in the lowest-traffic window for the
    #      North-Atlantic / European user base §16 assumes; the bulk
    #      ``DELETE ... WHERE created_at < cutoff`` takes a brief row
    #      lock on the backing index, so running it at the quiet hour
    #      keeps the p99 of a concurrent ``POST`` retry low.
    # ``misfire_grace_time=3600`` covers a scheduler restart around
    # 03:00 — running the sweep up to an hour late is strictly better
    # than skipping the day entirely. The callable is itself
    # idempotent (``DELETE`` where ``created_at < cutoff`` over rows
    # all older than the cutoff reaches zero after one run) so a
    # duplicate run is free.
    scheduler.add_job(
        wrap_job(
            _make_idempotency_sweep_body(resolved_clock),
            job_id=IDEMPOTENCY_SWEEP_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=CronTrigger(hour=3, minute=0),
        id=IDEMPOTENCY_SWEEP_JOB_ID,
        name=IDEMPOTENCY_SWEEP_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # --- Daily operational-log retention rotation (cd-vrfg) ---
    scheduler.add_job(
        wrap_job(
            _make_retention_rotation_body(resolved_clock),
            job_id=RETENTION_ROTATION_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=CronTrigger(hour=3, minute=30),
        id=RETENTION_ROTATION_JOB_ID,
        name=RETENTION_ROTATION_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # --- 60 s LLM budget aggregate refresh (cd-ca1k) ---
    # Spec §11 "Workspace usage budget" §"Meter" pins a 60 s cadence.
    # The fan-out body iterates every workspace, builds a system-actor
    # :class:`~app.tenancy.WorkspaceContext`, and calls
    # :func:`~app.domain.llm.budget.refresh_aggregate`. The per-workspace
    # call is idempotent (it rewrites ``spent_cents`` from the last 30
    # days of ``llm_usage``), so a misfire that runs late or a coalesced
    # tick is strictly safe.
    #
    # ``misfire_grace_time=90`` — one tick late is tolerated (idempotent
    # rewrite) but a two-tick-late run is a signal the scheduler is
    # stuck and a skip is preferable to a stacked catch-up.
    # ``coalesce=True`` + ``max_instances=1`` keep a slow refresh from
    # stacking ticks on an overloaded DB.
    scheduler.add_job(
        wrap_job(
            _make_llm_budget_refresh_body(resolved_clock),
            job_id=LLM_BUDGET_REFRESH_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=IntervalTrigger(seconds=LLM_BUDGET_REFRESH_INTERVAL_SECONDS),
        id=LLM_BUDGET_REFRESH_JOB_ID,
        name=LLM_BUDGET_REFRESH_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=90,
    )

    # --- 5 min soft-overdue sweeper fan-out (cd-hurw) ---
    # Single-workspace callable in ``app/worker/tasks/overdue.py``;
    # the per-tick fan-out across workspaces is built by
    # :func:`_make_overdue_fanout_body`. Interval-anchored at 5 min;
    # the cadence matches the spec default and the
    # ``tasks.overdue_tick_seconds`` workspace setting (whose
    # per-tenant override is the cd-settings-cascade follow-up — the
    # scheduler wires the deployment-wide default for now). The
    # detect_overdue body is itself idempotent (the load query
    # excludes ``state='overdue'`` rows and the per-row UPDATE
    # re-asserts the source-state predicate so a manual transition
    # between ticks is preserved), so a misfire that runs late or a
    # coalesced tick is strictly safe.
    scheduler.add_job(
        wrap_job(
            _make_overdue_fanout_body(resolved_clock),
            job_id=OVERDUE_DETECT_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=IntervalTrigger(seconds=OVERDUE_DETECT_INTERVAL_SECONDS),
        id=OVERDUE_DETECT_JOB_ID,
        name=OVERDUE_DETECT_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        # ``misfire_grace_time = OVERDUE_DETECT_INTERVAL_SECONDS`` —
        # one tick late is fine (the body is idempotent), two-ticks
        # late is a signal the scheduler is stuck and a skip is
        # preferable to a stacked catch-up that hammers the DB on an
        # already-strained host.
        misfire_grace_time=OVERDUE_DETECT_INTERVAL_SECONDS,
    )

    # --- 15 min iCal poller fan-out (cd-d48) ---
    # Single-workspace callable in ``app/worker/tasks/poll_ical.py``;
    # the per-tick fan-out across workspaces is built by
    # :func:`_make_poll_ical_fanout_body`. Interval-anchored at 15 min;
    # the cadence matches the spec §04 default ``*/15 * * * *`` and
    # the per-feed cadence guard inside :func:`poll_ical` filters
    # feeds whose ``last_polled_at`` has not yet aged past the
    # window — so a tick on a workspace whose feeds have all been
    # polled inside the last 15 min is a near-no-op.
    #
    # ``misfire_grace_time = POLL_ICAL_MISFIRE_GRACE_SECONDS`` (10 min):
    # a scheduler restart that misses the firing instant by up to
    # 10 min still gets to run the catch-up tick (the body is
    # idempotent — unchanged feeds are 304 short-circuits, Blocked
    # / cancelled VEVENTs upsert in-place). Past 10 min a skip is
    # preferable to a stacked catch-up that hammers upstream iCal
    # endpoints + the per-host rate-limit on a fleet returning from
    # a long pause.
    if not demo_mode:
        scheduler.add_job(
            wrap_job(
                _make_poll_ical_fanout_body(resolved_clock),
                job_id=POLL_ICAL_JOB_ID,
                clock=resolved_clock,
            ),
            trigger=IntervalTrigger(seconds=POLL_ICAL_INTERVAL_SECONDS),
            id=POLL_ICAL_JOB_ID,
            name=POLL_ICAL_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=POLL_ICAL_MISFIRE_GRACE_SECONDS,
        )

    # --- 5 min user_workspace derive-refresh (cd-yqm4) ---
    # The reconciler in
    # :mod:`app.domain.identity.user_workspace_refresh` is the
    # canonical writer for the derived junction (§02
    # "user_workspace"). Domain services (signup, grant, invite,
    # remove_member) write the upstream rows; this tick brings
    # ``user_workspace`` in line.
    #
    # ``misfire_grace_time = USER_WORKSPACE_REFRESH_INTERVAL_SECONDS``
    # — one tick late is fine (the reconciler is idempotent: it
    # rewrites the same set), but two-ticks-late is a signal the
    # scheduler is stuck and a skip is preferable to a stacked
    # catch-up. ``coalesce=True`` + ``max_instances=1`` keep a slow
    # reconcile from stacking ticks on an overloaded DB.
    scheduler.add_job(
        wrap_job(
            _make_user_workspace_refresh_body(resolved_clock),
            job_id=USER_WORKSPACE_REFRESH_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=IntervalTrigger(seconds=USER_WORKSPACE_REFRESH_INTERVAL_SECONDS),
        id=USER_WORKSPACE_REFRESH_JOB_ID,
        name=USER_WORKSPACE_REFRESH_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=USER_WORKSPACE_REFRESH_INTERVAL_SECONDS,
    )

    # --- 15 min approval-request TTL sweep (cd-9ghv) ---
    # The sweep callable in
    # :mod:`app.worker.tasks.approval_ttl.sweep_expired_approvals`
    # flips every ``approval_request`` row past its ``expires_at``
    # from ``status='pending'`` to ``status='timed_out'`` and emits
    # one :class:`~app.events.types.ApprovalDecided` per row. The
    # sweep is deployment-scope (NOT per-workspace) — see the module
    # docstring for the cross-tenant rationale.
    #
    # ``misfire_grace_time = APPROVAL_TTL_INTERVAL_SECONDS`` — one
    # tick late is fine (the sweep is idempotent: rows in terminal
    # state fall out of the predicate), two-ticks-late is a signal
    # the scheduler is stuck and a skip is preferable to a stacked
    # catch-up that hammers the DB on a fleet returning from a long
    # pause. ``coalesce=True`` + ``max_instances=1`` keep a slow
    # sweep from stacking ticks on an overloaded DB.
    scheduler.add_job(
        wrap_job(
            _make_approval_ttl_body(resolved_clock),
            job_id=APPROVAL_TTL_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=IntervalTrigger(seconds=APPROVAL_TTL_INTERVAL_SECONDS),
        id=APPROVAL_TTL_JOB_ID,
        name=APPROVAL_TTL_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=APPROVAL_TTL_INTERVAL_SECONDS,
    )

    # --- 15 min invite TTL expiry sweep (cd-za45) ---
    # The sweep callable in
    # :mod:`app.worker.tasks.invite_ttl.sweep_expired_invites`
    # flips every ``invite`` row past its ``expires_at`` from
    # ``state='pending'`` to ``state='expired'`` and emits one
    # :class:`~app.events.types.InviteExpired` per row. The sweep is
    # deployment-scope (NOT per-workspace) — see the module docstring
    # for the cross-tenant rationale. Mirrors the sibling
    # :data:`APPROVAL_TTL_JOB_ID` registration shape.
    #
    # ``misfire_grace_time = INVITE_TTL_INTERVAL_SECONDS`` — one tick
    # late is fine (the sweep is idempotent: rows in terminal state
    # fall out of the predicate), two-ticks-late is a signal the
    # scheduler is stuck and a skip is preferable to a stacked
    # catch-up. ``coalesce=True`` + ``max_instances=1`` keep a slow
    # sweep from stacking ticks on an overloaded DB.
    scheduler.add_job(
        wrap_job(
            _make_invite_ttl_body(resolved_clock),
            job_id=INVITE_TTL_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=IntervalTrigger(seconds=INVITE_TTL_INTERVAL_SECONDS),
        id=INVITE_TTL_JOB_ID,
        name=INVITE_TTL_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=INVITE_TTL_INTERVAL_SECONDS,
    )

    # --- 30 s outbound webhook dispatcher tick (cd-q885) ---
    # The tick callable in
    # :mod:`app.worker.tasks.webhook_dispatch.dispatch_due_webhooks`
    # walks every ``webhook_delivery`` row whose ``status='pending'``
    # and ``next_attempt_at <= now`` and fires one HTTP POST attempt.
    # The §10 retry schedule is ``[0s, 30s, 5m, 1h, 6h, 24h]`` so the
    # tick fires every 30 s — small enough to honour the smallest
    # non-zero retry slot without a long lag, large enough that an
    # idle fleet doesn't burn CPU on every wakeup.
    #
    # ``misfire_grace_time = WEBHOOK_DISPATCH_INTERVAL_SECONDS`` —
    # one tick late is fine (the dispatcher is idempotent on rows in
    # terminal state and re-attempts pending rows that were due);
    # two-ticks-late is a signal the scheduler is stuck and a skip
    # is preferable to a stacked catch-up. ``coalesce=True`` +
    # ``max_instances=1`` keep a slow dispatcher run from stacking
    # ticks on a long upstream timeout.
    if not demo_mode:
        scheduler.add_job(
            wrap_job(
                _make_webhook_dispatch_body(resolved_clock),
                job_id=WEBHOOK_DISPATCH_JOB_ID,
                clock=resolved_clock,
            ),
            trigger=IntervalTrigger(seconds=WEBHOOK_DISPATCH_INTERVAL_SECONDS),
            id=WEBHOOK_DISPATCH_JOB_ID,
            name=WEBHOOK_DISPATCH_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=WEBHOOK_DISPATCH_INTERVAL_SECONDS,
        )

    # --- 30 s chat-gateway dispatch safety-net sweep (cd-0gaa) ---
    # The tick callable in
    # :mod:`app.worker.tasks.chat_gateway_sweep.sweep_undispatched_messages`
    # walks every ``chat_message`` row whose channel kind is
    # ``chat_gateway``, whose ``dispatched_to_agent_at`` is still
    # ``NULL``, and whose ``created_at`` is older than 30 s, then
    # re-publishes ``chat.message.received`` so the in-process
    # dispatcher catches up. The dispatcher's CAS on
    # ``dispatched_to_agent_at`` keeps the re-publish idempotent.
    #
    # ``misfire_grace_time = CHAT_GATEWAY_SWEEP_INTERVAL_SECONDS`` —
    # one tick late is fine (the body is idempotent: rows still
    # carrying ``dispatched_to_agent_at IS NULL`` are simply
    # re-fired, and rows the dispatcher caught up first fall out of
    # the predicate); two-ticks-late is a signal the scheduler is
    # stuck and a skip is preferable to a stacked catch-up that
    # hammers the bus on a fleet returning from a long pause.
    # ``coalesce=True`` + ``max_instances=1`` keep a slow sweep from
    # stacking ticks on an overloaded DB.
    scheduler.add_job(
        wrap_job(
            _make_chat_gateway_sweep_body(resolved_clock),
            job_id=CHAT_GATEWAY_SWEEP_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=IntervalTrigger(seconds=CHAT_GATEWAY_SWEEP_INTERVAL_SECONDS),
        id=CHAT_GATEWAY_SWEEP_JOB_ID,
        name=CHAT_GATEWAY_SWEEP_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=CHAT_GATEWAY_SWEEP_INTERVAL_SECONDS,
    )

    # --- 30 s document text-extraction worker (cd-mo9e) ---
    # The tick callable in
    # :mod:`app.worker.tasks.extract_document.extract_pending_documents`
    # walks every ``file_extraction`` row in ``status='pending'``,
    # opens a fresh UoW per row, and runs the v1 passthrough rung
    # (text/* -> ``succeeded`` | ``empty``; everything else ->
    # ``unsupported``). The PDF / DOCX / OCR rungs are §21 follow-ups.
    #
    # ``misfire_grace_time = EXTRACT_DOCUMENT_INTERVAL_SECONDS`` —
    # one tick late is fine (the body is idempotent: rows in terminal
    # state fall out of the predicate, and the per-row
    # ``start_extraction`` flip handles concurrent ticks via the
    # ``pending`` -> ``extracting`` transition); two-ticks-late is a
    # signal the scheduler is stuck and a skip is preferable to a
    # stacked catch-up. ``coalesce=True`` + ``max_instances=1`` keep
    # a slow rung (a future PDF extractor) from stacking ticks on an
    # overloaded host.
    scheduler.add_job(
        wrap_job(
            _make_extract_document_body(resolved_clock),
            job_id=EXTRACT_DOCUMENT_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=IntervalTrigger(seconds=EXTRACT_DOCUMENT_INTERVAL_SECONDS),
        id=EXTRACT_DOCUMENT_JOB_ID,
        name=EXTRACT_DOCUMENT_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=EXTRACT_DOCUMENT_INTERVAL_SECONDS,
    )

    # --- Hourly inventory reorder-point check (cd-kxr0) ---
    scheduler.add_job(
        wrap_job(
            _make_inventory_reorder_body(resolved_clock),
            job_id=INVENTORY_REORDER_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=CronTrigger(minute=0),
        id=INVENTORY_REORDER_JOB_ID,
        name=INVENTORY_REORDER_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # --- Hourly daily-digest fan-out (cd-f0ue) ---
    # The body sends only users whose recipient-local clock is in the
    # 07:00 hour, so an hourly UTC trigger covers every timezone
    # without registering per-timezone jobs.
    if not demo_mode:
        scheduler.add_job(
            wrap_job(
                _make_daily_digest_fanout_body(resolved_clock),
                job_id=DAILY_DIGEST_JOB_ID,
                clock=resolved_clock,
            ),
            trigger=CronTrigger(minute=0),
            id=DAILY_DIGEST_JOB_ID,
            name=DAILY_DIGEST_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=DAILY_DIGEST_MISFIRE_GRACE_SECONDS,
        )

    # --- 60 s web-push delivery dispatcher (cd-y60x) ---
    # The body walks the ``notification_push_queue`` staging table and
    # fires :func:`pywebpush.webpush` for due rows. Cross-tenant by
    # design — like the webhook dispatcher and approval-TTL sweep, the
    # tick is deployment-scope; each row carries its own ``workspace_
    # id`` so the per-row audit (token-purge on 410 / 404) keys off
    # that field through a system-actor :class:`WorkspaceContext`.
    if not demo_mode:
        scheduler.add_job(
            wrap_job(
                _make_web_push_dispatch_body(resolved_clock),
                job_id=WEB_PUSH_DISPATCH_JOB_ID,
                clock=resolved_clock,
            ),
            trigger=IntervalTrigger(seconds=WEB_PUSH_DISPATCH_INTERVAL_SECONDS),
            id=WEB_PUSH_DISPATCH_JOB_ID,
            name=WEB_PUSH_DISPATCH_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=WEB_PUSH_DISPATCH_INTERVAL_SECONDS,
        )

    if not demo_mode:
        scheduler.add_job(
            wrap_job(
                _make_agent_compaction_body(resolved_clock),
                job_id=AGENT_COMPACTION_JOB_ID,
                clock=resolved_clock,
            ),
            trigger=IntervalTrigger(seconds=AGENT_COMPACTION_INTERVAL_SECONDS),
            id=AGENT_COMPACTION_JOB_ID,
            name=AGENT_COMPACTION_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=AGENT_COMPACTION_INTERVAL_SECONDS,
        )

    if demo_mode:
        assert settings is not None
        scheduler.add_job(
            wrap_job(
                _make_demo_gc_body(settings, resolved_clock),
                job_id=DEMO_GC_JOB_ID,
                clock=resolved_clock,
            ),
            trigger=IntervalTrigger(seconds=DEMO_GC_INTERVAL_SECONDS),
            id=DEMO_GC_JOB_ID,
            name=DEMO_GC_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=DEMO_GC_INTERVAL_SECONDS,
        )
        scheduler.add_job(
            wrap_job(
                _make_demo_usage_rollup_body(resolved_clock),
                job_id=DEMO_USAGE_ROLLUP_JOB_ID,
                clock=resolved_clock,
            ),
            trigger=IntervalTrigger(seconds=DEMO_USAGE_ROLLUP_INTERVAL_SECONDS),
            id=DEMO_USAGE_ROLLUP_JOB_ID,
            name=DEMO_USAGE_ROLLUP_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=90,
        )


def start(scheduler: AsyncIOScheduler) -> None:
    """Start ``scheduler`` if it isn't already running.

    Idempotent: calling :func:`start` twice — or on a scheduler that
    another lifespan hook already started — is a no-op. APScheduler
    itself raises :class:`SchedulerAlreadyRunningError` on a double
    start, which would otherwise turn a benign supervisor restart
    into a boot-blocking exception.
    """
    if scheduler.running:
        _log.debug(
            "scheduler already running; start is a no-op",
            extra={"event": "worker.scheduler.start_noop"},
        )
        return
    scheduler.start()
    _log.info(
        "scheduler started",
        extra={"event": "worker.scheduler.started"},
    )


def stop(scheduler: AsyncIOScheduler, *, wait: bool = False) -> None:
    """Stop ``scheduler`` if it's running; no-op otherwise.

    ``wait`` is forwarded to :meth:`AsyncIOScheduler.shutdown` — the
    default ``False`` returns immediately without draining pending
    runs, which is what a SIGTERM handler wants (a supervised
    process restart must not hang on a slow job body). The lifespan
    hook in the FastAPI factory can override with ``wait=True`` if
    graceful shutdown is preferred and the ASGI shutdown deadline
    is generous enough.
    """
    if not scheduler.running:
        _log.debug(
            "scheduler not running; stop is a no-op",
            extra={"event": "worker.scheduler.stop_noop"},
        )
        return
    scheduler.shutdown(wait=wait)
    _log.info(
        "scheduler stopped",
        extra={"event": "worker.scheduler.stopped", "waited": wait},
    )


# ---------------------------------------------------------------------------
# Diagnostic helpers — used by tests and the ``__main__`` entrypoint
# ---------------------------------------------------------------------------


def registered_job_ids(scheduler: AsyncIOScheduler) -> tuple[str, ...]:
    """Return the sorted tuple of job ids currently registered.

    Exists so the unit tests can assert shape without reaching into
    APScheduler internals. :meth:`AsyncIOScheduler.get_jobs` is the
    documented API; we wrap it so the return is a deterministic
    tuple (the underlying list is insertion-ordered, which is fine,
    but sorting makes the test assertion trivially stable under a
    future reordering of the register_jobs body).
    """
    jobs: list[Any] = scheduler.get_jobs()
    return tuple(sorted(job.id for job in jobs))
