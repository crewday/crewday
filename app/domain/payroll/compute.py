"""Payslip computation from booking-derived payroll entries."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from app.domain.payroll.bookings import derive_booking_pay_entry
from app.domain.payroll.ports import (
    BookingPayRow,
    PayPeriodRow,
    PayRuleRow,
    PayslipComputeRepository,
    PayslipReimbursableClaimRow,
    PayslipRow,
)
from app.events import EventBus
from app.events import bus as default_event_bus
from app.events.types import PayslipComputed
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.isoformat import iso_or_none
from app.util.ulid import new_ulid

__all__ = [
    "DEFAULT_WEEKLY_OVERTIME_THRESHOLD_MINUTES",
    "NIGHT_END",
    "NIGHT_START",
    "PayslipComputation",
    "PayslipComputeConflict",
    "PayslipInvariantViolated",
    "PayslipPeriodNotFound",
    "compute_payslip",
    "payslip_recompute",
]


DEFAULT_WEEKLY_OVERTIME_THRESHOLD_MINUTES = 40 * 60
NIGHT_START = time(22, 0)
NIGHT_END = time(6, 0)
_CENT = Decimal("1")
_HOUR = Decimal("60")
_ONE = Decimal("1")
_ZERO = Decimal("0")


class PayslipPeriodNotFound(LookupError):
    """The requested pay period is absent from the caller's workspace."""


class PayslipInvariantViolated(ValueError):
    """A payslip cannot be computed deterministically."""


class PayslipComputeConflict(RuntimeError):
    """The pay period state forbids recomputation."""


@dataclass(frozen=True, slots=True)
class _Segment:
    start: datetime
    minutes: int
    rule: PayRuleRow
    booking_id: str
    property_country: str | None


@dataclass(frozen=True, slots=True)
class _BookingSource:
    booking_id: str
    work_engagement_id: str
    rule_id: str
    minutes: int
    regular_minutes: int
    overtime_minutes: int
    night_minutes: int
    weekend_minutes: int
    holiday_minutes: int
    gross_cents: int

    def to_json(self) -> dict[str, object]:
        return {
            "booking_id": self.booking_id,
            "work_engagement_id": self.work_engagement_id,
            "rule_id": self.rule_id,
            "minutes": self.minutes,
            "regular_minutes": self.regular_minutes,
            "overtime_minutes": self.overtime_minutes,
            "night_minutes": self.night_minutes,
            "weekend_minutes": self.weekend_minutes,
            "holiday_minutes": self.holiday_minutes,
            "gross_cents": self.gross_cents,
        }


@dataclass(frozen=True, slots=True)
class _SegmentPay:
    regular_minutes: int
    overtime_minutes: int
    night_minutes: int
    weekend_minutes: int
    holiday_minutes: int
    base_cents: int
    overtime_cents: int
    night_cents: int
    weekend_cents: int
    holiday_cents: int
    holiday_multiplier: Decimal | None

    @property
    def gross_cents(self) -> int:
        return (
            self.base_cents
            + self.overtime_cents
            + self.night_cents
            + self.weekend_cents
            + self.holiday_cents
        )


@dataclass(slots=True)
class _BookingSourceTotals:
    booking_id: str
    work_engagement_id: str
    rule_id: str
    minutes: int = 0
    regular_minutes: int = 0
    overtime_minutes: int = 0
    night_minutes: int = 0
    weekend_minutes: int = 0
    holiday_minutes: int = 0
    gross_cents: int = 0

    def add_segment(self, segment: _Segment, pay: _SegmentPay) -> None:
        self.minutes += segment.minutes
        self.regular_minutes += pay.regular_minutes
        self.overtime_minutes += pay.overtime_minutes
        self.night_minutes += pay.night_minutes
        self.weekend_minutes += pay.weekend_minutes
        self.holiday_minutes += pay.holiday_minutes
        self.gross_cents += pay.gross_cents

    def to_source(self) -> _BookingSource:
        return _BookingSource(
            booking_id=self.booking_id,
            work_engagement_id=self.work_engagement_id,
            rule_id=self.rule_id,
            minutes=self.minutes,
            regular_minutes=self.regular_minutes,
            overtime_minutes=self.overtime_minutes,
            night_minutes=self.night_minutes,
            weekend_minutes=self.weekend_minutes,
            holiday_minutes=self.holiday_minutes,
            gross_cents=self.gross_cents,
        )


