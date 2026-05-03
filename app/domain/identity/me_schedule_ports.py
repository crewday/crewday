"""Identity context — read-only repository seam for the schedule aggregator (cd-lot5).

Defines the seam :mod:`app.domain.identity.me_schedule` uses to pull
the four row shapes the worker calendar feed needs (rota, leaves,
overrides, bookings, plus the existing override / leave projections
from :mod:`app.domain.identity.availability_ports`) — without
importing SQLAlchemy model classes from
``app.adapters.db.{availability,payroll}.models``.

Spec: ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 —
each context defines its own repository port in its public surface
(``app/domain/<context>/ports.py`` — split into a sibling
``me_schedule_ports.py`` here so the existing
:mod:`app.domain.identity.availability_ports` stays focused on the
override / leave CRUD seams it already declares, and the
``me_schedule`` reads can grow independently of the override / leave
state machines).

One seam lives here:

* :class:`MeScheduleQueryRepository` — read-only aggregator that walks
  the §06 weekly-pattern + leave + override + §09 booking stack for a
  single ``(workspace_id, user_id)`` pair. Returns immutable row
  projections so the domain never sees an ORM row.

**Why one aggregated repo (not four).** The me_schedule service runs
exactly one aggregation per request and reads from two adapter
packages. Carving separate Protocols would either duplicate the
existing :class:`UserAvailabilityOverrideRepository` /
:class:`UserLeaveRepository` ``list()`` shapes (which expose
date-bound and status filters those services need but the schedule
aggregator does not), or force the router to compose multiple DI args
into a single call site. One narrow aggregator keeps the §12 "Self-
service shortcuts" feed's wiring to a single repo argument and pins
the read shape — the leave overlap predicate (``starts_on <= window_end
AND ends_on >= window_start``) does not match
:meth:`UserLeaveRepository.list`'s ``starts_after`` / ``ends_before``
inclusion filter, so reusing it would force-fit semantics. The four
methods here mirror the four SELECTs the aggregator runs verbatim.

The row projections reuse three existing seam shapes from
:mod:`app.domain.identity.availability_ports`:
:class:`UserAvailabilityOverrideRow`, :class:`UserLeaveRow`,
:class:`UserWeeklyAvailabilityRow`. One new shape
(:class:`BookingRefRow`) covers the booking read; it stays narrow
(only the columns the calendar feed renders) so the seam can grow
new readers without re-shaping the value object.

The §14 worker calendar's rota / slot / assignment / property / task
synthesis is **not** reads through this seam — both
``/me/schedule`` and ``/scheduler/calendar`` share the synthesiser
in :mod:`app.api.v1._scheduler_resolver` until the
``schedule_ruleset`` table lands (§06 "Schedule ruleset"). Holidays
are tracked separately in
:mod:`app.adapters.db.holidays.models` and surface through the
manager-side ``/public_holidays`` API; a follow-up reads them
through this seam once the worker calendar grows §14 "Public
holidays and property closures" markers.

Protocols are deliberately **not** ``runtime_checkable``: structural
compatibility is checked statically by mypy. Runtime ``isinstance``
against this protocol would mask typos and invite duck-typing
shortcuts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from app.domain.identity.availability_ports import (
    UserAvailabilityOverrideRow,
    UserLeaveRow,
    UserWeeklyAvailabilityRow,
)

__all__ = [
    "BookingRefRow",
    "MeScheduleQueryRepository",
]


# ---------------------------------------------------------------------------
# Row shapes (value objects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BookingRefRow:
    """Immutable projection of a ``booking`` row covering the worker's window.

    Mirrors the public §09 booking shape the
    ``/me/schedule`` page renders inline. ``user_id`` is the resolved
    booking ``user_id`` (the worker the booking pays); the seam keeps
    ``work_engagement_id`` so the wire layer can populate the
    frontend ``Booking.employee_id`` slot consistently for now (one
    user = one engagement per workspace, per the §02 partial UNIQUE).

    Tombstoned rows (``deleted_at IS NOT NULL``) are filtered at the
    repo layer so the worker calendar never renders a withdrawn
    booking.
    """

    id: str
    workspace_id: str
    user_id: str
    work_engagement_id: str
    property_id: str | None
    client_org_id: str | None
    status: str
    kind: str
    scheduled_start: datetime
    scheduled_end: datetime
    actual_minutes: int | None
    actual_minutes_paid: int
    break_seconds: int
    pending_amend_minutes: int | None
    pending_amend_reason: str | None
    declined_at: datetime | None
    declined_reason: str | None
    notes_md: str | None
    adjusted: bool
    adjustment_reason: str | None


# ---------------------------------------------------------------------------
# MeScheduleQueryRepository
# ---------------------------------------------------------------------------


class MeScheduleQueryRepository(Protocol):
    """Read-only seam for the §12 worker calendar aggregator.

    Four narrow SELECTs keyed on ``(workspace_id, user_id)`` (the
    weekly pattern + per-user leaves / overrides / bookings). Every
    read honours the workspace-scoping invariant: the SA concretion
    always pins reads to the ``workspace_id`` passed by the caller,
    mirroring the ORM tenant filter as defence-in-depth (a
    misconfigured filter must fail loud).

    The repo never writes — the schedule feed is pure aggregation —
    so there is no ``session`` accessor and no ``flush`` contract. The
    SA concretion holds an open ``Session`` purely to issue the
    SELECTs; the caller's UoW owns the transaction boundary.

    Tombstone filtering: every read that targets a soft-delete-aware
    table (``user_leave``, ``user_availability_override``,
    ``booking``) excludes rows with ``deleted_at IS NOT NULL``.
    """

    def list_weekly_pattern(
        self,
        *,
        workspace_id: str,
        user_id: str,
    ) -> Sequence[UserWeeklyAvailabilityRow]:
        """Return the user's standing weekly pattern (0..7 rows).

        Ordered by ``weekday ASC`` so the caller can render Mon..Sun
        without re-sorting. A user with no rows returns an empty
        sequence (the §06 calculator treats every weekday as "off"
        in that case).
        """
        ...

    def list_overrides_in_window(
        self,
        *,
        workspace_id: str,
        user_id: str,
        from_date: date,
        to_date: date,
    ) -> Sequence[UserAvailabilityOverrideRow]:
        """Return live override rows whose ``date`` falls in ``[from_date, to_date]``.

        Inclusive bounds on both edges, matching the §12 window
        semantics. Ordered by ``date ASC``. Tombstoned rows
        (``deleted_at IS NOT NULL``) are excluded. The caller carries
        approved + pending rows merged; each row exposes its own
        ``approval_required`` / ``approved_at`` so the SPA can branch.
        """
        ...

    def list_leaves_in_window(
        self,
        *,
        workspace_id: str,
        user_id: str,
        from_date: date,
        to_date: date,
    ) -> Sequence[UserLeaveRow]:
        """Return live leave rows overlapping the window.

        Overlap predicate: a leave covers the window iff
        ``starts_on <= to_date AND ends_on >= from_date`` — standard
        interval overlap, matches §06 "user_leave" semantics. Ordered
        by ``starts_on ASC``. Tombstoned rows (``deleted_at IS NOT
        NULL``) are excluded. Approved + pending rows are returned
        together; each row exposes its own ``approved_at``.
        """
        ...

    def list_bookings_in_window(
        self,
        *,
        workspace_id: str,
        user_id: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> Sequence[BookingRefRow]:
        """Return live bookings whose ``scheduled_start`` falls in the UTC window.

        Inclusive on both edges. Ordered by ``scheduled_start ASC``,
        then ``id ASC`` so the wire payload is deterministic.
        Tombstoned rows (``deleted_at IS NOT NULL``) are excluded.
        Every status is returned — the worker calendar surfaces
        ``pending_approval`` + ``cancelled_*`` rows alongside the
        live ``scheduled`` ones (§14 "Schedule view").
        """
        ...
