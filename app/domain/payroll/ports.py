"""Payroll context — repository ports.

Defines :class:`PayRuleRepository`, the seam
:mod:`app.domain.payroll.rules` uses to read and write
``pay_rule`` rows plus the ``pay_period`` / ``payslip`` lookups the
"locked-period" guard needs — without importing SQLAlchemy model
classes directly (cd-ea7).

Spec: ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 —
each context defines its own repository port in its public surface
(``app/domain/<context>/ports.py``) and a SQLAlchemy adapter under
``app/adapters/db/<context>/``. Mirrors the cd-kezq seam shape
introduced for places.

The repo carries an open SQLAlchemy ``Session`` so the audit writer
(:func:`app.audit.write_audit`) — which still takes a concrete
``Session`` today — can ride the same Unit of Work without forcing
callers to thread a second seam. Drops once the audit writer gains
its own Protocol.

The repo-shaped value object :class:`PayRuleRow` mirrors the domain's
:class:`~app.domain.payroll.rules.PayRuleView`. It lives on the seam
so the SA adapter has a domain-owned shape to project ORM rows into
without importing the service module that produces the view (which
would create a circular dependency between ``rules`` and this
module).

Protocol is deliberately **not** ``runtime_checkable``: structural
compatibility is checked statically by mypy. Runtime ``isinstance``
against this Protocol would mask typos and invite duck-typing
shortcuts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol

from sqlalchemy.orm import Session

__all__ = [
    "BookingPayRepository",
    "BookingPayRow",
    "ExpenseLedgerExportRow",
    "PayPeriodEntryRow",
    "PayPeriodRecomputeScheduler",
    "PayPeriodRepository",
    "PayPeriodRow",
    "PayRuleRepository",
    "PayRuleRow",
    "PayrollExportRepository",
    "PayslipComputeRepository",
    "PayslipExportRow",
    "PayslipReadRepository",
    "PayslipReadRow",
    "PayslipRow",
    "TimesheetExportRow",
]


# ---------------------------------------------------------------------------
# Row shape (value object)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PayRuleRow:
    """Immutable projection of a ``pay_rule`` row.

    Mirrors the shape of
    :class:`app.domain.payroll.rules.PayRuleView`; declared here so
    the Protocol surface does not depend on the service module
    (which itself imports this seam).
    """

    id: str
    workspace_id: str
    user_id: str
    currency: str
    base_cents_per_hour: int
    overtime_multiplier: Decimal
    night_multiplier: Decimal
    weekend_multiplier: Decimal
    effective_from: datetime
    effective_to: datetime | None
    created_by: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PayPeriodRow:
    """Immutable projection of a ``pay_period`` row."""

    id: str
    workspace_id: str
    starts_at: datetime
    ends_at: datetime
    state: str
    locked_at: datetime | None
    locked_by: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BookingPayRow:
    """Immutable projection of a pay-bearing ``booking`` row."""

    id: str
    workspace_id: str
    work_engagement_id: str
    user_id: str
    property_id: str | None
    property_country: str | None
    status: str
    kind: str
    pay_basis: Literal["scheduled", "actual"]
    scheduled_start: datetime
    scheduled_end: datetime
    actual_minutes: int | None
    actual_minutes_paid: int
    break_seconds: int
    adjusted: bool
    adjustment_reason: str | None
    pending_amend_minutes: int | None
    pending_amend_reason: str | None
    cancelled_at: datetime | None
    cancellation_window_hours: int
    cancellation_pay_to_worker: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PayPeriodEntryRow:
    """Immutable projection of a daily booking payroll ledger row."""

    id: str
    workspace_id: str
    pay_period_id: str
    work_engagement_id: str
    user_id: str
    entry_date: date
    minutes: int
    source_booking_ids: tuple[str, ...]
    source_details: tuple[dict[str, object], ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PayslipRow:
    """Immutable projection of a computed ``payslip`` row."""

    id: str
    workspace_id: str
    pay_period_id: str
    user_id: str
    shift_hours_decimal: Decimal
    overtime_hours_decimal: Decimal
    gross_cents: int
    deductions_cents: dict[str, int]
    net_cents: int
    components_json: dict[str, object]
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PayslipReadRow:
    """Immutable projection for the REST payslip read surface."""

    id: str
    workspace_id: str
    pay_period_id: str
    user_id: str
    currency: str
    shift_hours_decimal: Decimal
    overtime_hours_decimal: Decimal
    gross_cents: int
    deductions_cents: dict[str, int]
    net_cents: int
    components_json: dict[str, object]
    status: str
    issued_at: datetime | None
    paid_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TimesheetExportRow:
    """Flat row for the manager/accountant timesheet CSV export."""

    shift_id: str
    user_email: str
    property_label: str
    starts_at_utc: datetime
    ends_at_utc: datetime
    hours_decimal: Decimal
    source: str
    notes: str


@dataclass(frozen=True, slots=True)
class PayslipExportRow:
    """Flat row for the payroll register CSV export."""

    payslip_id: str
    user_email: str
    period_starts_at: datetime
    period_ends_at: datetime
    hours: Decimal
    overtime_hours: Decimal
    gross_cents: int
    deductions_cents: int
    net_cents: int
    currency: str
    paid_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExpenseLedgerExportRow:
    """Flat row for the approved/reimbursed expense ledger CSV export."""

    expense_id: str
    claimant_email: str
    vendor: str
    spent_at: datetime
    category: str
    amount_cents: int
    currency: str
    property_label: str
    decided_at: datetime | None
    reimbursed_via: str


class PayPeriodRecomputeScheduler(Protocol):
    """Port for scheduling payslip recomputation after a period locks."""

    def schedule_period_recompute(
        self,
        *,
        workspace_id: str,
        period_id: str,
    ) -> None:
        """Schedule the Phase 8 payslip recompute for ``period_id``."""
        ...


class PayrollExportRepository(Protocol):
    """Read seam for CSV payroll exports."""

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session."""
        ...

    def pay_period_window(
        self, *, workspace_id: str, period_id: str
    ) -> tuple[datetime, datetime] | None:
        """Return a pay period window or ``None`` when absent."""
        ...

    def iter_timesheets(
        self, *, workspace_id: str, since: datetime, until: datetime
    ) -> Iterable[TimesheetExportRow]:
        """Return closed shifts intersecting the export window."""
        ...

    def iter_payslips(
        self,
        *,
        workspace_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        period_id: str | None = None,
    ) -> Iterable[PayslipExportRow]:
        """Return payslips for one period or an intersecting date window."""
        ...

    def iter_expense_ledger(
        self,
        *,
        workspace_id: str,
        since: datetime,
        until: datetime,
        status_filter: str,
    ) -> Iterable[ExpenseLedgerExportRow]:
        """Return expense claims in the export window."""
        ...


