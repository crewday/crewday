"""Unit tests for payslip computation from booking payroll data."""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.payroll.compute import (
    PayslipComputation,
    compute_payslip,
)
from app.domain.payroll.ports import (
    BookingPayRow,
    PayPeriodEntryRow,
    PayPeriodRow,
    PayRuleRow,
    PayslipReimbursableClaimRow,
    PayslipRow,
)
from app.tenancy import WorkspaceContext

_WORKSPACE_ID = "01HWA00000000000000000WS01"
_USER_ID = "01HWA00000000000000000USR1"
_ENGAGEMENT_ID = "01HWA00000000000000000ENG1"
_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_PERIOD = PayPeriodRow(
    id="01HWA00000000000000000PER1",
    workspace_id=_WORKSPACE_ID,
    starts_at=datetime(2026, 5, 1, tzinfo=UTC),
    ends_at=datetime(2026, 5, 15, tzinfo=UTC),
    state="locked",
    locked_at=_NOW,
    locked_by="manager",
    created_at=_NOW,
)


class FakeRepo:
    def __init__(
        self,
        *,
        bookings: Sequence[BookingPayRow],
        rules: Sequence[PayRuleRow],
        holidays: Mapping[tuple[date, str | None], Decimal] | None = None,
        reimbursable: Sequence[PayslipReimbursableClaimRow] = (),
    ) -> None:
        self.bookings = list(bookings)
        self.rules = list(rules)
        self.holidays = dict(holidays or {})
        self.reimbursable = list(reimbursable)

    @property
    def session(self) -> Session:
        raise AssertionError("unit fake has no SQLAlchemy session")

    def get_period(self, *, workspace_id: str, period_id: str) -> PayPeriodRow | None:
        return None

    def replace_period_entries(
        self,
        *,
        workspace_id: str,
        pay_period_id: str,
        starts_at: datetime,
        ends_at: datetime,
        now: datetime,
    ) -> Sequence[PayPeriodEntryRow]:
        return ()

    def list_period_entries(
        self,
        *,
        workspace_id: str,
        pay_period_id: str,
    ) -> Sequence[PayPeriodEntryRow]:
        return ()

    def list_pay_bearing_bookings(
        self,
        *,
        workspace_id: str,
        starts_at: datetime,
        ends_at: datetime,
        user_id: str | None = None,
        work_engagement_id: str | None = None,
    ) -> Sequence[BookingPayRow]:
        return [
            booking
            for booking in self.bookings
            if booking.workspace_id == workspace_id
            and booking.scheduled_start < ends_at
            and booking.scheduled_end > starts_at
            and (user_id is None or booking.user_id == user_id)
            and (
                work_engagement_id is None
                or booking.work_engagement_id == work_engagement_id
            )
        ]

    def get_effective_pay_rule(
        self,
        *,
        workspace_id: str,
        user_id: str,
        at: datetime,
    ) -> PayRuleRow | None:
        matches = [
            rule
            for rule in self.rules
            if rule.workspace_id == workspace_id
            and rule.user_id == user_id
            and rule.effective_from <= at
            and (rule.effective_to is None or rule.effective_to >= at)
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda rule: (rule.effective_from, rule.id))[-1]

    def list_holiday_multipliers(
        self,
        *,
        workspace_id: str,
        starts_on: date,
        ends_before: date,
        countries: Set[str],
    ) -> Mapping[tuple[date, str | None], Decimal]:
        return {
            key: multiplier
            for key, multiplier in self.holidays.items()
            for day, country in (key,)
            if starts_on <= day < ends_before
            and (country is None or country in countries)
        }

    def has_paid_payslip(self, *, workspace_id: str, period_id: str) -> bool:
        return False

    def upsert_payslip(
        self,
        *,
        payslip_id: str,
        workspace_id: str,
        pay_period_id: str,
        user_id: str,
        shift_hours_decimal: Decimal,
        overtime_hours_decimal: Decimal,
        gross_cents: int,
        deductions_cents: dict[str, int],
        expense_reimbursements_cents: int,
        net_cents: int,
        components_json: dict[str, object],
        now: datetime,
    ) -> PayslipRow:
        raise AssertionError("unit compute tests do not persist")

    def list_reimbursable_claims_for_payslip(
        self,
        *,
        workspace_id: str,
        user_id: str,
        starts_at: datetime,
        ends_at: datetime,
    ) -> Sequence[PayslipReimbursableClaimRow]:
        return [
            claim
            for claim in self.reimbursable
            if starts_at <= claim.purchased_at < ends_at
        ]


