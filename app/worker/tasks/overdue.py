"""``detect_overdue`` — soft-overdue sweeper tick (cd-hurw).

Walks every live task in the caller's workspace whose
``state IN ('scheduled', 'pending', 'in_progress')`` and whose
``ends_at + grace_minutes`` has slipped below ``now``, flips them to
``state='overdue'``, stamps ``overdue_since=now``, and emits one
:class:`~app.events.types.TaskOverdue` per row.

Idempotent by construction: a task already in ``state='overdue'`` is
excluded from the load query (``state IN (...)`` filter), so a second
tick over the same data set inserts no audit, writes no row, fires no
event. The full sweep summary lands as a ``tasks.overdue_tick`` audit
row (one per workspace per tick) so operator dashboards can chart
flip rate + per-property breakdown over time.

Public surface:

* :class:`OverdueReport` — counts the worker returns (and the audit
  payload it writes). Frozen + slotted so the audit writer can
  flatten to JSON deterministically and tests can equality-check
  the full shape.
* :func:`detect_overdue` — the entry point. Signature
  ``(ctx, *, options=DetectOverdueOptions(...)) -> OverdueReport``.

**Manual-transition safety.** Between the SELECT (load eligible
candidates) and the per-row UPDATE (flip state), a worker / manager
may have manually transitioned the task (start, complete, skip,
cancel). To avoid clobbering a deliberate move, the per-row UPDATE
re-asserts the ``state IN ('scheduled', 'pending', 'in_progress')``
predicate in the WHERE clause: a manual transition lands first; the
sweeper's UPDATE matches zero rows and the deliberate move stands.
The skip is silent (no event, no audit beyond the per-tick summary),
matching the spec's "soft state never overwrites a manual transition
that happened between ticks" invariant.

**Settings.** The grace window and tick cadence are spec'd as
settings (``tasks.overdue_grace_minutes`` and
``tasks.overdue_tick_seconds``). Tick cadence is workspace-scoped;
the grace window resolves through the shared §02 cascade for each
candidate task when the caller does not pass an explicit
``grace_minutes`` override.

**WorkspaceContext** is threaded through every DB read and event
publish. The worker never reads tenancy from the environment; the
caller (APScheduler tick fan-out, CLI invocation, test) resolves a
context per workspace before calling in.

See ``docs/specs/06-tasks-and-scheduling.md`` §"State machine"
("overdue is soft, never terminal; manual transitions clear
``overdue_since``") and
``docs/specs/16-deployment-operations.md`` §"Worker process".
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from app.adapters.db.messaging.audiences import (
    list_owner_manager_user_ids,
    list_owner_user_ids,
)
from app.adapters.db.tasks.models import Occurrence
from app.adapters.notifications.service import SqlAlchemyNotificationSink
from app.audit import write_audit
from app.domain.llm.notifications import (
    AnomalyDetectedView,
    AnomalyNotificationOptions,
    notify_anomaly_detected,
)
from app.domain.settings.cascade import (
    SettingScopeChain,
    resolve_most_specific,
    task_scope_chain,
)
from app.domain.tasks.notifications import (
    LegacyTaskOptions,
    TaskNotificationRuntime,
    TaskNotificationSink,
    TaskOverdueNotification,
    notify_task_overdue,
)
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import TaskOverdue
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.clock import aware_utc as _ensure_utc

__all__ = [
    "DEFAULT_OVERDUE_GRACE_MINUTES",
    "DEFAULT_OVERDUE_TICK_SECONDS",
    "SETTINGS_KEY_OVERDUE_GRACE_MINUTES",
    "SETTINGS_KEY_OVERDUE_TICK_SECONDS",
    "DetectOverdueOptions",
    "OverdueReport",
    "detect_overdue",
    "resolve_overdue_grace_minutes",
    "resolve_overdue_tick_seconds",
]


# ---------------------------------------------------------------------------
# Constants + setting keys
# ---------------------------------------------------------------------------


# §06 default grace window. The per-workspace setting
# ``tasks.overdue_grace_minutes`` overrides; the constant is the
# fallback when the key is unset (or the workspace row is missing).
# Pinned at 15 min to match spec §06 + the cd-hurw migration's
# backfill. Keeping the literal here means the worker, the migration,
# and the test fixtures all agree on the default without an indirect
# import.
DEFAULT_OVERDUE_GRACE_MINUTES: Final[int] = 15

# §06 default tick cadence. Surfaced as a constant so the
# scheduler-wiring callsite can import it instead of re-deriving the
# 5-minute boundary from the spec.
DEFAULT_OVERDUE_TICK_SECONDS: Final[int] = 300

# Dotted keys inside ``workspace.settings_json``. The §02 settings
# cascade owns the namespace (``tasks.*`` for task-domain knobs); the
# key strings are pinned here so the worker, the API admin surface,
# and the (future) settings-cascade resolver line up.
SETTINGS_KEY_OVERDUE_GRACE_MINUTES: Final[str] = "tasks.overdue_grace_minutes"
SETTINGS_KEY_OVERDUE_TICK_SECONDS: Final[str] = "tasks.overdue_tick_seconds"


# Source states the sweeper will flip. Any other state — ``completed``,
# ``skipped``, ``cancelled``, ``approved``, or already ``overdue`` —
# is left untouched. Pulled out so the load-query filter and the
# per-row UPDATE guard reference the same tuple (the manual-transition
# safety invariant only holds when the two predicates agree).
_FLIPPABLE_STATES: Final[tuple[str, ...]] = ("scheduled", "pending", "in_progress")


# ---------------------------------------------------------------------------
# Settings resolvers
# ---------------------------------------------------------------------------


def _resolve_int_setting(
    session: Session,
    *,
    chain: SettingScopeChain,
    key: str,
    default: int,
) -> int:
    """Resolve a positive integer setting through the shared cascade.

    Returns ``default`` for any of:

    * the key is absent;
    * the value is not coercible to a positive integer.

    A non-positive value (zero or negative) collapses to ``default``
    too: a zero grace window or zero tick cadence is almost certainly
    a misconfiguration, and the worker would otherwise either flip
    every just-ended task instantly (grace=0) or never tick (tick=0).
    The conservative posture matches the rest of the worker's
    "missing setting → fall back to spec default" stance.
    """
    raw = resolve_most_specific(session, key, chain, default=default)
    if isinstance(raw, bool):
        # ``isinstance(True, int)`` is ``True`` in Python; explicitly
        # reject bool so a stray ``"key": true`` in the settings JSON
        # does not silently coerce to ``1``.
        return default
    if isinstance(raw, int) and raw > 0:
        return raw
    return default


def resolve_overdue_grace_minutes(session: Session, *, workspace_id: str) -> int:
    """Resolve ``tasks.overdue_grace_minutes`` for a workspace.

    This public helper intentionally answers the workspace root value
    used by scheduler/audit callers. The sweeper resolves the full
    task-scoped cascade internally when ``detect_overdue`` receives no
    explicit ``grace_minutes`` override.
    """
    return _resolve_int_setting(
        session,
        chain=SettingScopeChain(workspace_id=workspace_id),
        key=SETTINGS_KEY_OVERDUE_GRACE_MINUTES,
        default=DEFAULT_OVERDUE_GRACE_MINUTES,
    )


def resolve_overdue_tick_seconds(session: Session, *, workspace_id: str) -> int:
    """Resolve ``tasks.overdue_tick_seconds`` for a workspace.

    Mirror of :func:`resolve_overdue_grace_minutes` for the cadence
    knob the scheduler uses. Currently unused inside the worker
    body itself — exposed so the scheduler-wiring layer (cd-hurw
    extension to :mod:`app.worker.scheduler`) can read it without
    importing the same dotted key string twice. Falls back to
    :data:`DEFAULT_OVERDUE_TICK_SECONDS`.
    """
    return _resolve_int_setting(
        session,
        chain=SettingScopeChain(workspace_id=workspace_id),
        key=SETTINGS_KEY_OVERDUE_TICK_SECONDS,
        default=DEFAULT_OVERDUE_TICK_SECONDS,
    )


def _resolve_task_overdue_grace_minutes(
    session: Session,
    *,
    workspace_id: str,
    task: Occurrence,
) -> int:
    """Resolve overdue grace for the task's normal performer cascade."""

    return _resolve_int_setting(
        session,
        chain=task_scope_chain(
            task, workspace_id=workspace_id, actor_user_id=task.assignee_user_id
        ),
        key=SETTINGS_KEY_OVERDUE_GRACE_MINUTES,
        default=DEFAULT_OVERDUE_GRACE_MINUTES,
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OverdueReport:
    """Summary counts for one ``detect_overdue`` invocation.

    Frozen + slotted so the audit writer can flatten to JSON
    deterministically and tests can equality-check the full shape.

    * ``flipped_count`` — rows the sweeper transitioned to
      ``overdue`` (i.e. the UPDATE actually landed). The denominator
      for the per-tick fan-out's structured-log summary.
    * ``skipped_already_overdue`` — candidates the load query saw in
      ``state='overdue'`` already. Always zero today (the load query
      excludes that state), kept on the report so a future widening
      that re-enrols stuck rows has a place to surface them without a
      shape change.
    * ``skipped_manual_transition`` — candidates whose per-row UPDATE
      matched zero rows because a concurrent manual transition
      landed between SELECT and UPDATE. The §06 "soft state never
      overwrites a manual transition" invariant materialised in a
      counter.
    * ``per_property_breakdown`` — ``{property_id: count}``, only
      properties with ``flipped_count > 0`` appear. Personal /
      workspace-scoped tasks (``property_id IS NULL``) are bucketed
      under the empty string key so the dict is JSON-serialisable
      without a magic ``None``.
    * ``flipped_task_ids`` — ULIDs of the rows the sweeper flipped,
      so callers (tests, operator dashboards) can walk the set
      without re-querying.
    * ``tick_started_at`` / ``tick_ended_at`` — sweeper bookends.
      Useful for measuring per-tick duration in the audit feed
      without joining heartbeat rows.
    """

    flipped_count: int
    skipped_already_overdue: int
    skipped_manual_transition: int
    per_property_breakdown: Mapping[str, int]
    tick_started_at: datetime
    tick_ended_at: datetime
    flipped_task_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DetectOverdueOptions:
    session: Session
    now: datetime | None = None
    clock: Clock | None = None
    grace_minutes: int | None = None
    event_bus: EventBus | None = None
    notifications: TaskNotificationSink | None = None


@dataclass(frozen=True, slots=True)
class _OverdueRuntime:
    ctx: WorkspaceContext
    session: Session
    now: datetime
    clock: Clock
    bus: EventBus
    notifications: TaskNotificationSink
    default_notifications: SqlAlchemyNotificationSink
    grace_minutes: int | None


@dataclass(slots=True)
class _OverdueAccumulator:
    flipped_task_ids: list[str] = field(default_factory=list)
    per_property_breakdown: dict[str, int] = field(default_factory=dict)
    skipped_manual_transition: int = 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def detect_overdue(
    ctx: WorkspaceContext,
    *,
    options: DetectOverdueOptions | None = None,
    **legacy_options: object,
) -> OverdueReport:
    """Run one sweeper tick for the caller's workspace.

    ``now`` pins the comparison instant; if omitted it is taken from
    ``clock`` (or :class:`~app.util.clock.SystemClock` if ``clock`` is
    also omitted). Both are exposed so tests can drive the worker
    deterministically.

    ``grace_minutes`` overrides the settings cascade; if ``None`` the
    worker resolves ``tasks.overdue_grace_minutes`` per candidate task.
    Tests that need a deterministic global grace pass the kwarg.

    Returns an :class:`OverdueReport`. Writes one
    ``tasks.overdue_tick`` audit row at the end of the run with the
    full count set + per-property breakdown. Publishes one
    :class:`~app.events.types.TaskOverdue` per flipped row.

    Does **not** commit the session; the caller's Unit-of-Work owns
    the transaction boundary (§01 "Key runtime invariants" #3).

    Raises :class:`ValueError` on a non-positive ``grace_minutes``
    override — a zero or negative grace is almost certainly a caller
    bug (would flip every just-ended task instantly).
    """
    runtime = _build_overdue_runtime(
        ctx, _detect_overdue_options(options, legacy_options)
    )
    audit_grace = (
        runtime.grace_minutes
        if runtime.grace_minutes is not None
        else resolve_overdue_grace_minutes(
            runtime.session, workspace_id=ctx.workspace_id
        )
    )
    tick_started_at = runtime.now
    totals = _OverdueAccumulator()

    for task in _load_overdue_candidates(runtime):
        _process_overdue_candidate(runtime, task, totals)

    tick_ended_at = runtime.clock.now()

    _write_overdue_tick_audit(
        runtime.session,
        ctx,
        flipped_count=len(totals.flipped_task_ids),
        skipped_already_overdue=0,
        skipped_manual_transition=totals.skipped_manual_transition,
        per_property_breakdown=totals.per_property_breakdown,
        grace_minutes=audit_grace,
        tick_started_at=tick_started_at,
        tick_ended_at=tick_ended_at,
        clock=runtime.clock,
    )

    return OverdueReport(
        flipped_count=len(totals.flipped_task_ids),
        skipped_already_overdue=0,
        skipped_manual_transition=totals.skipped_manual_transition,
        per_property_breakdown=totals.per_property_breakdown,
        tick_started_at=tick_started_at,
        tick_ended_at=tick_ended_at,
        flipped_task_ids=tuple(totals.flipped_task_ids),
    )


def _detect_overdue_options(
    options: DetectOverdueOptions | None,
    legacy_options: dict[str, object],
) -> DetectOverdueOptions:
    reader = LegacyTaskOptions("detect_overdue", legacy_options)
    reader.reject_if_combined(options is not None)
    if options is not None:
        return options
    session = reader.pop_session()
    now = reader.pop_datetime("now")
    clock = reader.pop_clock("clock")
    grace_minutes = reader.pop_int("grace_minutes")
    event_bus = reader.pop_event_bus("event_bus")
    notifications = reader.pop_notifications("notifications")
    reader.reject_unknown()
    return DetectOverdueOptions(
        session=session,
        now=now,
        clock=clock,
        grace_minutes=grace_minutes,
        event_bus=event_bus,
        notifications=notifications,
    )


def _build_overdue_runtime(
    ctx: WorkspaceContext,
    options: DetectOverdueOptions,
) -> _OverdueRuntime:
    if options.grace_minutes is not None and options.grace_minutes <= 0:
        raise ValueError(
            f"grace_minutes must be a positive integer; got {options.grace_minutes}"
        )
    resolved_clock = options.clock if options.clock is not None else SystemClock()
    resolved_now = options.now if options.now is not None else resolved_clock.now()
    if resolved_now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime in UTC")
    resolved_bus = (
        options.event_bus if options.event_bus is not None else default_event_bus
    )
    default_notifications = SqlAlchemyNotificationSink(
        options.session,
        ctx,
        clock=resolved_clock,
        bus=resolved_bus,
    )
    return _OverdueRuntime(
        ctx=ctx,
        session=options.session,
        now=resolved_now,
        clock=resolved_clock,
        bus=resolved_bus,
        notifications=options.notifications or default_notifications,
        default_notifications=default_notifications,
        grace_minutes=options.grace_minutes,
    )


def _load_overdue_candidates(runtime: _OverdueRuntime) -> list[Occurrence]:
    candidate_cutoff = (
        runtime.now - timedelta(minutes=runtime.grace_minutes)
        if runtime.grace_minutes is not None
        else runtime.now
    )
    return list(
        runtime.session.scalars(
            select(Occurrence)
            .where(Occurrence.workspace_id == runtime.ctx.workspace_id)
            .where(Occurrence.state.in_(_FLIPPABLE_STATES))
            .where(Occurrence.ends_at < candidate_cutoff)
            .order_by(Occurrence.id.asc())
        ).all()
    )


def _process_overdue_candidate(
    runtime: _OverdueRuntime,
    task: Occurrence,
    totals: _OverdueAccumulator,
) -> None:
    ends_at_aware = _ensure_utc(task.ends_at)
    task_grace = _task_grace_minutes(runtime, task)
    if ends_at_aware >= runtime.now - timedelta(minutes=task_grace):
        return
    if not _flip_task_overdue(runtime, task):
        totals.skipped_manual_transition += 1
        return

    totals.flipped_task_ids.append(task.id)
    bucket_key = task.property_id if task.property_id is not None else ""
    totals.per_property_breakdown[bucket_key] = (
        totals.per_property_breakdown.get(bucket_key, 0) + 1
    )
    _publish_overdue(runtime, task, ends_at_aware)


def _task_grace_minutes(runtime: _OverdueRuntime, task: Occurrence) -> int:
    if runtime.grace_minutes is not None:
        return runtime.grace_minutes
    return _resolve_task_overdue_grace_minutes(
        runtime.session, workspace_id=runtime.ctx.workspace_id, task=task
    )


def _flip_task_overdue(runtime: _OverdueRuntime, task: Occurrence) -> bool:
    result = runtime.session.execute(
        update(Occurrence)
        .where(Occurrence.id == task.id)
        .where(Occurrence.workspace_id == runtime.ctx.workspace_id)
        .where(Occurrence.state.in_(_FLIPPABLE_STATES))
        .values(state="overdue", overdue_since=runtime.now)
    )
    assert isinstance(result, CursorResult)
    return result.rowcount != 0


def _publish_overdue(
    runtime: _OverdueRuntime,
    task: Occurrence,
    ends_at_aware: datetime,
) -> None:
    slipped_seconds = (runtime.now - ends_at_aware).total_seconds()
    slipped_minutes = max(0, math.floor(slipped_seconds / 60))
    runtime.bus.publish(
        TaskOverdue(
            workspace_id=runtime.ctx.workspace_id,
            actor_id=runtime.ctx.actor_id,
            correlation_id=runtime.ctx.audit_correlation_id,
            occurred_at=runtime.now,
            task_id=task.id,
            assigned_user_id=task.assignee_user_id,
            overdue_since=runtime.now,
            slipped_minutes=slipped_minutes,
        )
    )
    notify_task_overdue(
        TaskNotificationRuntime(
            runtime.session,
            runtime.ctx,
            runtime.clock,
            runtime.bus,
            runtime.notifications,
        ),
        notification=TaskOverdueNotification(
            task=task,
            overdue_since=runtime.now,
            slipped_minutes=slipped_minutes,
            recipient_user_ids=_overdue_recipient_user_ids(runtime, task),
        ),
    )
    if task.is_personal:
        return
    notify_anomaly_detected(
        runtime.session,
        runtime.ctx,
        anomaly=AnomalyDetectedView(
            anomaly_kind="task_missed",
            subject_kind="task",
            subject_id=task.id,
            window_start=ends_at_aware,
            window_end=runtime.now,
            detected_at=runtime.now,
            title=task.title or "Task missed its scheduled window",
            explanation=_missed_task_explanation(task, slipped_minutes),
            severity="warning",
        ),
        options=AnomalyNotificationOptions(
            clock=runtime.clock,
            bus=runtime.bus,
            recipient_user_ids=list_owner_manager_user_ids(
                runtime.session, workspace_id=runtime.ctx.workspace_id
            ),
            sink=runtime.default_notifications,
        ),
    )


def _overdue_recipient_user_ids(
    runtime: _OverdueRuntime,
    task: Occurrence,
) -> Sequence[str]:
    audience = list_owner_user_ids if task.is_personal else list_owner_manager_user_ids
    return audience(runtime.session, workspace_id=runtime.ctx.workspace_id)


def _missed_task_explanation(task: Occurrence, slipped_minutes: int) -> str:
    minute_label = "minute" if slipped_minutes == 1 else "minutes"
    return (
        f"{task.title or 'Task'} is {slipped_minutes} "
        f"{minute_label} past its scheduled end."
    )


# ---------------------------------------------------------------------------
# Audit writer
# ---------------------------------------------------------------------------


def _write_overdue_tick_audit(  # code-health: ignore[params] Audit payload boundary.  # noqa: E501
    session: Session,
    ctx: WorkspaceContext,
    *,
    flipped_count: int,
    skipped_already_overdue: int,
    skipped_manual_transition: int,
    per_property_breakdown: Mapping[str, int],
    grace_minutes: int,
    tick_started_at: datetime,
    tick_ended_at: datetime,
    clock: Clock,
) -> None:
    """Record the per-tick summary row.

    §06 asks for one ``tasks.overdue_tick`` audit entry with the full
    count set + per-property breakdown so operators can chart flip
    rate (and the breakdown) over time. Anchored on the workspace —
    ``entity_id = workspace_id`` — matching the
    ``schedules.generation_tick`` convention from
    :func:`app.worker.tasks.generator.generate_task_occurrences`.
    """
    write_audit(
        session,
        ctx,
        entity_kind="workspace",
        entity_id=ctx.workspace_id,
        action="tasks.overdue_tick",
        diff={
            "flipped_count": flipped_count,
            "skipped_already_overdue": skipped_already_overdue,
            "skipped_manual_transition": skipped_manual_transition,
            "per_property_breakdown": dict(per_property_breakdown),
            "grace_minutes": grace_minutes,
            "tick_started_at": tick_started_at.isoformat(),
            "tick_ended_at": tick_ended_at.isoformat(),
        },
        clock=clock,
    )
