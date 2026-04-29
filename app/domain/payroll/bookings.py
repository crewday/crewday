"""Booking-derived payroll entries (§09 "Bookings" / "Pay period")."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.domain.payroll.ports import BookingPayRow

__all__ = [
    "BookingPayEntry",
    "BookingPayInvariantViolated",
    "derive_booking_pay_entry",
    "group_booking_entries_by_day",
]

_SETTLED_STATUSES = {
    "completed",
    "adjusted",
    "cancelled_by_client",
    "cancelled_by_agency",
    "no_show_worker",
}


class BookingPayInvariantViolated(ValueError):
    """A booking row cannot produce a deterministic payroll entry."""


@dataclass(frozen=True, slots=True)
class BookingPayEntry:
    """Computed pay contribution for one settled booking."""

    booking_id: str
    workspace_id: str
    work_engagement_id: str
    user_id: str
    property_id: str | None
    entry_date: date
    status: str
    kind: str
    pay_basis: Literal["scheduled", "actual"]
    minutes: int
    scheduled_minutes: int
    actual_minutes: int | None
    actual_minutes_paid: int
    adjusted: bool
    unsettled: bool

    def source_detail(self) -> dict[str, object]:
        return {
            "booking_id": self.booking_id,
            "property_id": self.property_id,
            "status": self.status,
            "kind": self.kind,
            "pay_basis": self.pay_basis,
            "minutes": self.minutes,
            "scheduled_minutes": self.scheduled_minutes,
            "actual_minutes": self.actual_minutes,
            "actual_minutes_paid": self.actual_minutes_paid,
            "adjusted": self.adjusted,
        }


def _scheduled_minutes(row: BookingPayRow) -> int:
    scheduled_seconds = int((row.scheduled_end - row.scheduled_start).total_seconds())
    if scheduled_seconds <= 0:
        raise BookingPayInvariantViolated("scheduled_end must be after scheduled_start")
    if row.break_seconds < 0:
        raise BookingPayInvariantViolated("break_seconds must be non-negative")
    return max(0, (scheduled_seconds - row.break_seconds) // 60)


def _lead_hours(row: BookingPayRow) -> float:
    if row.cancelled_at is None:
        raise BookingPayInvariantViolated("cancelled bookings require cancelled_at")
    return (row.scheduled_start - row.cancelled_at).total_seconds() / 3600


def _completed_minutes(row: BookingPayRow, scheduled_minutes: int) -> int:
    if row.actual_minutes_paid < 0:
        raise BookingPayInvariantViolated("actual_minutes_paid must be non-negative")
    if row.pay_basis == "actual":
        return row.actual_minutes_paid
    if row.adjusted or row.actual_minutes is not None:
        return row.actual_minutes_paid
    return scheduled_minutes


def derive_booking_pay_entry(row: BookingPayRow) -> BookingPayEntry:
    """Derive payroll minutes for one booking without consulting shifts."""

    scheduled_minutes = _scheduled_minutes(row)
    minutes = 0

    if row.status not in _SETTLED_STATUSES:
        return BookingPayEntry(
            booking_id=row.id,
            workspace_id=row.workspace_id,
            work_engagement_id=row.work_engagement_id,
            user_id=row.user_id,
            property_id=row.property_id,
            entry_date=row.scheduled_start.date(),
            status=row.status,
            kind=row.kind,
            pay_basis=row.pay_basis,
            minutes=0,
            scheduled_minutes=scheduled_minutes,
            actual_minutes=row.actual_minutes,
            actual_minutes_paid=row.actual_minutes_paid,
            adjusted=row.adjusted,
            unsettled=True,
        )

    if row.status in {"completed", "adjusted"}:
        minutes = _completed_minutes(row, scheduled_minutes)
    elif row.status == "cancelled_by_client":
        inside_window = _lead_hours(row) < row.cancellation_window_hours
        if inside_window and row.cancellation_pay_to_worker:
            minutes = scheduled_minutes
    elif row.status == "cancelled_by_agency":
        if _lead_hours(row) < row.cancellation_window_hours:
            minutes = scheduled_minutes
    elif row.status == "no_show_worker":
        minutes = 0

    return BookingPayEntry(
        booking_id=row.id,
        workspace_id=row.workspace_id,
        work_engagement_id=row.work_engagement_id,
        user_id=row.user_id,
        property_id=row.property_id,
        entry_date=row.scheduled_start.date(),
        status=row.status,
        kind=row.kind,
        pay_basis=row.pay_basis,
        minutes=minutes,
        scheduled_minutes=scheduled_minutes,
        actual_minutes=row.actual_minutes,
        actual_minutes_paid=row.actual_minutes_paid,
        adjusted=row.adjusted,
        unsettled=row.pending_amend_minutes is not None,
    )


def group_booking_entries_by_day(
    entries: Iterable[BookingPayEntry],
) -> dict[tuple[str, str, date], list[BookingPayEntry]]:
    grouped: dict[tuple[str, str, date], list[BookingPayEntry]] = {}
    for entry in entries:
        key = (entry.work_engagement_id, entry.user_id, entry.entry_date)
        grouped.setdefault(key, []).append(entry)
    return grouped
