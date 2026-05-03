"""Self-service schedule aggregator (cd-6uij).

Read-only helper that powers ``GET /me/schedule`` (§12 "Self-service
shortcuts" / §14 "Schedule view"). Walks the §06 availability stack +
the §09 booking ledger for the caller and projects every row covering
a date window into the wire shape consumed by the worker
``/schedule`` page.

This module owns the **identity-side seam reads** (weekly pattern,
leaves, overrides, bookings); the rota / assignment / property / task
synthesis lives in :mod:`app.api.v1._scheduler_resolver` because both
``/me/schedule`` (worker self-view) and ``/scheduler/calendar``
(manager view) project from the same backing rows. The router
composes the two sources into a single wire payload.

Public surface:

* **DTOs** — :class:`WeeklySlotView`, :class:`SchedulePayload`.
* **Aggregator** — :func:`aggregate_schedule`. Takes a
  :class:`MeScheduleQueryRepository` plus a :class:`WorkspaceContext`
  plus optional ``from_date`` / ``to_date`` (defaults to the §12
  ``[today, today+14d]`` window) and returns a
  :class:`SchedulePayload`.

**Self-only by construction.** Every read predicate is keyed on
``ctx.actor_id`` — a worker cannot use this surface to leak another
user's data through the feed. The router does **not** expose a
``user_id`` query param: managers wanting cross-user visibility use
the per-resource generic endpoints (`/user_leaves`,
`/user_availability_overrides`, `/tasks?assignee_user_id=…`).

**Approved + pending merged.** The §14 worker calendar surfaces both
states inline — each row carries its own ``approved_at`` /
``approval_required`` so the SPA renders the pending banner without
needing a parallel bucket. The earlier ``pending`` envelope from the
cd-6uij wire shape was dropped when the frontend port (§14 "Schedule
view") and the API converged on the rich §12 calendar wire.

**No audit.** Read-only — the aggregator never writes a row. The
:class:`WorkspaceContext` carries the actor id we filter on; no
``write_audit`` call lands.

**Tenancy.** The ORM tenant filter auto-narrows every SELECT issued
by the SA-backed
:class:`~app.adapters.db.identity.repositories.SqlAlchemyMeScheduleQueryRepository`;
the repo also re-asserts the ``workspace_id = ctx.workspace_id``
predicate explicitly as defence-in-depth.

**Holidays.** v1 does not surface holidays through the worker
calendar — the §14 UI has no public-holiday markers yet. The
``public_holiday`` table still exists and is queryable through the
manager-side ``/public_holidays`` surface; a follow-up adds a
``holidays[]`` field once the SPA grows the §14 "Public holidays
and property closures" markers.

**Architecture (cd-lot5).** The module talks to a
:class:`~app.domain.identity.me_schedule_ports.MeScheduleQueryRepository`
Protocol — never to the SQLAlchemy model classes. The SA-backed
concretion lives in
:mod:`app.adapters.db.identity.repositories`; unit tests inject
fakes or wire the SA repo over an in-memory SQLite session.

See ``docs/specs/12-rest-api.md`` §"Self-service shortcuts";
``docs/specs/06-tasks-and-scheduling.md`` §"user_leave",
§"user_availability_overrides", §"Weekly availability";
``docs/specs/14-web-frontend.md`` §"Schedule view".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.identity.me_schedule_ports import (
    BookingRefRow,
    MeScheduleQueryRepository,
)
from app.domain.identity.user_availability_overrides import (
    UserAvailabilityOverrideView,
)

# Reuse the sibling services' seam-Row → View projections rather than
# duplicating them here.
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
    "SchedulePayload",
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
class SchedulePayload:
    """Identity-side slice of the ``/me/schedule`` wire payload.

    Covers everything the :mod:`app.domain.identity` seam owns —
    weekly pattern, leaves, overrides, bookings — plus the resolved
    window + caller id. The rota / slot / assignment / task /
    property bag is stitched on top by the router using
    :mod:`app.api.v1._scheduler_resolver`.

    ``leaves`` / ``overrides`` are the **merged** approved + pending
    lists per §14 "Schedule view": each row carries its own
    ``approved_at`` / ``approval_required`` so the SPA can render
    the pending banner without a parallel bucket.
    """

    from_date: date
    to_date: date
    user_id: str
    weekly_availability: list[WeeklySlotView]
    leaves: list[UserLeaveView]
    overrides: list[UserAvailabilityOverrideView]
    bookings: list[BookingRefRow]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _resolve_zone(tz_name: str | None) -> tzinfo:
    """Resolve an IANA timezone name, falling back to UTC.

    A booking with no ``property_id`` (and hence no entry in
    ``property_timezones``) or an unrecognised IANA string falls
    back to UTC — the same fallback :func:`local_date_for_task`
    in :mod:`app.api.v1._scheduler_resolver` uses, so the worker
    calendar's task and booking branches stay symmetric.
    """
    if not tz_name:
        return UTC
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return UTC


def _booking_local_date(
    row: BookingRefRow,
    *,
    property_timezones: Mapping[str, str],
) -> date:
    """Return the booking's ``scheduled_start`` projected into property-local date.

    A booking with no ``property_id``, with a ``property_id`` missing
    from ``property_timezones`` (archived or cross-workspace property),
    or whose property carries an unrecognised IANA string falls back
    to UTC. Tombstoned bookings (``deleted_at IS NOT NULL``) never
    reach this helper — the repo filters them at SELECT time.
    Mirrors :func:`app.api.v1._scheduler_resolver.local_date_for_task`
    so the §14 worker calendar lines up tasks and bookings against
    the same property-local grid.
    """
    starts_at = row.scheduled_start
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=UTC)
    tz_name = property_timezones.get(row.property_id) if row.property_id else None
    return starts_at.astimezone(_resolve_zone(tz_name)).date()


def aggregate_schedule(
    repo: MeScheduleQueryRepository,
    ctx: WorkspaceContext,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    clock: Clock | None = None,
    property_timezones: Mapping[str, str] | None = None,
) -> SchedulePayload:
    """Return the caller's :class:`SchedulePayload` for the requested window.

    See the module docstring for the full contract. A backwards
    window (``to_date < from_date``) returns an empty feed — the
    router validates the window at the wire layer; the aggregator
    stays permissive so a malformed request collapses cleanly.

    ``property_timezones`` maps each visible ``property_id`` to its
    IANA timezone string. Used to decide which bookings fall inside
    ``[from_date, to_date]`` **in property-local time** — a booking at
    local 00:30 on the first window day in ``Pacific/Auckland``
    (UTC+13) lands 11:30 the previous UTC day; without this map the
    naïve UTC bound would silently drop the row. Bookings without a
    ``property_id`` (or with an unresolvable timezone) fall back to
    UTC, matching :func:`app.api.v1._scheduler_resolver.local_date_for_task`.
    Defaults to an empty mapping so direct domain-level callers (the
    unit suite, future scriptable consumers) still get the
    UTC-only behaviour.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_from, resolved_to = _resolve_window(
        from_date=from_date,
        to_date=to_date,
        clock=resolved_clock,
    )
    tz_map: Mapping[str, str] = (
        property_timezones if property_timezones is not None else {}
    )

    user_id = ctx.actor_id
    workspace_id = ctx.workspace_id

    # --- Weekly pattern --------------------------------------------------
    weekly_rows = repo.list_weekly_pattern(
        workspace_id=workspace_id,
        user_id=user_id,
    )
    weekly_availability = [
        WeeklySlotView(
            weekday=row.weekday,
            starts_local=row.starts_local,
            ends_local=row.ends_local,
        )
        for row in weekly_rows
    ]

    # --- Leaves (approved + pending merged) -----------------------------
    leave_rows = repo.list_leaves_in_window(
        workspace_id=workspace_id,
        user_id=user_id,
        from_date=resolved_from,
        to_date=resolved_to,
    )
    leaves = [_leave_row_to_view(row) for row in leave_rows]

    # --- Overrides (approved + pending merged) --------------------------
    override_rows = repo.list_overrides_in_window(
        workspace_id=workspace_id,
        user_id=user_id,
        from_date=resolved_from,
        to_date=resolved_to,
    )
    overrides = [_override_row_to_view(row) for row in override_rows]

    # --- Bookings --------------------------------------------------------
    # The §14 worker calendar treats the ``[from, to]`` window as
    # **property-local** — a booking belongs to the window iff its
    # ``scheduled_start`` projected into the property's IANA timezone
    # falls within ``[from, to]``. We over-fetch by one UTC day on
    # each side (covers any IANA offset, including the ±14h corners)
    # then post-filter the rows whose property-local date falls
    # inside the requested window. This mirrors the task path in
    # :func:`app.api.v1._scheduler_resolver.task_rows_for_window`,
    # so the worker calendar's task and booking branches use the same
    # property-local grid.
    window_start_utc = datetime.combine(
        resolved_from - timedelta(days=1), time.min, tzinfo=UTC
    )
    window_end_utc = datetime.combine(
        resolved_to + timedelta(days=1), time.max, tzinfo=UTC
    )
    booking_rows = repo.list_bookings_in_window(
        workspace_id=workspace_id,
        user_id=user_id,
        window_start_utc=window_start_utc,
        window_end_utc=window_end_utc,
    )
    bookings_in_window = [
        row
        for row in booking_rows
        if resolved_from
        <= _booking_local_date(row, property_timezones=tz_map)
        <= resolved_to
    ]

    return SchedulePayload(
        from_date=resolved_from,
        to_date=resolved_to,
        user_id=user_id,
        weekly_availability=weekly_availability,
        leaves=leaves,
        overrides=overrides,
        bookings=bookings_in_window,
    )