class PayslipReadRepository(Protocol):
    """Read seam for payslip REST list/detail routes."""

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session."""
        ...

    def get_payslip(
        self, *, workspace_id: str, payslip_id: str
    ) -> PayslipReadRow | None:
        """Return one payslip or ``None`` when absent."""
        ...

    def list_payslips(
        self,
        *,
        workspace_id: str,
        user_id: str | None = None,
        pay_period_id: str | None = None,
    ) -> Sequence[PayslipReadRow]:
        """Return payslips ordered newest period first."""
        ...

    def set_payslip_state(
        self,
        *,
        workspace_id: str,
        payslip_id: str,
        status: Literal["issued", "paid", "voided"],
        issued_at: datetime | None = None,
        paid_at: datetime | None = None,
        payout_snapshot_json: dict[str, object] | None = None,
    ) -> PayslipReadRow:
        """Persist a payslip state transition and return the refreshed row."""
        ...


class PayslipComputeRepository(Protocol):
    """Read + write seam for payslip recomputation."""

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session."""
        ...

    def get_period(self, *, workspace_id: str, period_id: str) -> PayPeriodRow | None:
        """Return the period or ``None`` when invisible to the caller."""
        ...

    def replace_period_entries(
        self,
        *,
        workspace_id: str,
        pay_period_id: str,
        starts_at: datetime,
        ends_at: datetime,
        now: datetime,
    ) -> Sequence[PayPeriodEntryRow]:
        """Recompute booking-derived period entries."""
        ...

    def list_period_entries(
        self,
        *,
        workspace_id: str,
        pay_period_id: str,
    ) -> Sequence[PayPeriodEntryRow]:
        """Return period entries ordered deterministically."""
        ...

    def list_pay_bearing_bookings(
        self,
        *,
        workspace_id: str,
        starts_at: datetime,
        ends_at: datetime,
        user_id: str | None = None,
        work_engagement_id: str | None = None,
    ) -> Sequence[BookingPayRow]:
        """Return settled pay-bearing bookings intersecting the period."""
        ...

    def get_effective_pay_rule(
        self,
        *,
        workspace_id: str,
        user_id: str,
        at: datetime,
    ) -> PayRuleRow | None:
        """Return the rule active at ``at`` using §09 precedence."""
        ...

    def list_holiday_multipliers(
        self,
        *,
        workspace_id: str,
        starts_on: date,
        ends_before: date,
        countries: Set[str],
    ) -> Mapping[tuple[date, str | None], Decimal]:
        """Return workspace-wide and country-specific holiday multipliers."""
        ...

    def has_paid_payslip(self, *, workspace_id: str, period_id: str) -> bool:
        """Return whether any contained payslip is already paid."""
        ...

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
        net_cents: int,
        components_json: dict[str, object],
        now: datetime,
    ) -> PayslipRow:
        """Insert or update the draft payslip for ``(period, user)``."""
        ...