@dataclass(slots=True)
class _PayslipAggregation:
    weekly_minutes: dict[tuple[int, int], int] = field(default_factory=dict)
    gross_breakdown: dict[str, int] = field(default_factory=dict)
    sources_by_booking: dict[str, _BookingSourceTotals] = field(default_factory=dict)
    currencies: set[str] = field(default_factory=set)
    rule_ids: set[str] = field(default_factory=set)
    total_minutes: int = 0
    regular_minutes: int = 0
    overtime_minutes: int = 0
    night_minutes: int = 0
    weekend_minutes: int = 0
    holiday_minutes: int = 0

    @property
    def gross_cents(self) -> int:
        return sum(self.gross_breakdown.values())

    def add_booking(
        self,
        *,
        booking: BookingPayRow,
        period: PayPeriodRow,
        rule: PayRuleRow,
        holidays: Mapping[tuple[date, str | None], Decimal],
        paid_minutes: int,
    ) -> None:
        self.currencies.add(rule.currency)
        self.rule_ids.add(rule.id)
        source = self._source_for(booking=booking, rule=rule)
        if paid_minutes <= 0:
            return
        for segment in _iter_booking_segments(
            booking=booking,
            period=period,
            rule=rule,
        ):
            pay = _segment_pay(
                segment=segment,
                holidays=holidays,
                weekly_minutes=self.weekly_minutes,
            )
            self._add_segment(segment=segment, pay=pay, source=source)

    def sources(self) -> list[_BookingSource]:
        return [source.to_source() for source in self.sources_by_booking.values()]

    def _source_for(
        self,
        *,
        booking: BookingPayRow,
        rule: PayRuleRow,
    ) -> _BookingSourceTotals:
        source = self.sources_by_booking.get(booking.id)
        if source is None:
            source = _BookingSourceTotals(
                booking_id=booking.id,
                work_engagement_id=booking.work_engagement_id,
                rule_id=rule.id,
            )
            self.sources_by_booking[booking.id] = source
        return source

    def _add_segment(
        self,
        *,
        segment: _Segment,
        pay: _SegmentPay,
        source: _BookingSourceTotals,
    ) -> None:
        _add_segment_components(self.gross_breakdown, segment=segment, pay=pay)
        self.total_minutes += segment.minutes
        self.regular_minutes += pay.regular_minutes
        self.overtime_minutes += pay.overtime_minutes
        self.night_minutes += pay.night_minutes
        self.weekend_minutes += pay.weekend_minutes
        self.holiday_minutes += pay.holiday_minutes
        source.add_segment(segment, pay)


@dataclass(frozen=True, slots=True)
class _ReimbursementAggregation:
    folded: list[PayslipReimbursableClaimRow]
    skipped: list[PayslipReimbursableClaimRow]
    expense_reimbursements_cents: int


@dataclass(frozen=True, slots=True)
class PayslipComputation:
    """Computed payslip values ready for persistence."""

    workspace_id: str
    pay_period_id: str
    user_id: str
    currency: str
    shift_hours_decimal: Decimal
    overtime_hours_decimal: Decimal
    gross_cents: int
    deductions_cents: dict[str, int]
    expense_reimbursements_cents: int
    net_cents: int
    components_json: dict[str, object]


def _hours(minutes: int) -> Decimal:
    return (Decimal(minutes) / _HOUR).quantize(Decimal("0.01"))


def _cents_for_minutes(
    *,
    cents_per_hour: int,
    minutes: int,
    multiplier: Decimal,
) -> int:
    if minutes <= 0 or multiplier == _ZERO:
        return 0
    raw = Decimal(cents_per_hour) * Decimal(minutes) * multiplier / _HOUR
    return int(raw.quantize(_CENT, rounding=ROUND_HALF_EVEN))


def _premium_multiplier(multiplier: Decimal) -> Decimal:
    premium = multiplier - _ONE
    return premium if premium > _ZERO else _ZERO


