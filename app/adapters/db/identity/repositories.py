"""SA-backed repositories implementing :mod:`app.domain.identity.me_schedule_ports`.

The concrete class here adapts SQLAlchemy ``Session`` work to the
read-only Protocol surface the §12 worker calendar feed consumes
(cd-lot5):

* :class:`SqlAlchemyMeScheduleQueryRepository` — wraps the five
  SELECTs the schedule aggregator runs. Reads from
  :mod:`app.adapters.db.availability.models` (rota / overrides /
  leaves), :mod:`app.adapters.db.holidays.models` (workspace
  holidays), and :mod:`app.adapters.db.tasks.models` (assigned
  occurrence refs). Consumed by
  :mod:`app.domain.identity.me_schedule`.

Reaches into three adapter packages directly. Adapter-to-adapter
imports are allowed by the import-linter — only ``app.domain →
app.adapters`` is forbidden. We re-use the row-projection helpers
(``_to_weekly_row`` / ``_to_override_row`` / ``_to_leave_row``)
from :mod:`app.adapters.db.availability.repositories` rather than
duplicating the field-by-field copies; both adapters convert the
same ORM types into the same seam-level rows, so a single source
of truth keeps them aligned when columns land on the underlying
tables.

The repo carries an open ``Session`` and never commits — the schedule
feed is pure read aggregation. The caller's UoW owns the transaction
boundary (§01 "Key runtime invariants" #3); five independent SELECTs
land inside one transaction so the feed sees a consistent snapshot
even if a sibling writer is active.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date as _date_cls
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.availability.models import (
    UserAvailabilityOverride,
    UserLeave,
    UserWeeklyAvailability,
)

# Re-use the cd-r5j2 / cd-2upg row-projection helpers from the
# availability adapter rather than copy-pasting the field-by-field
# converters. Both files own the conversion of the same ORM types
# into the same seam-level rows declared on
# :mod:`app.domain.identity.availability_ports`; duplicating them
# would invite drift the moment a column lands. Adapter-to-adapter
# imports are allowed by the import-linter (only ``app.domain →
# app.adapters`` is forbidden), and the underscore-prefixed
# crossing mirrors the same trade-off
# :mod:`app.domain.identity.me_schedule` accepts when re-using the
# sibling services' ``_row_to_view`` projections.
from app.adapters.db.availability.repositories import (
    _to_leave_row,
    _to_override_row,
    _to_weekly_row,
)
from app.adapters.db.holidays.models import PublicHoliday
from app.adapters.db.tasks.models import Occurrence
from app.domain.identity.availability_ports import (
    UserAvailabilityOverrideRow,
    UserLeaveRow,
    UserWeeklyAvailabilityRow,
)
from app.domain.identity.me_schedule_ports import (
    MeScheduleQueryRepository,
    OccurrenceRefRow,
    PublicHolidayRow,
)

__all__ = ["SqlAlchemyMeScheduleQueryRepository"]


# ---------------------------------------------------------------------------
# Row projections
# ---------------------------------------------------------------------------


def _to_holiday_row(row: PublicHoliday) -> PublicHolidayRow:
    """Project an ORM ``PublicHoliday`` into the seam-level row."""
    return PublicHolidayRow(
        id=row.id,
        name=row.name,
        date=row.date,
        country=row.country,
        scheduling_effect=row.scheduling_effect,
        reduced_starts_local=row.reduced_starts_local,
        reduced_ends_local=row.reduced_ends_local,
        payroll_multiplier=row.payroll_multiplier,
    )


def _to_occurrence_ref_row(row: Occurrence) -> OccurrenceRefRow:
    """Project an ORM ``Occurrence`` into the lightweight calendar ref.

    Falls back to ``starts_at.isoformat()`` when ``scheduled_for_local``
    is null. The cd-22e generator always populates the local column,
    so this fallback only fires for legacy rows (or hand-seeded test
    fixtures that skip the column); keeping it deterministic in the
    adapter avoids a JSON ``null`` leaking into a UI that expects a
    renderable timestamp.
    """
    if row.scheduled_for_local is not None:
        local = row.scheduled_for_local
    else:
        local = row.starts_at.isoformat()
    return OccurrenceRefRow(id=row.id, scheduled_for_local=local)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class SqlAlchemyMeScheduleQueryRepository(MeScheduleQueryRepository):
    """SA-backed concretion of :class:`MeScheduleQueryRepository`.

    Wraps an open :class:`~sqlalchemy.orm.Session` and never writes —
    the schedule feed is pure aggregation. Reads run inside the
    caller's UoW so the five SELECTs see a consistent snapshot.

    Defence-in-depth pins every read to the caller's ``workspace_id``
    even though the ORM tenant filter already narrows them; a
    misconfigured filter must fail loud, not silently.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_weekly_pattern(
        self,
        *,
        workspace_id: str,
        user_id: str,
    ) -> Sequence[UserWeeklyAvailabilityRow]:
        rows = self._session.scalars(
            select(UserWeeklyAvailability)
            .where(
                UserWeeklyAvailability.workspace_id == workspace_id,
                UserWeeklyAvailability.user_id == user_id,
            )
            .order_by(UserWeeklyAvailability.weekday.asc())
        ).all()
        return [_to_weekly_row(r) for r in rows]

    def list_overrides_in_window(
        self,
        *,
        workspace_id: str,
        user_id: str,
        from_date: _date_cls,
        to_date: _date_cls,
    ) -> Sequence[UserAvailabilityOverrideRow]:
        rows = self._session.scalars(
            select(UserAvailabilityOverride)
            .where(
                UserAvailabilityOverride.workspace_id == workspace_id,
                UserAvailabilityOverride.user_id == user_id,
                UserAvailabilityOverride.deleted_at.is_(None),
                UserAvailabilityOverride.date >= from_date,
                UserAvailabilityOverride.date <= to_date,
            )
            .order_by(UserAvailabilityOverride.date.asc())
        ).all()
        return [_to_override_row(r) for r in rows]

    def list_leaves_in_window(
        self,
        *,
        workspace_id: str,
        user_id: str,
        from_date: _date_cls,
        to_date: _date_cls,
    ) -> Sequence[UserLeaveRow]:
        # Standard interval-overlap predicate — see the
        # :class:`MeScheduleQueryRepository` Protocol docstring for
        # the §06 wording. A leave covers the window iff
        # ``starts_on <= to_date AND ends_on >= from_date``.
        rows = self._session.scalars(
            select(UserLeave)
            .where(
                UserLeave.workspace_id == workspace_id,
                UserLeave.user_id == user_id,
                UserLeave.deleted_at.is_(None),
                UserLeave.starts_on <= to_date,
                UserLeave.ends_on >= from_date,
            )
            .order_by(UserLeave.starts_on.asc())
        ).all()
        return [_to_leave_row(r) for r in rows]

    def list_holidays_in_window(
        self,
        *,
        workspace_id: str,
        from_date: _date_cls,
        to_date: _date_cls,
    ) -> Sequence[PublicHolidayRow]:
        rows = self._session.scalars(
            select(PublicHoliday)
            .where(
                PublicHoliday.workspace_id == workspace_id,
                PublicHoliday.deleted_at.is_(None),
                PublicHoliday.date >= from_date,
                PublicHoliday.date <= to_date,
            )
            .order_by(PublicHoliday.date.asc())
        ).all()
        return [_to_holiday_row(r) for r in rows]

    def list_assigned_occurrences_in_window(
        self,
        *,
        workspace_id: str,
        user_id: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> Sequence[OccurrenceRefRow]:
        # Walks the ``ix_occurrence_workspace_assignee_starts``
        # composite index — leading ``workspace_id`` carries the tenant
        # filter, and ``starts_at`` ranges inside the index.
        rows = self._session.scalars(
            select(Occurrence)
            .where(
                Occurrence.workspace_id == workspace_id,
                Occurrence.assignee_user_id == user_id,
                Occurrence.starts_at >= window_start_utc,
                Occurrence.starts_at <= window_end_utc,
            )
            .order_by(Occurrence.starts_at.asc())
        ).all()
        return [_to_occurrence_ref_row(r) for r in rows]