class PayPeriodRepository(Protocol):
    """Read + write seam for ``pay_period`` rows and payslip guards."""

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session for audit writes."""
        ...

    def get(self, *, workspace_id: str, period_id: str) -> PayPeriodRow | None:
        """Return the period or ``None`` when invisible to the caller."""
        ...

    def list(self, *, workspace_id: str) -> Sequence[PayPeriodRow]:
        """Return workspace periods newest first."""
        ...

    def has_overlap(
        self,
        *,
        workspace_id: str,
        starts_at: datetime,
        ends_at: datetime,
        exclude_period_id: str | None = None,
    ) -> bool:
        """Return whether another period intersects ``[starts_at, ends_at)``."""
        ...

    def insert(
        self,
        *,
        period_id: str,
        workspace_id: str,
        starts_at: datetime,
        ends_at: datetime,
        now: datetime,
    ) -> PayPeriodRow:
        """Insert an ``open`` period and return its projection."""
        ...

    def lock(
        self,
        *,
        workspace_id: str,
        period_id: str,
        locked_at: datetime,
        locked_by: str | None,
    ) -> PayPeriodRow:
        """Flip ``open`` to ``locked`` and stamp lock metadata."""
        ...

    def update(
        self,
        *,
        workspace_id: str,
        period_id: str,
        starts_at: datetime,
        ends_at: datetime,
    ) -> PayPeriodRow:
        """Update the window for an ``open`` period."""
        ...

    def reopen(self, *, workspace_id: str, period_id: str) -> PayPeriodRow:
        """Flip ``locked`` to ``open`` and reset contained payslips to draft."""
        ...

    def mark_paid(self, *, workspace_id: str, period_id: str) -> PayPeriodRow:
        """Flip ``locked`` to ``paid``."""
        ...

    def delete(self, *, workspace_id: str, period_id: str) -> None:
        """Delete an ``open`` period."""
        ...

    def has_paid_payslip(self, *, workspace_id: str, period_id: str) -> bool:
        """Return whether any contained payslip is already paid."""
        ...

    def has_unpaid_payslip(self, *, workspace_id: str, period_id: str) -> bool:
        """Return whether any contained payslip is not fully paid."""
        ...

    def list_unsettled_booking_ids(
        self,
        *,
        workspace_id: str,
        starts_at: datetime,
        ends_at: datetime,
        limit: int,
    ) -> Sequence[str]:
        """Return unsettled booking ids that block locking this period."""
        ...


class BookingPayRepository(Protocol):
    """Read + write seam for booking-derived payroll ledger entries."""

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session."""
        ...

    def list_pay_bearing_bookings(
        self,
        *,
        workspace_id: str,
        starts_at: datetime,
        ends_at: datetime,
        user_id: str | None = None,
        work_engagement_id: str | None = None,
    ) -> Sequence[BookingPayRow]:
        """Return settled pay-bearing bookings intersecting the period."""
        ...

    def list_unsettled_booking_ids(
        self,
        *,
        workspace_id: str,
        starts_at: datetime,
        ends_at: datetime,
        limit: int,
    ) -> Sequence[str]:
        """Return scheduled/pending/pending-amend bookings in the period."""
        ...

    def replace_period_entries(
        self,
        *,
        workspace_id: str,
        pay_period_id: str,
        starts_at: datetime,
        ends_at: datetime,
        now: datetime,
    ) -> Sequence[PayPeriodEntryRow]:
        """Recompute daily pay-period entries from settled bookings."""
        ...


