"""Unit tests for booking-derived payroll entries (cd-n0t4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from app.domain.payroll.bookings import (
    BookingPayInvariantViolated,
    derive_booking_pay_entry,
)
from app.domain.payroll.ports import BookingPayRow

_START = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
_END = datetime(2026, 5, 4, 13, 0, tzinfo=UTC)


def _row(
    *,
    status: str = "completed",
    pay_basis: Literal["scheduled", "actual"] = "scheduled",
    actual_minutes: int | None = None,
    actual_minutes_paid: int = 240,
    break_seconds: int = 0,
    adjusted: bool = False,
    adjustment_reason: str | None = None,
    pending_amend_minutes: int | None = None,
    pending_amend_reason: str | None = None,
    cancelled_at: datetime | None = None,
) -> BookingPayRow:
    return BookingPayRow(
        id="01HWA00000000000000000BKG1",
        workspace_id="01HWA00000000000000000WS01",
        work_engagement_id="01HWA00000000000000000ENG1",
        user_id="01HWA00000000000000000USR1",
        property_id="01HWA00000000000000000PRP1",
        property_country="FR",
        status=status,
        kind="work",
        pay_basis=pay_basis,
        scheduled_start=_START,
        scheduled_end=_END,
        actual_minutes=actual_minutes,
        actual_minutes_paid=actual_minutes_paid,
        break_seconds=break_seconds,
        adjusted=adjusted,
        adjustment_reason=adjustment_reason,
        pending_amend_minutes=pending_amend_minutes,
        pending_amend_reason=pending_amend_reason,
        cancelled_at=cancelled_at,
        cancellation_window_hours=24,
        cancellation_pay_to_worker=True,
        created_at=_START - timedelta(days=1),
        updated_at=_START - timedelta(days=1),
    )


def test_scheduled_basis_uses_scheduled_minutes_minus_break() -> None:
    entry = derive_booking_pay_entry(
        _row(actual_minutes_paid=999, break_seconds=30 * 60)
    )

    assert entry.minutes == 210
    assert entry.scheduled_minutes == 210
    assert entry.pay_basis == "scheduled"


def test_actual_basis_uses_actual_minutes_paid() -> None:
    entry = derive_booking_pay_entry(
        _row(pay_basis="actual", actual_minutes=185, actual_minutes_paid=185)
    )

    assert entry.minutes == 185
    assert entry.actual_minutes == 185


def test_adjusted_booking_uses_approved_actual_paid_minutes() -> None:
    entry = derive_booking_pay_entry(
        _row(
            status="adjusted",
            adjusted=True,
            adjustment_reason="heavy checkout",
            actual_minutes=265,
            actual_minutes_paid=265,
        )
    )

    assert entry.minutes == 265
    assert entry.adjusted is True


@pytest.mark.parametrize(
    ("cancelled_at", "expected_minutes"),
    [
        (_START - timedelta(hours=2), 240),
        (_START - timedelta(hours=30), 0),
    ],
)
def test_cancelled_by_client_follows_default_worker_pay_rule(
    cancelled_at: datetime,
    expected_minutes: int,
) -> None:
    entry = derive_booking_pay_entry(
        _row(status="cancelled_by_client", cancelled_at=cancelled_at)
    )

    assert entry.minutes == expected_minutes


def test_cancelled_by_agency_pays_worker_only_inside_window() -> None:
    late = derive_booking_pay_entry(
        _row(status="cancelled_by_agency", cancelled_at=_START - timedelta(hours=3))
    )
    early = derive_booking_pay_entry(
        _row(status="cancelled_by_agency", cancelled_at=_START - timedelta(hours=48))
    )

    assert late.minutes == 240
    assert early.minutes == 0


def test_no_show_worker_is_unpaid() -> None:
    entry = derive_booking_pay_entry(_row(status="no_show_worker"))

    assert entry.minutes == 0


def test_pending_amend_marks_entry_unsettled_until_approved() -> None:
    entry = derive_booking_pay_entry(
        _row(pending_amend_minutes=300, pending_amend_reason="overrun")
    )

    assert entry.unsettled is True
    assert entry.minutes == 240


def test_scheduled_booking_is_unsettled_and_has_no_pay_minutes() -> None:
    entry = derive_booking_pay_entry(_row(status="scheduled"))

    assert entry.unsettled is True
    assert entry.minutes == 0


def test_cancelled_booking_requires_cancelled_at() -> None:
    with pytest.raises(BookingPayInvariantViolated, match="cancelled_at"):
        derive_booking_pay_entry(_row(status="cancelled_by_client"))