def _ctx() -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=_WORKSPACE_ID,
        workspace_slug="payroll",
        actor_id="manager",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000COR1",
    )


def _rule(
    *,
    rule_id: str = "01HWA00000000000000000RUL1",
    hourly_cents: int = 1000,
    effective_from: datetime = datetime(2026, 1, 1, tzinfo=UTC),
    overtime_multiplier: Decimal = Decimal("1.5"),
    night_multiplier: Decimal = Decimal("1.25"),
    weekend_multiplier: Decimal = Decimal("1.5"),
) -> PayRuleRow:
    return PayRuleRow(
        id=rule_id,
        workspace_id=_WORKSPACE_ID,
        user_id=_USER_ID,
        currency="USD",
        base_cents_per_hour=hourly_cents,
        overtime_multiplier=overtime_multiplier,
        night_multiplier=night_multiplier,
        weekend_multiplier=weekend_multiplier,
        effective_from=effective_from,
        effective_to=None,
        created_by="manager",
        created_at=effective_from,
    )


def _booking(
    *,
    booking_id: str,
    start: datetime,
    minutes: int,
    property_country: str | None = None,
    status: str = "completed",
) -> BookingPayRow:
    return BookingPayRow(
        id=booking_id,
        workspace_id=_WORKSPACE_ID,
        work_engagement_id=_ENGAGEMENT_ID,
        user_id=_USER_ID,
        property_id=None,
        property_country=property_country,
        status=status,
        kind="work",
        pay_basis="scheduled",
        scheduled_start=start,
        scheduled_end=start + timedelta(minutes=minutes),
        actual_minutes=None,
        actual_minutes_paid=minutes,
        break_seconds=0,
        adjusted=False,
        adjustment_reason=None,
        pending_amend_minutes=None,
        pending_amend_reason=None,
        cancelled_at=None,
        cancellation_window_hours=24,
        cancellation_pay_to_worker=True,
        created_at=start - timedelta(days=1),
        updated_at=start - timedelta(days=1),
    )


def _gross_components(result: PayslipComputation) -> dict[str, int]:
    components = result.components_json["gross_breakdown"]
    assert isinstance(components, list)
    rendered: dict[str, int] = {}
    for item in components:
        assert isinstance(item, dict)
        key = item.get("key")
        cents = item.get("cents")
        assert isinstance(key, str)
        assert isinstance(cents, int)
        rendered[key] = cents
    return rendered