# ---------------------------------------------------------------------------
# PayRuleRepository
# ---------------------------------------------------------------------------


class PayRuleRepository(Protocol):
    """Read + write seam for ``pay_rule`` rows + the locked-period guard.

    The repo carries an open SQLAlchemy ``Session`` so domain callers
    that also need :func:`app.audit.write_audit` (which still takes a
    concrete ``Session`` today) can thread the same UoW without
    holding a second seam. The accessor drops once the audit writer
    gains its own Protocol port.

    Every method honours the workspace-scoping invariant: the SA
    concretion always pins reads + writes to the ``workspace_id``
    passed by the caller, mirroring the ORM tenant filter as
    defence-in-depth (a misconfigured filter must fail loud).

    The repo never commits outside what the underlying statements
    require — the caller's UoW owns the transaction boundary (§01
    "Key runtime invariants" #3). Methods that mutate state flush so
    the caller's next read (and the audit writer's FK reference to
    ``entity_id``) sees the new row.
    """

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session.

        Exposed for callers that need to thread the same UoW through
        :func:`app.audit.write_audit` (which still takes a concrete
        ``Session`` today). Drops when the audit writer gains its
        own Protocol port.
        """
        ...

    # -- Reads -----------------------------------------------------------

    def get(
        self,
        *,
        workspace_id: str,
        rule_id: str,
    ) -> PayRuleRow | None:
        """Return the row or ``None`` when invisible to the caller.

        Defence-in-depth pins the lookup to ``workspace_id`` even
        though the ORM tenant filter already narrows the read; a
        misconfigured filter must fail loud, not silently. There is
        no ``include_deleted`` flag — pay rules use the
        ``effective_to`` column as a soft-retire signal rather than
        a separate ``deleted_at`` (a row whose ``effective_to`` is
        in the past is still labour-law evidence and must remain
        readable).
        """
        ...

    def list_for_user(
        self,
        *,
        workspace_id: str,
        user_id: str,
        limit: int,
        after_cursor: str | None = None,
    ) -> Sequence[PayRuleRow]:
        """Return up to ``limit + 1`` rows for ``(workspace, user)``.

        Ordered ``effective_from DESC, id DESC`` so the newest rule
        for the user surfaces first — matches the §09 "Pay-rule
        selection" precedence (greatest ``effective_from`` wins).

        ``after_cursor`` is the opaque-cursor handle the
        :func:`~app.api.pagination.paginate` helper round-trips. The
        cursor is **composite** — formatted as
        ``"<effective_from-isoformat>|<id>"`` — because ``effective_from``
        is workspace-author-controlled (a manager may backdate or
        future-date a rule), so ``effective_from`` need not align
        with ULID order. A ULID-only cursor would skip or repeat
        rows whenever a backdated rule has a higher id than an
        earlier rule with a later ``effective_from``. The composite
        cursor walks the desc page deterministically:
        ``(effective_from, id) < (cursor_effective_from, cursor_id)``.
        """
        ...

    def has_paid_payslip_overlap(
        self,
        *,
        workspace_id: str,
        user_id: str,
        effective_from: datetime,
        effective_to: datetime | None,
    ) -> bool:
        """Return ``True`` iff the rule's window overlaps a paid payslip.

        The §09 §"Labour-law compliance" + §15 §"Right to erasure"
        rules pin a pay-rule once it has been consumed by a payslip
        in a paid pay_period — editing or hard-deleting it would
        retro-corrupt payroll evidence.

        The check is structurally:

        * any ``payslip`` whose ``user_id`` matches the rule's,
          whose parent ``pay_period`` is in ``state = 'paid'``, and
          whose ``(starts_at, ends_at)`` window overlaps the rule's
          ``[effective_from, effective_to]`` window — counts as a
          "consumed" rule.

        Window-overlap semantics: two windows overlap iff
        ``effective_from <= period.ends_at`` AND
        ``(effective_to IS NULL OR effective_to >= period.starts_at)``.
        ``effective_to=None`` means "open-ended" — the rule still
        applies, so the second clause collapses to ``TRUE``.

        v1's :class:`Payslip` does not yet carry its own
        per-payslip state column; ``pay_period.state == 'paid'`` is
        the canonical "every payslip in the period was marked paid"
        signal (cd-a3w transition flips the period state once every
        contained payslip is paid). When the per-payslip
        ``status`` enum lands (cd-* TBD) this check upgrades to
        ``payslip.status = 'paid'`` without changing the seam shape.
        """
        ...

    # -- Writes ----------------------------------------------------------

    def insert(
        self,
        *,
        rule_id: str,
        workspace_id: str,
        user_id: str,
        currency: str,
        base_cents_per_hour: int,
        overtime_multiplier: Decimal,
        night_multiplier: Decimal,
        weekend_multiplier: Decimal,
        effective_from: datetime,
        effective_to: datetime | None,
        created_by: str | None,
        now: datetime,
    ) -> PayRuleRow:
        """Insert a new ``pay_rule`` row and return its projection.

        Flushes so the caller's next read (and the audit writer's
        FK reference to ``entity_id``) sees the new row. The DB
        CHECKs (currency length, non-negative cents, multipliers
        >= 1) are belt-and-braces — the domain layer validates
        the same predicates *plus* the upper-bound multiplier cap
        and the ISO-4217 allow-list before reaching here, so a
        flush-time violation is a programming error worth a stack
        trace rather than a typed exception.
        """
        ...

    def update(
        self,
        *,
        workspace_id: str,
        rule_id: str,
        currency: str,
        base_cents_per_hour: int,
        overtime_multiplier: Decimal,
        night_multiplier: Decimal,
        weekend_multiplier: Decimal,
        effective_from: datetime,
        effective_to: datetime | None,
    ) -> PayRuleRow:
        """Apply a full-replacement update and return the refreshed projection.

        v1 treats the mutable surface as a complete replacement —
        the spec does not (yet) call for per-field PATCH on pay
        rules and a partial update would let a caller silently
        widen the effective window without re-asserting consent.
        Stamps no ``updated_at`` because the column is not yet on
        the v1 schema; the audit row + the row's ``created_at``
        are the canonical timestamps.

        Caller has already confirmed the row exists (via :meth:`get`)
        and that the locked-period guard does not fire.
        """
        ...

    def soft_delete(
        self,
        *,
        workspace_id: str,
        rule_id: str,
        now: datetime,
    ) -> PayRuleRow:
        """Stamp ``effective_to = now`` and return the refreshed projection.

        Pay rules are never hard-deleted: the row is payroll-law
        evidence (§09 §"Labour-law compliance"). "Delete" here is a
        soft-retire — set ``effective_to`` so the rule no longer
        applies to future periods but historical payslips still
        link to a live row. If the row was already retired
        (``effective_to`` is set and in the past), this becomes a
        no-op write that still reports the (unchanged) projection
        back to the caller; the service is the gate that decides
        whether the operation is meaningful.
        """
        ...
