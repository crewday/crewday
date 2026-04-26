"""Identity context — read-only repository seam for the schedule aggregator (cd-lot5).

Defines the seam :mod:`app.domain.identity.me_schedule` uses to pull
the five row shapes the worker calendar feed needs (rota, holidays,
assigned occurrences, plus the existing override / leave projections
from :mod:`app.domain.identity.availability_ports`) — without
importing SQLAlchemy model classes from ``app.adapters.db.{availability,
holidays,tasks}.models``.

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
  the §06 weekly-pattern + leave + override + holiday + assigned-
  occurrence stack for a single ``(workspace_id, user_id)`` pair.
  Returns immutable row projections so the domain never sees an ORM
  row.

**Why one aggregated repo (not four).** The me_schedule service runs
exactly one aggregation per request and reads from three different
adapter packages. Carving four separate Protocols would either:
duplicate the existing :class:`UserAvailabilityOverrideRepository` /
:class:`UserLeaveRepository` ``list()`` shapes (which expose
date-bound and status filters those services need but the schedule
aggregator does not), or force the router to compose four DI args
into a single call site. One narrow aggregator keeps the §12 "Self-
service shortcuts" feed's wiring to a single repo argument and pins
the read shape — the leave overlap predicate (``starts_on <= window_end
AND ends_on >= window_start``) does not match
:meth:`UserLeaveRepository.list`'s ``starts_after`` / ``ends_before``
inclusion filter, so reusing it would force-fit semantics. The five
methods here mirror the five SELECTs the aggregator runs verbatim.

The row projections reuse three existing seam shapes from
:mod:`app.domain.identity.availability_ports`:
:class:`UserAvailabilityOverrideRow`, :class:`UserLeaveRow`,
:class:`UserWeeklyAvailabilityRow`. Two new shapes
(:class:`PublicHolidayRow`, :class:`OccurrenceRefRow`) cover the
holidays / occurrences read; both stay narrow (only the columns the
calendar feed renders) so the seam can grow new readers without
re-shaping the value objects.

Protocols are deliberately **not** ``runtime_checkable``: structural
compatibility is checked statically by mypy. Runtime ``isinstance``
against this protocol would mask typos and invite duck-typing
shortcuts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as time_cls
from decimal import Decimal
from typing import Protocol

from app.domain.identity.availability_ports import (
    UserAvailabilityOverrideRow,
    UserLeaveRow,
    UserWeeklyAvailabilityRow,
)

__all__ = [
    "MeScheduleQueryRepository",
    "OccurrenceRefRow",
    "PublicHolidayRow",
]


# ---------------------------------------------------------------------------
# Row shapes (value objects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PublicHolidayRow:
    """Immutable projection of a ``public_holiday`` row covering the window.

    Mirrors :class:`~app.domain.identity.me_schedule.PublicHolidayView`
    minus the ORM-managed columns the calendar feed never renders
    (``recurrence``, ``notes_md``, audit timestamps). Declared on the
    seam so the SA adapter can project ORM rows into a domain-owned
    shape without importing the service module that consumes it.

    ``payroll_multiplier`` carries :class:`~decimal.Decimal` semantics
    cleanly across SQLite (TEXT) and Postgres (numeric); the wire
    layer above serialises it as a string at response time.
    """

    id: str
    name: str
    date: date
    country: str | None
    scheduling_effect: str
    reduced_starts_local: time_cls | None
    reduced_ends_local: time_cls | None
    payroll_multiplier: Decimal | None


@dataclass(frozen=True, slots=True)
class OccurrenceRefRow:
    """Immutable lightweight projection of an :class:`Occurrence` row.

    Carries only what the §12 "Self-service shortcuts" feed renders
    on the calendar — the full :class:`Occurrence` shape lives at
    ``/tasks/{id}``. ``scheduled_for_local`` is the property-local
    ISO-8601 string the scheduler worker stamped at generation time;
    the SA adapter falls back to ``starts_at.isoformat()`` when the
    column is null (legacy rows pre-cd-22e), so the domain never sees
    a missing local timestamp.
    """

    id: str
    scheduled_for_local: str


# ---------------------------------------------------------------------------
# MeScheduleQueryRepository
# ---------------------------------------------------------------------------


class MeScheduleQueryRepository(Protocol):
    """Read-only seam for the §12 worker calendar aggregator.

    Five narrow SELECTs keyed on ``(workspace_id, user_id)`` (the
    weekly pattern + per-user leaves / overrides / occurrences) plus
    one workspace-scoped SELECT for holidays. Every read honours the
    workspace-scoping invariant: the SA concretion always pins reads
    to the ``workspace_id`` passed by the caller, mirroring the ORM
    tenant filter as defence-in-depth (a misconfigured filter must
    fail loud).

    The repo never writes — the schedule feed is pure aggregation —
    so there is no ``session`` accessor and no ``flush`` contract. The
    SA concretion holds an open ``Session`` purely to issue the
    SELECTs; the caller's UoW owns the transaction boundary.

    Tombstone filtering: every read that targets a soft-delete-aware
    table (``user_leave``, ``user_availability_override``,
    ``public_holiday``) excludes rows with ``deleted_at IS NOT NULL``.
    Occurrences do not carry a tombstone column — the §06 lifecycle
    cancels via ``state='cancelled'`` instead — so the occurrence
    read returns every assignee row in the window regardless of
    state. The router's renderer is responsible for any state-based
    filtering.
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
        (``deleted_at IS NOT NULL``) are excluded. The caller
        partitions the returned rows into approved / pending buckets
        per the §06 "Approved vs pending" rule.
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
        NULL``) are excluded. The caller partitions the returned rows
        into approved / pending buckets.
        """
        ...

    def list_holidays_in_window(
        self,
        *,
        workspace_id: str,
        from_date: date,
        to_date: date,
    ) -> Sequence[PublicHolidayRow]:
        """Return live holiday rows whose ``date`` falls in the window.

        Inclusive bounds on both edges. Ordered by ``date ASC``.
        Tombstoned rows (``deleted_at IS NOT NULL``) are excluded.

        v1 returns every holiday in the workspace regardless of
        ``country`` — country narrowing per the user's primary
        property is deferred until the property-timezone surface
        catches up (see :mod:`app.domain.identity.me_schedule` module
        docstring).
        """
        ...

    def list_assigned_occurrences_in_window(
        self,
        *,
        workspace_id: str,
        user_id: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> Sequence[OccurrenceRefRow]:
        """Return occurrence refs assigned to ``user_id`` inside the UTC window.

        The caller resolves the property-local window dates into UTC
        bounds (``window_start_utc`` = ``from_date`` 00:00 UTC,
        ``window_end_utc`` = ``to_date`` 23:59:59.999999 UTC) so the
        SA concretion can walk the
        ``ix_occurrence_workspace_assignee_starts`` composite index
        without re-doing the timezone math. Inclusive on both edges.
        Ordered by ``starts_at ASC``.

        The SA concretion fills :attr:`OccurrenceRefRow.scheduled_for_local`
        from ``starts_at.isoformat()`` when the column is null
        (legacy rows pre-cd-22e), so the returned shape always carries
        a renderable timestamp.
        """
        ...