def test_midnight_weekend_night_and_holiday_premiums_are_split() -> None:
    repo = FakeRepo(
        bookings=[
            _booking(
                booking_id="01HWA00000000000000000BKG1",
                start=datetime(2026, 5, 1, 21, 0, tzinfo=UTC),
                minutes=360,
            )
        ],
        rules=[_rule()],
        holidays={(date(2026, 5, 1), None): Decimal("2.0")},
    )

    result = compute_payslip(repo, _ctx(), period=_PERIOD, user_id=_USER_ID)

    assert result.gross_cents == 11750
    assert result.net_cents == 11750
    assert result.shift_hours_decimal == Decimal("6.00")
    metadata = result.components_json["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["minutes_regular"] == 360
    assert metadata["minutes_overtime"] == 0
    assert metadata["minutes_night"] == 300
    assert metadata["minutes_weekend"] == 180
    assert metadata["minutes_holiday"] == 180
    assert _gross_components(result) == {
        "base_pay": 6000,
        "holiday_200": 3000,
        "night_125": 1250,
        "weekend_150": 1500,
    }


def test_weekly_overtime_applies_above_forty_hours() -> None:
    bookings = [
        _booking(
            booking_id=f"01HWA0000000000000000BKG{day}",
            start=datetime(2026, 5, 4 + day, 8, 0, tzinfo=UTC),
            minutes=480,
        )
        for day in range(5)
    ]
    bookings.append(
        _booking(
            booking_id="01HWA00000000000000000BKGA",
            start=datetime(2026, 5, 9, 8, 0, tzinfo=UTC),
            minutes=60,
        )
    )
    repo = FakeRepo(
        bookings=bookings,
        rules=[
            _rule(
                night_multiplier=Decimal("1"),
                weekend_multiplier=Decimal("1"),
            )
        ],
    )

    result = compute_payslip(repo, _ctx(), period=_PERIOD, user_id=_USER_ID)

    assert result.gross_cents == 41500
    assert result.overtime_hours_decimal == Decimal("1.00")
    metadata = result.components_json["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["minutes_regular"] == 2400
    assert metadata["minutes_overtime"] == 60
    assert _gross_components(result) == {
        "base_pay": 41000,
        "overtime_150": 500,
    }


def test_booking_uses_rule_active_at_booking_start() -> None:
    old_rule = _rule(
        rule_id="01HWA00000000000000000RUL1",
        hourly_cents=1000,
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        overtime_multiplier=Decimal("1"),
        night_multiplier=Decimal("1"),
        weekend_multiplier=Decimal("1"),
    )
    new_rule = _rule(
        rule_id="01HWA00000000000000000RUL2",
        hourly_cents=2000,
        effective_from=datetime(2026, 5, 2, tzinfo=UTC),
        overtime_multiplier=Decimal("1"),
        night_multiplier=Decimal("1"),
        weekend_multiplier=Decimal("1"),
    )
    repo = FakeRepo(
        bookings=[
            _booking(
                booking_id="01HWA00000000000000000BKG1",
                start=datetime(2026, 5, 1, 23, 0, tzinfo=UTC),
                minutes=120,
            )
        ],
        rules=[old_rule, new_rule],
    )

    result = compute_payslip(repo, _ctx(), period=_PERIOD, user_id=_USER_ID)

    assert result.gross_cents == 2000
    metadata = result.components_json["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["rule_ids"] == [old_rule.id]


def test_zero_minute_settled_booking_computes_zero_payslip() -> None:
    repo = FakeRepo(
        bookings=[
            _booking(
                booking_id="01HWA00000000000000000BKG1",
                start=datetime(2026, 5, 4, 8, 0, tzinfo=UTC),
                minutes=120,
                status="no_show_worker",
            )
        ],
        rules=[_rule()],
    )

    result = compute_payslip(repo, _ctx(), period=_PERIOD, user_id=_USER_ID)

    assert result.gross_cents == 0
    assert result.net_cents == 0
    assert result.shift_hours_decimal == Decimal("0.00")
    assert _gross_components(result) == {}
    metadata = result.components_json["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["minutes_total"] == 0
    assert metadata["rule_ids"] == ["01HWA00000000000000000RUL1"]


def test_country_specific_holiday_multiplier_applies_to_matching_property() -> None:
    repo = FakeRepo(
        bookings=[
            _booking(
                booking_id="01HWA00000000000000000BKG1",
                start=datetime(2026, 5, 1, 8, 0, tzinfo=UTC),
                minutes=60,
                property_country="FR",
            )
        ],
        rules=[
            _rule(
                night_multiplier=Decimal("1"),
                weekend_multiplier=Decimal("1"),
            )
        ],
        holidays={
            (date(2026, 5, 1), None): Decimal("1.5"),
            (date(2026, 5, 1), "FR"): Decimal("2.0"),
            (date(2026, 5, 1), "IT"): Decimal("3.0"),
        },
    )

    result = compute_payslip(repo, _ctx(), period=_PERIOD, user_id=_USER_ID)

    assert result.gross_cents == 2000
    assert _gross_components(result) == {
        "base_pay": 1000,
        "holiday_200": 1000,
    }


def _claim(
    *,
    claim_id: str = "01HWA0000000000000000CLM1",
    purchased_at: datetime,
    amount_cents: int,
    currency: str = "USD",
    description: str = "fuel",
) -> PayslipReimbursableClaimRow:
    return PayslipReimbursableClaimRow(
        claim_id=claim_id,
        work_engagement_id=_ENGAGEMENT_ID,
        purchased_at=purchased_at,
        decided_at=purchased_at + timedelta(days=1),
        description=description,
        currency=currency,
        amount_cents=amount_cents,
    )


def test_approved_claim_in_period_folds_into_net_and_components() -> None:
    repo = FakeRepo(
        bookings=[
            _booking(
                booking_id="01HWA00000000000000000BKG1",
                start=datetime(2026, 5, 4, 8, 0, tzinfo=UTC),
                minutes=120,
            )
        ],
        rules=[
            _rule(
                night_multiplier=Decimal("1"),
                weekend_multiplier=Decimal("1"),
            )
        ],
        reimbursable=[
            _claim(
                claim_id="01HWA0000000000000000CLM1",
                purchased_at=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
                amount_cents=4500,
                description="fuel",
            ),
            _claim(
                claim_id="01HWA0000000000000000CLM2",
                purchased_at=datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
                amount_cents=1500,
                description="parking",
            ),
        ],
    )

    result = compute_payslip(repo, _ctx(), period=_PERIOD, user_id=_USER_ID)

    assert result.gross_cents == 2000
    assert result.expense_reimbursements_cents == 6000
    # net = gross - deductions + reimbursements
    assert result.net_cents == 8000

    reimbursements = result.components_json["reimbursements"]
    assert isinstance(reimbursements, list)
    assert len(reimbursements) == 2
    first = reimbursements[0]
    assert isinstance(first, dict)
    assert first["claim_id"] == "01HWA0000000000000000CLM1"
    assert first["amount_cents"] == 4500
    assert first["description"] == "fuel"
    assert first["currency"] == "USD"
    second = reimbursements[1]
    assert isinstance(second, dict)
    assert second["claim_id"] == "01HWA0000000000000000CLM2"


def test_claim_outside_period_window_is_excluded() -> None:
    # Claim purchased before period.starts_at — must not be folded.
    before = _claim(
        claim_id="01HWA0000000000000000CLMB",
        purchased_at=datetime(2026, 4, 30, 12, 0, tzinfo=UTC),
        amount_cents=999,
    )
    # Claim purchased exactly at period.ends_at — exclusive bound, must
    # not be folded either.
    after = _claim(
        claim_id="01HWA0000000000000000CLMA",
        purchased_at=datetime(2026, 5, 15, 0, 0, tzinfo=UTC),
        amount_cents=999,
    )
    repo = FakeRepo(
        bookings=[
            _booking(
                booking_id="01HWA00000000000000000BKG1",
                start=datetime(2026, 5, 4, 8, 0, tzinfo=UTC),
                minutes=120,
            )
        ],
        rules=[
            _rule(
                night_multiplier=Decimal("1"),
                weekend_multiplier=Decimal("1"),
            )
        ],
        reimbursable=[before, after],
    )

    result = compute_payslip(repo, _ctx(), period=_PERIOD, user_id=_USER_ID)

    assert result.expense_reimbursements_cents == 0
    assert result.net_cents == result.gross_cents
    reimbursements = result.components_json["reimbursements"]
    assert reimbursements == []


def test_no_approved_claims_yields_empty_reimbursements() -> None:
    repo = FakeRepo(
        bookings=[
            _booking(
                booking_id="01HWA00000000000000000BKG1",
                start=datetime(2026, 5, 4, 8, 0, tzinfo=UTC),
                minutes=120,
            )
        ],
        rules=[
            _rule(
                night_multiplier=Decimal("1"),
                weekend_multiplier=Decimal("1"),
            )
        ],
    )

    result = compute_payslip(repo, _ctx(), period=_PERIOD, user_id=_USER_ID)

    assert result.expense_reimbursements_cents == 0
    assert result.components_json["reimbursements"] == []


def test_cross_currency_claim_is_skipped_not_folded() -> None:
    """Spec §09 §"Currency mismatch": cross-currency claims aren't a
    payroll-blocking error. v1 leaves them for the manual
    ``mark_reimbursed`` route — compute folds same-currency claims and
    surfaces the skipped ones in
    ``components_json["reimbursements_skipped"]`` so the PDF / API can
    render a "settled out of band" hint.
    """
    repo = FakeRepo(
        bookings=[
            _booking(
                booking_id="01HWA00000000000000000BKG1",
                start=datetime(2026, 5, 4, 8, 0, tzinfo=UTC),
                minutes=120,
            )
        ],
        rules=[
            _rule(
                night_multiplier=Decimal("1"),
                weekend_multiplier=Decimal("1"),
            )
        ],
        reimbursable=[
            _claim(
                claim_id="01HWA0000000000000000CLM1",
                purchased_at=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
                amount_cents=4500,
                currency="USD",
                description="fuel",
            ),
            _claim(
                claim_id="01HWA0000000000000000CLM2",
                purchased_at=datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
                amount_cents=8800,
                currency="EUR",
                description="hotel",
            ),
        ],
    )

    result = compute_payslip(repo, _ctx(), period=_PERIOD, user_id=_USER_ID)

    # Only the USD claim is folded; EUR claim lands in the skipped slot.
    assert result.expense_reimbursements_cents == 4500
    assert result.net_cents == result.gross_cents + 4500

    folded = result.components_json["reimbursements"]
    assert isinstance(folded, list)
    assert len(folded) == 1
    folded_first = folded[0]
    assert isinstance(folded_first, dict)
    assert folded_first["claim_id"] == "01HWA0000000000000000CLM1"
    assert folded_first["currency"] == "USD"

    skipped = result.components_json["reimbursements_skipped"]
    assert isinstance(skipped, list)
    assert len(skipped) == 1
    skipped_first = skipped[0]
    assert isinstance(skipped_first, dict)
    assert skipped_first["claim_id"] == "01HWA0000000000000000CLM2"
    assert skipped_first["currency"] == "EUR"