def _component_key(prefix: str, multiplier: Decimal) -> str:
    pct = int((multiplier * Decimal(100)).quantize(_CENT, rounding=ROUND_HALF_EVEN))
    return f"{prefix}_{pct}"


def _is_night(moment: datetime) -> bool:
    current = moment.timetz().replace(tzinfo=None)
    return current >= NIGHT_START or current < NIGHT_END


def _next_time_boundary(moment: datetime, boundary: time) -> datetime:
    candidate = moment.replace(
        hour=boundary.hour,
        minute=boundary.minute,
        second=0,
        microsecond=0,
    )
    if candidate <= moment:
        candidate += timedelta(days=1)
    return candidate


def _next_midnight(moment: datetime) -> datetime:
    return (moment + timedelta(days=1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def _next_segment_boundary(moment: datetime, end: datetime) -> datetime:
    return min(
        end,
        _next_midnight(moment),
        _next_time_boundary(moment, NIGHT_END),
        _next_time_boundary(moment, NIGHT_START),
    )


def _segment_window(start: datetime, end: datetime) -> Iterator[tuple[datetime, int]]:
    cursor = start
    while cursor < end:
        boundary = _next_segment_boundary(cursor, end)
        minutes = int((boundary - cursor).total_seconds() // 60)
        if minutes <= 0:
            raise PayslipInvariantViolated("payable windows must split into minutes")
        yield cursor, minutes
        cursor = boundary


def _week_key(moment: datetime) -> tuple[int, int]:
    iso = moment.isocalendar()
    return iso.year, iso.week


def _holiday_multiplier(
    holidays: Mapping[tuple[date, str | None], Decimal],
    entry_date: date,
    property_country: str | None,
) -> Decimal | None:
    multiplier = holidays.get((entry_date, None))
    if property_country is not None:
        country_multiplier = holidays.get((entry_date, property_country))
        if country_multiplier is not None and (
            multiplier is None or country_multiplier > multiplier
        ):
            multiplier = country_multiplier
    if multiplier is None or multiplier <= _ONE:
        return None
    return multiplier


def _booking_paid_end(row: BookingPayRow, minutes: int) -> datetime:
    return row.scheduled_start + timedelta(minutes=minutes)


def _iter_booking_segments(
    *,
    booking: BookingPayRow,
    period: PayPeriodRow,
    rule: PayRuleRow,
) -> Iterator[_Segment]:
    entry = derive_booking_pay_entry(booking)
    if entry.minutes <= 0 or entry.unsettled:
        return

    paid_start = max(booking.scheduled_start, period.starts_at)
    paid_end = min(_booking_paid_end(booking, entry.minutes), period.ends_at)
    if paid_end <= paid_start:
        return

    for starts_at, minutes in _segment_window(paid_start, paid_end):
        yield _Segment(
            start=starts_at,
            minutes=minutes,
            rule=rule,
            booking_id=booking.id,
            property_country=booking.property_country,
        )


def _add_component(
    components: dict[str, int],
    *,
    key: str,
    cents: int,
) -> None:
    if cents:
        components[key] = components.get(key, 0) + cents


def _regular_overtime_split(
    segment: _Segment,
    weekly_minutes: dict[tuple[int, int], int],
) -> tuple[int, int]:
    week = _week_key(segment.start)
    before_week = weekly_minutes.get(week, 0)
    regular_left = max(0, DEFAULT_WEEKLY_OVERTIME_THRESHOLD_MINUTES - before_week)
    regular_minutes = min(segment.minutes, regular_left)
    weekly_minutes[week] = before_week + segment.minutes
    return regular_minutes, segment.minutes - regular_minutes


def _segment_pay(
    *,
    segment: _Segment,
    holidays: Mapping[tuple[date, str | None], Decimal],
    weekly_minutes: dict[tuple[int, int], int],
) -> _SegmentPay:
    regular_minutes, overtime_minutes = _regular_overtime_split(
        segment,
        weekly_minutes,
    )
    night_minutes = segment.minutes if _is_night(segment.start) else 0
    weekend_minutes = segment.minutes if segment.start.weekday() >= 5 else 0
    holiday = _holiday_multiplier(
        holidays,
        segment.start.date(),
        segment.property_country,
    )
    holiday_minutes = segment.minutes if holiday is not None else 0
    return _SegmentPay(
        regular_minutes=regular_minutes,
        overtime_minutes=overtime_minutes,
        night_minutes=night_minutes,
        weekend_minutes=weekend_minutes,
        holiday_minutes=holiday_minutes,
        base_cents=_cents_for_minutes(
            cents_per_hour=segment.rule.base_cents_per_hour,
            minutes=segment.minutes,
            multiplier=_ONE,
        ),
        overtime_cents=_premium_cents(
            segment=segment,
            minutes=overtime_minutes,
            multiplier=segment.rule.overtime_multiplier,
        ),
        night_cents=_premium_cents(
            segment=segment,
            minutes=night_minutes,
            multiplier=segment.rule.night_multiplier,
        ),
        weekend_cents=_premium_cents(
            segment=segment,
            minutes=weekend_minutes,
            multiplier=segment.rule.weekend_multiplier,
        ),
        holiday_cents=_premium_cents(
            segment=segment,
            minutes=holiday_minutes,
            multiplier=holiday if holiday is not None else _ONE,
        ),
        holiday_multiplier=holiday,
    )


def _premium_cents(
    *,
    segment: _Segment,
    minutes: int,
    multiplier: Decimal,
) -> int:
    return _cents_for_minutes(
        cents_per_hour=segment.rule.base_cents_per_hour,
        minutes=minutes,
        multiplier=_premium_multiplier(multiplier),
    )


def _add_segment_components(
    gross_breakdown: dict[str, int],
    *,
    segment: _Segment,
    pay: _SegmentPay,
) -> None:
    _add_component(gross_breakdown, key="base_pay", cents=pay.base_cents)
    _add_component(
        gross_breakdown,
        key=_component_key("overtime", segment.rule.overtime_multiplier),
        cents=pay.overtime_cents,
    )
    _add_component(
        gross_breakdown,
        key=_component_key("night", segment.rule.night_multiplier),
        cents=pay.night_cents,
    )
    _add_component(
        gross_breakdown,
        key=_component_key("weekend", segment.rule.weekend_multiplier),
        cents=pay.weekend_cents,
    )
    if pay.holiday_multiplier is not None:
        _add_component(
            gross_breakdown,
            key=_component_key("holiday", pay.holiday_multiplier),
            cents=pay.holiday_cents,
        )


def _reimbursement_to_json(claim: PayslipReimbursableClaimRow) -> dict[str, object]:
    return {
        "claim_id": claim.claim_id,
        "work_engagement_id": claim.work_engagement_id,
        "purchased_at": claim.purchased_at.isoformat(),
        "decided_at": iso_or_none(claim.decided_at),
        "description": claim.description,
        "currency": claim.currency,
        "amount_cents": claim.amount_cents,
    }


def _build_components_json(
    *,
    currency: str,
    gross_breakdown: dict[str, int],
    deductions_cents: dict[str, int],
    reimbursements: Sequence[PayslipReimbursableClaimRow],
    reimbursements_skipped: Sequence[PayslipReimbursableClaimRow],
    sources: Sequence[_BookingSource],
    total_minutes: int,
    regular_minutes: int,
    overtime_minutes: int,
    night_minutes: int,
    weekend_minutes: int,
    holiday_minutes: int,
    rule_ids: Sequence[str],
) -> dict[str, object]:
    # code-health: ignore[params] Persisted component schema keeps line fields explicit.
    return {
        "schema_version": 1,
        "currency": currency,
        "gross_breakdown": [
            {"key": key, "cents": cents}
            for key, cents in sorted(gross_breakdown.items())
            if cents
        ],
        "deductions": [
            {"key": key, "cents": cents, "reason": None}
            for key, cents in sorted(deductions_cents.items())
        ],
        "reimbursements": [_reimbursement_to_json(claim) for claim in reimbursements],
        "reimbursements_skipped": [
            _reimbursement_to_json(claim) for claim in reimbursements_skipped
        ],
        "statutory": [],
        "metadata": {
            "minutes_total": total_minutes,
            "minutes_regular": regular_minutes,
            "minutes_overtime": overtime_minutes,
            "minutes_night": night_minutes,
            "minutes_weekend": weekend_minutes,
            "minutes_holiday": holiday_minutes,
            "hours_total": str(_hours(total_minutes)),
            "hours_regular": str(_hours(regular_minutes)),
            "hours_overtime": str(_hours(overtime_minutes)),
            "rule_ids": list(rule_ids),
            "sources": [source.to_json() for source in sources],
        },
    }


def _fold_reimbursements(
    claims: Sequence[PayslipReimbursableClaimRow],
    *,
    payslip_currency: str,
) -> _ReimbursementAggregation:
    # Spec §09 §"Currency mismatch": cross-currency claims are not a
    # payroll-blocking error. v1 cannot fold them into the single-currency
    # net, so they stay visible for manual reimbursement handling.
    folded: list[PayslipReimbursableClaimRow] = []
    skipped: list[PayslipReimbursableClaimRow] = []
    for claim in claims:
        if claim.currency == payslip_currency:
            folded.append(claim)
        else:
            skipped.append(claim)
    return _ReimbursementAggregation(
        folded=folded,
        skipped=skipped,
        expense_reimbursements_cents=sum(claim.amount_cents for claim in folded),
    )


def _computed_payslip(
    *,
    aggregation: _PayslipAggregation,
    ctx: WorkspaceContext,
    period: PayPeriodRow,
    user_id: str,
    currency: str,
    reimbursements: _ReimbursementAggregation,
) -> PayslipComputation:
    deductions_cents: dict[str, int] = {}
    gross_cents = aggregation.gross_cents
    net_cents = (
        gross_cents
        - sum(deductions_cents.values())
        + reimbursements.expense_reimbursements_cents
    )
    return PayslipComputation(
        workspace_id=ctx.workspace_id,
        pay_period_id=period.id,
        user_id=user_id,
        currency=currency,
        shift_hours_decimal=_hours(aggregation.total_minutes),
        overtime_hours_decimal=_hours(aggregation.overtime_minutes),
        gross_cents=gross_cents,
        deductions_cents=deductions_cents,
        expense_reimbursements_cents=reimbursements.expense_reimbursements_cents,
        net_cents=net_cents,
        components_json=_build_components_json(
            currency=currency,
            gross_breakdown=aggregation.gross_breakdown,
            deductions_cents=deductions_cents,
            reimbursements=reimbursements.folded,
            reimbursements_skipped=reimbursements.skipped,
            sources=aggregation.sources(),
            total_minutes=aggregation.total_minutes,
            regular_minutes=aggregation.regular_minutes,
            overtime_minutes=aggregation.overtime_minutes,
            night_minutes=aggregation.night_minutes,
            weekend_minutes=aggregation.weekend_minutes,
            holiday_minutes=aggregation.holiday_minutes,
            rule_ids=sorted(aggregation.rule_ids),
        ),
    )


def _aggregate_bookings(
    *,
    repo: PayslipComputeRepository,
    ctx: WorkspaceContext,
    period: PayPeriodRow,
    bookings: Sequence[BookingPayRow],
    holidays: Mapping[tuple[date, str | None], Decimal],
) -> _PayslipAggregation:
    aggregation = _PayslipAggregation()
    for booking in bookings:
        entry = derive_booking_pay_entry(booking)
        if entry.unsettled:
            continue
        rule = repo.get_effective_pay_rule(
            workspace_id=ctx.workspace_id,
            user_id=booking.user_id,
            at=booking.scheduled_start,
        )
        if rule is None:
            raise PayslipInvariantViolated(
                f"missing active pay rule for booking {booking.id}"
            )
        aggregation.add_booking(
            booking=booking,
            period=period,
            rule=rule,
            holidays=holidays,
            paid_minutes=entry.minutes,
        )
    return aggregation


def compute_payslip(
    repo: PayslipComputeRepository,
    ctx: WorkspaceContext,
    *,
    period: PayPeriodRow,
    user_id: str,
) -> PayslipComputation:
    """Compute one user's draft payslip from booking-derived payroll data."""
    bookings = repo.list_pay_bearing_bookings(
        workspace_id=ctx.workspace_id,
        starts_at=period.starts_at,
        ends_at=period.ends_at,
        user_id=user_id,
    )
    countries = {
        booking.property_country
        for booking in bookings
        if booking.property_country is not None
    }
    holidays = repo.list_holiday_multipliers(
        workspace_id=ctx.workspace_id,
        starts_on=period.starts_at.date(),
        ends_before=period.ends_at.date() + timedelta(days=1),
        countries=countries,
    )

    aggregation = _aggregate_bookings(
        repo=repo,
        ctx=ctx,
        period=period,
        bookings=bookings,
        holidays=holidays,
    )
    if not aggregation.currencies:
        raise PayslipInvariantViolated(f"no pay-bearing bookings for user {user_id}")
    if len(aggregation.currencies) > 1:
        raise PayslipInvariantViolated(
            f"mixed currencies for user {user_id}: "
            f"{', '.join(sorted(aggregation.currencies))}"
        )

    payslip_currency = next(iter(aggregation.currencies))
    reimbursable = repo.list_reimbursable_claims_for_payslip(
        workspace_id=ctx.workspace_id,
        user_id=user_id,
        pay_period_id=period.id,
        starts_at=period.starts_at,
        ends_at=period.ends_at,
    )
    reimbursements = _fold_reimbursements(
        reimbursable,
        payslip_currency=payslip_currency,
    )
    return _computed_payslip(
        aggregation=aggregation,
        ctx=ctx,
        period=period,
        user_id=user_id,
        currency=payslip_currency,
        reimbursements=reimbursements,
    )


def payslip_recompute(
    repo: PayslipComputeRepository,
    ctx: WorkspaceContext,
    *,
    period_id: str,
    event_bus: EventBus | None = None,
    clock: Clock | None = None,
) -> Sequence[PayslipRow]:
    """Recompute every draft payslip for a pay period idempotently."""
    # code-health: ignore[nloc] Recompute keeps period validation, entry refresh, payslip upserts, and events in one ordered transaction.  # noqa: E501

    period = repo.get_period(workspace_id=ctx.workspace_id, period_id=period_id)
    if period is None:
        raise PayslipPeriodNotFound(period_id)
    if period.state == "paid":
        raise PayslipComputeConflict("paid pay periods cannot be recomputed")
    if period.state not in {"open", "locked"}:
        raise PayslipComputeConflict(f"unsupported pay period state: {period.state}")
    if repo.has_paid_payslip(workspace_id=ctx.workspace_id, period_id=period.id):
        raise PayslipComputeConflict(
            "pay periods with paid payslips cannot be recomputed"
        )

    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    entries = repo.replace_period_entries(
        workspace_id=ctx.workspace_id,
        pay_period_id=period.id,
        starts_at=period.starts_at,
        ends_at=period.ends_at,
        now=now,
    )
    if not entries:
        entries = repo.list_period_entries(
            workspace_id=ctx.workspace_id,
            pay_period_id=period.id,
        )

    user_ids = sorted({entry.user_id for entry in entries})
    rows: list[PayslipRow] = []
    publisher = event_bus if event_bus is not None else default_event_bus
    for user_id in user_ids:
        computed = compute_payslip(repo, ctx, period=period, user_id=user_id)
        row = repo.upsert_payslip(
            payslip_id=new_ulid(clock=resolved_clock),
            workspace_id=ctx.workspace_id,
            pay_period_id=period.id,
            user_id=user_id,
            shift_hours_decimal=computed.shift_hours_decimal,
            overtime_hours_decimal=computed.overtime_hours_decimal,
            gross_cents=computed.gross_cents,
            deductions_cents=computed.deductions_cents,
            expense_reimbursements_cents=computed.expense_reimbursements_cents,
            net_cents=computed.net_cents,
            components_json=computed.components_json,
            now=now,
        )
        rows.append(row)
        publisher.publish(
            PayslipComputed(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=now,
                pay_period_id=period.id,
                payslip_id=row.id,
                user_id=user_id,
            )
        )
    return rows
