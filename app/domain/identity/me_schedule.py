"""Self-service schedule aggregator (cd-6uij).

Read-only helper that powers ``GET /me/schedule`` (§12 "Self-service
shortcuts" / §14 "Schedule view"). Walks the §06 availability stack +
the assigned-task table for the caller and projects every row covering
a date window into the wire shape consumed by the worker
``/schedule`` page.

This module is the **single read seam** for the schedule feed. The
HTTP router in :mod:`app.api.v1.me_schedule` is a thin DTO passthrough
that forwards to :func:`aggregate_schedule` and serialises the
:class:`SchedulePayload`.

Public surface:

* **DTOs** — :class:`WeeklySlotView`, :class:`TaskRefView`,
  :class:`PublicHolidayView`, :class:`PendingItems`,
  :class:`SchedulePayload`.
* **Aggregator** — :func:`aggregate_schedule`. Takes a
  :class:`MeScheduleQueryRepository` plus a :class:`WorkspaceContext`
  plus optional ``from_date`` / ``to_date`` (defaults to the §12
  ``[today, today+14d]`` window) and returns a
  :class:`SchedulePayload`.

**Self-only by construction.** Every read predicate is keyed on
``ctx.actor_id`` — a worker cannot use this surface to leak another
user's leaves, overrides, or assigned tasks. The router does **not**
expose a ``user_id`` query param: managers wanting cross-user
visibility use the per-resource generic endpoints (`/user_leaves`,
`/user_availability_overrides`, `/tasks?assignee_user_id=…`).

**Approved vs pending.** Per §12 "Self-service shortcuts" the
aggregator returns approved leaves + overrides + holidays inline, and
pending leaves + overrides under :attr:`SchedulePayload.pending` so
the UI can render "pending approval" state without treating a
not-yet-approved row as live in the precedence stack.

**No audit.** Read-only — the aggregator never writes a row. The
:class:`WorkspaceContext` carries the actor id we filter on; no
``write_audit`` call lands.

**Tenancy.** The ORM tenant filter auto-narrows every SELECT issued
by the SA-backed
:class:`~app.adapters.db.identity.repositories.SqlAlchemyMeScheduleQueryRepository`;
the repo also re-asserts the ``workspace_id = ctx.workspace_id``
predicate explicitly as defence-in-depth (matches the sibling
:mod:`app.domain.identity.user_leaves` /
:mod:`app.domain.identity.user_availability_overrides` shape).

**Holiday country matching is intentionally simple in v1.** The
aggregator returns every :class:`PublicHolidayRow` whose calendar
date falls in the window, regardless of ``country``. Country
narrowing per the user's primary property requires the
Stay/Property join the ``/me/schedule`` page does not yet drive — a
follow-up Beads task lands the country-aware filter once the
property timezone surface catches up. Annual recurrence is also
deferred: v1 surfaces the literal calendar date the row carries,
not the recurring anchor.

**Architecture (cd-lot5).** The module talks to a
:class:`~app.domain.identity.me_schedule_ports.MeScheduleQueryRepository`
Protocol — never to the SQLAlchemy model classes. The SA-backed
concretion lives in
:mod:`app.adapters.db.identity.repositories`; unit tests inject
fakes or wire the SA repo over an in-memory SQLite session. Mirrors
the cd-r5j2 / cd-2upg pattern shipped for the sibling
:mod:`app.domain.identity.user_availability_overrides` /
:mod:`app.domain.identity.user_leaves` services.

See ``docs/specs/12-rest-api.md`` §"Self-service shortcuts";
``docs/specs/06-tasks-and-scheduling.md`` §"user_leave",
§"user_availability_overrides", §"Weekly availability",
§"public_holidays".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from app.domain.identity.me_schedule_ports import (
    MeScheduleQueryRepository,
    OccurrenceRefRow,
    PublicHolidayRow,
)
from app.domain.identity.user_availability_overrides import (
    UserAvailabilityOverrideView,
)

# Reuse the sibling services' seam-Row → View projections rather than
# duplicating them here (cd-lot5 dropped the inlined ``_override_row_to_view``
# / ``_leave_row_to_view`` shipped by cd-r5j2 / cd-2upg). The underscore
# crossing is intentional: both helpers are stable per-row projections of
# the same seam Row shape this module already imports, and inlining a third
# copy would invite drift between the three sites the moment a column
# lands.
from app.domain.identity.user_availability_overrides import (
    _row_to_view as _override_row_to_view,
)
from app.domain.identity.user_leaves import (
    UserLeaveView,
)
from app.domain.identity.user_leaves import (
    _row_to_view as _leave_row_to_view,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock

__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "PendingItems",
    "PublicHolidayView",
    "SchedulePayload",
    "TaskRefView",
    "WeeklySlotView",
    "aggregate_schedule",
]


# §12 "Self-service shortcuts" pins the default window at
# ``[today, today+14d]``. Pulled out as a module constant so a future
# UX change lands in one place + the test suite can reference it
# without re-encoding the literal.
DEFAULT_WINDOW_DAYS: int = 14


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WeeklySlotView:
    """One row of the caller's standing weekly availability pattern.

    Mirrors :class:`~app.adapters.db.availability.models.UserWeeklyAvailability`
    minus the workspace + user keys (the caller knows both already).
    Both ``starts_local`` and ``ends_local`` are NULL when the worker
    is "off" that weekday — the §06 BOTH-OR-NEITHER invariant.
    """

    weekday: int
    starts_local: time | None
    ends_local: time | None


@dataclass(frozen=True, slots=True)
class TaskRefView:
    """Lightweight reference to a task assigned to the caller in the window.

    Per the cd-6uij task description: "task ids + scheduled_for_local".
    The full :class:`~app.adapters.db.tasks.models.Occurrence` shape
    lives at ``/tasks/{id}``; the schedule feed only needs enough to
    drop a marker on the calendar.

    ``scheduled_for_local`` is the property-local ISO-8601 string the
    scheduler worker stamped at generation time; the SA adapter falls
    back to the UTC ``starts_at`` ISO when the column is null (legacy
    rows pre-cd-22e).
    """

    id: str
    scheduled_for_local: str


@dataclass(frozen=True, slots=True)
class PublicHolidayView:
    """Read projection of a :class:`PublicHolidayRow` covering the window.

    Pared down to the columns the worker calendar needs — the manager
    configuration screen at ``/public_holidays`` carries the full
    edit shape. ``payroll_multiplier`` is a :class:`~decimal.Decimal`
    on the row; the wire JSON serialises it as a string to preserve
    Decimal semantics across SQLite (TEXT) and Postgres (numeric).
    """

    id: str
    name: str
    date: date
    country: str | None
    scheduling_effect: str
    reduced_starts_local: time | None
    reduced_ends_local: time | None
    payroll_multiplier: Decimal | None


@dataclass(frozen=True, slots=True)
class PendingItems:
    """Pending leaves + overrides bucketed away from the live precedence stack.

    The §06 invariant is that **only approved** leaves / overrides
    affect candidate-pool selection. Surfacing them inline alongside
    approved rows would invite the UI to render them as live; surfacing
    them under a dedicated bucket lets the worker see "I asked for X,
    awaiting approval" without confusing the assignment authority.
    """

    leaves: list[UserLeaveView]
    overrides: list[UserAvailabilityOverrideView]


@dataclass(frozen=True, slots=True)
class SchedulePayload:
    """Aggregated calendar feed for the caller across ``[from_date, to_date]``.

    The wire envelope mirrors the §12 "Self-service shortcuts"
    description verbatim:

    * ``rota`` — the caller's seven-row weekly pattern (Mon..Sun).
    * ``tasks`` — :class:`~app.adapters.db.tasks.models.Occurrence`
      rows assigned to the caller whose ``starts_at`` falls inside
      the window.
    * ``leaves`` — approved :class:`~app.adapters.db.availability.models.UserLeave`
      rows overlapping the window.
    * ``overrides`` — approved
      :class:`~app.adapters.db.availability.models.UserAvailabilityOverride`
      rows inside the window.
    * ``holidays`` — :class:`~app.adapters.db.holidays.models.PublicHoliday`
      rows whose calendar date falls in the window.
    * ``pending`` — pending :class:`~app.adapters.db.availability.models.UserLeave`
      + :class:`~app.adapters.db.availability.models.UserAvailabilityOverride`
      rows; explicitly bucketed so the UI does not promote them into
      the live precedence stack.

    ``from_date`` / ``to_date`` are echoed back so a caller that fell
    through to the default window sees the resolved bounds without a
    second round trip.
    """

    from_date: date
    to_date: date
    rota: list[WeeklySlotView]
    tasks: list[TaskRefView]
    leaves: list[UserLeaveView]
    overrides: list[UserAvailabilityOverrideView]
    holidays: list[PublicHolidayView]
    pending: PendingItems


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _holiday_row_to_view(row: PublicHolidayRow) -> PublicHolidayView:
    """Project a seam-level :class:`PublicHolidayRow` into the public view."""
    return PublicHolidayView(
        id=row.id,
        name=row.name,
        date=row.date,
        country=row.country,
        scheduling_effect=row.scheduling_effect,
        reduced_starts_local=row.reduced_starts_local,
        reduced_ends_local=row.reduced_ends_local,
        payroll_multiplier=row.payroll_multiplier,
    )


def _occurrence_ref_to_view(row: OccurrenceRefRow) -> TaskRefView:
    """Project a seam-level :class:`OccurrenceRefRow` into :class:`TaskRefView`.

    The seam already resolved the
    :attr:`OccurrenceRefRow.scheduled_for_local` fallback (UTC
    ``starts_at`` when the property-local column is null), so this
    re-pack is field-for-field.
    """
    return TaskRefView(id=row.id, scheduled_for_local=row.scheduled_for_local)


def _resolve_window(
    *,
    from_date: date | None,
    to_date: date | None,
    clock: Clock,
) -> tuple[date, date]:
    """Resolve the schedule window, applying §12 defaults for unset edges.

    Both edges default independently — a caller that sends only
    ``to=`` slices "today through to" without re-stating the start.
    The default window is :data:`DEFAULT_WINDOW_DAYS` days **inclusive**:
    ``today + 14d`` matches the §12 wording verbatim. ``ends_on``
    semantics are inclusive throughout the §06 surface, so the window
    treats both edges as inclusive too.
    """
    today = clock.now().date()
    resolved_from = from_date if from_date is not None else today
    resolved_to = (
        to_date if to_date is not None else today + timedelta(days=DEFAULT_WINDOW_DAYS)
    )
    return resolved_from, resolved_to


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def aggregate_schedule(
    repo: MeScheduleQueryRepository,
    ctx: WorkspaceContext,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    clock: Clock | None = None,
) -> SchedulePayload:
    """Return the caller's :class:`SchedulePayload` for the requested window.

    See the module docstring for the full contract. The aggregator is
    deliberately small: each repo call runs a single table scan keyed
    on ``(workspace_id, user_id)`` (the indexes the cd-l2r9 migration
    installed) so the whole feed lands in one transaction without
    N+1 surprises.

    A backwards window (``to_date < from_date``) returns an empty
    feed in every list except ``rota`` (the weekly pattern is always
    seven rows max, independent of the calendar window). The caller
    is expected to validate the window at the wire layer; the
    aggregator stays permissive so a malformed request collapses
    cleanly rather than raising mid-aggregate.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_from, resolved_to = _resolve_window(
        from_date=from_date,
        to_date=to_date,
        clock=resolved_clock,
    )

    user_id = ctx.actor_id
    workspace_id = ctx.workspace_id

    # --- Rota (weekly pattern) ------------------------------------------
    weekly_rows = repo.list_weekly_pattern(
        workspace_id=workspace_id,
        user_id=user_id,
    )
    rota = [
        WeeklySlotView(
            weekday=row.weekday,
            starts_local=row.starts_local,
            ends_local=row.ends_local,
        )
        for row in weekly_rows
    ]

    # --- Assigned tasks --------------------------------------------------
    # Bound the window in UTC using the start of ``from_date`` and the
    # end of ``to_date`` so a task scheduled at 23:30 on the last day of
    # the window still matches.
    window_start_utc = datetime.combine(resolved_from, time.min, tzinfo=UTC)
    window_end_utc = datetime.combine(resolved_to, time.max, tzinfo=UTC)
    occurrence_rows = repo.list_assigned_occurrences_in_window(
        workspace_id=workspace_id,
        user_id=user_id,
        window_start_utc=window_start_utc,
        window_end_utc=window_end_utc,
    )
    tasks = [_occurrence_ref_to_view(row) for row in occurrence_rows]

    # --- Leaves (approved + pending) ------------------------------------
    leave_rows = repo.list_leaves_in_window(
        workspace_id=workspace_id,
        user_id=user_id,
        from_date=resolved_from,
        to_date=resolved_to,
    )
    approved_leaves: list[UserLeaveView] = []
    pending_leaves: list[UserLeaveView] = []
    for leave_row in leave_rows:
        leave_view = _leave_row_to_view(leave_row)
        if leave_view.approved_at is not None:
            approved_leaves.append(leave_view)
        else:
            pending_leaves.append(leave_view)

    # --- Overrides (approved + pending) ---------------------------------
    override_rows = repo.list_overrides_in_window(
        workspace_id=workspace_id,
        user_id=user_id,
        from_date=resolved_from,
        to_date=resolved_to,
    )
    approved_overrides: list[UserAvailabilityOverrideView] = []
    pending_overrides: list[UserAvailabilityOverrideView] = []
    for override_row in override_rows:
        override_view = _override_row_to_view(override_row)
        # Approved iff ``approved_at IS NOT NULL`` — covers both
        # auto-approved (``approval_required=False``) and
        # manager-approved (``approval_required=True``,
        # ``approved_at=now`` after an approve transition).
        # Pending requires both ``approval_required=True`` AND
        # ``approved_at IS NULL`` per spec §12 wording: making the
        # ``approval_required`` half explicit guards against an
        # unreachable-but-defensive state (``approval_required=False``
        # without ``approved_at``) leaking into either bucket.
        if override_view.approved_at is not None:
            approved_overrides.append(override_view)
        elif override_view.approval_required:
            pending_overrides.append(override_view)

    # --- Holidays --------------------------------------------------------
    # No country narrowing in v1 — see module docstring.
    holiday_rows = repo.list_holidays_in_window(
        workspace_id=workspace_id,
        from_date=resolved_from,
        to_date=resolved_to,
    )
    holidays = [_holiday_row_to_view(row) for row in holiday_rows]

    return SchedulePayload(
        from_date=resolved_from,
        to_date=resolved_to,
        rota=rota,
        tasks=tasks,
        leaves=approved_leaves,
        overrides=approved_overrides,
        holidays=holidays,
        pending=PendingItems(
            leaves=pending_leaves,
            overrides=pending_overrides,
        ),
    )
