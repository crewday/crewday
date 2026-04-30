"""Unit tests for client portal read helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.domain.billing.client_portal import (
    ClientPortalAccrualRow,
    raw_accruals_to_billable_hours,
)


def test_billable_hours_group_by_work_order_property_and_week() -> None:
    rows = (
        ClientPortalAccrualRow(
            work_order_id="wo-a",
            property_id="prop-a",
            property_name="Alpha Villa",
            organization_id="org-a",
            currency="EUR",
            hours_decimal=Decimal("1.25"),
            accrued_cents=12500,
            created_at=datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
        ),
        ClientPortalAccrualRow(
            work_order_id="wo-a",
            property_id="prop-a",
            property_name="Alpha Villa",
            organization_id="org-a",
            currency="EUR",
            hours_decimal=Decimal("2.50"),
            accrued_cents=25000,
            created_at=datetime(2026, 4, 30, 10, 0, tzinfo=UTC),
        ),
        ClientPortalAccrualRow(
            work_order_id="wo-a",
            property_id="prop-a",
            property_name="Alpha Villa",
            organization_id="org-a",
            currency="EUR",
            hours_decimal=Decimal("3.00"),
            accrued_cents=30000,
            created_at=datetime(2026, 5, 5, 10, 0, tzinfo=UTC),
        ),
    )

    grouped = raw_accruals_to_billable_hours(rows)

    assert len(grouped) == 2
    assert grouped[0].week_start.isoformat() == "2026-04-27"
    assert grouped[0].hours_decimal == Decimal("3.75")
    assert grouped[0].total_cents == 37500
    assert grouped[1].week_start.isoformat() == "2026-05-04"


def test_billable_hours_projection_has_no_staff_or_cost_fields() -> None:
    grouped = raw_accruals_to_billable_hours(
        (
            ClientPortalAccrualRow(
                work_order_id="wo-a",
                property_id="prop-a",
                property_name="Alpha Villa",
                organization_id="org-a",
                currency="EUR",
                hours_decimal=Decimal("1.00"),
                accrued_cents=10000,
                created_at=datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
            ),
        )
    )

    fields = set(grouped[0].__dataclass_fields__)
    assert "shift_id" not in fields
    assert "hourly_rate_cents" not in fields
    assert "worker_id" not in fields
    assert "staff_name" not in fields
