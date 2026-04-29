"""CSV exports for payroll/accounting reports."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from typing import Literal

from app.audit import write_audit
from app.domain.payroll.ports import (
    ExpenseLedgerExportRow,
    PayrollExportRepository,
    PayslipExportRow,
    TimesheetExportRow,
)
from app.tenancy import WorkspaceContext

ExportKind = Literal["timesheets", "payslips", "expense-ledger"]

TIMESHEET_HEADER: tuple[str, ...] = (
    "shift_id",
    "user_email",
    "property_label",
    "starts_at_utc",
    "ends_at_utc",
    "hours_decimal",
    "source",
    "notes",
)
PAYSLIP_HEADER: tuple[str, ...] = (
    "payslip_id",
    "user_email",
    "period_starts_at",
    "period_ends_at",
    "hours",
    "overtime_hours",
    "gross_cents",
    "deductions_cents",
    "net_cents",
    "currency",
    "paid_at",
)
EXPENSE_LEDGER_HEADER: tuple[str, ...] = (
    "expense_id",
    "claimant_email",
    "vendor",
    "spent_at",
    "category",
    "amount_cents",
    "currency",
    "property_label",
    "decided_at",
    "reimbursed_via",
)


@dataclass(frozen=True, slots=True)
class CsvExport:
    """Streaming CSV export plus audit metadata."""

    kind: ExportKind
    filename: str
    since: datetime
    until: datetime
    header: Sequence[str]
    rows: Iterable[Sequence[str]]


class ExportWindowInvalid(ValueError):
    """The export date window is missing or inverted."""


class PayPeriodNotFound(LookupError):
    """The requested pay period is absent from the workspace."""


class ExpenseStatusInvalid(ValueError):
    """The expense ledger status filter is not a known claim state."""


def export_timesheets_csv(
    repo: PayrollExportRepository,
    ctx: WorkspaceContext,
    *,
    since: datetime,
    until: datetime,
) -> CsvExport:
    _require_window(since=since, until=until)
    rows = repo.iter_timesheets(
        workspace_id=ctx.workspace_id,
        since=since,
        until=until,
    )
    return CsvExport(
        kind="timesheets",
        filename="timesheets.csv",
        since=since,
        until=until,
        header=TIMESHEET_HEADER,
        rows=(_timesheet_values(row) for row in rows),
    )


def export_payslips_csv(
    repo: PayrollExportRepository,
    ctx: WorkspaceContext,
    *,
    period_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> CsvExport:
    if period_id is not None:
        window = repo.pay_period_window(
            workspace_id=ctx.workspace_id, period_id=period_id
        )
        if window is None:
            raise PayPeriodNotFound(period_id)
        since, until = window
        filename = f"payslips-{period_id}.csv"
    else:
        if since is None or until is None:
            raise ExportWindowInvalid(
                "payslip exports require period_id or since/until"
            )
        filename = "payslips.csv"
    _require_window(since=since, until=until)
    rows = repo.iter_payslips(
        workspace_id=ctx.workspace_id,
        since=since,
        until=until,
        period_id=period_id,
    )
    return CsvExport(
        kind="payslips",
        filename=filename,
        since=since,
        until=until,
        header=PAYSLIP_HEADER,
        rows=(_payslip_values(row) for row in rows),
    )


def export_expense_ledger_csv(
    repo: PayrollExportRepository,
    ctx: WorkspaceContext,
    *,
    since: datetime,
    until: datetime,
    status_filter: str = "approved",
) -> CsvExport:
    _require_window(since=since, until=until)
    _require_expense_status(status_filter)
    rows = repo.iter_expense_ledger(
        workspace_id=ctx.workspace_id,
        since=since,
        until=until,
        status_filter=status_filter,
    )
    return CsvExport(
        kind="expense-ledger",
        filename="expense-ledger.csv",
        since=since,
        until=until,
        header=EXPENSE_LEDGER_HEADER,
        rows=(_expense_values(row) for row in rows),
    )


def stream_csv_with_audit(
    export: CsvExport,
    repo: PayrollExportRepository,
    ctx: WorkspaceContext,
    *,
    include_bom: bool = False,
) -> Iterator[str]:
    row_count = 0
    if include_bom:
        yield "\ufeff"
    yield _csv_line(export.header)
    for row in export.rows:
        row_count += 1
        yield _csv_line(row)
    write_audit(
        repo.session,
        ctx,
        entity_kind="payroll_export",
        entity_id=export.kind,
        action="payroll.exported",
        diff={
            "kind": export.kind,
            "since": _format_dt(export.since),
            "until": _format_dt(export.until),
            "row_count": row_count,
        },
        via="api",
    )


def _require_window(*, since: datetime, until: datetime) -> None:
    if until <= since:
        raise ExportWindowInvalid("until must be after since")


def _require_expense_status(value: str) -> None:
    if value in {"all", "draft", "submitted", "approved", "rejected", "reimbursed"}:
        return
    raise ExpenseStatusInvalid(f"unknown expense status filter: {value}")


def _csv_line(values: Sequence[str]) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(values)
    return buffer.getvalue()


def _format_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _format_decimal(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def _timesheet_values(row: TimesheetExportRow) -> tuple[str, ...]:
    return (
        row.shift_id,
        row.user_email,
        row.property_label,
        _format_dt(row.starts_at_utc),
        _format_dt(row.ends_at_utc),
        _format_decimal(row.hours_decimal),
        row.source,
        row.notes,
    )


def _payslip_values(row: PayslipExportRow) -> tuple[str, ...]:
    return (
        row.payslip_id,
        row.user_email,
        _format_dt(row.period_starts_at),
        _format_dt(row.period_ends_at),
        _format_decimal(row.hours),
        _format_decimal(row.overtime_hours),
        str(row.gross_cents),
        str(row.deductions_cents),
        str(row.net_cents),
        row.currency,
        _format_dt(row.paid_at),
    )


def _expense_values(row: ExpenseLedgerExportRow) -> tuple[str, ...]:
    return (
        row.expense_id,
        row.claimant_email,
        row.vendor,
        _format_dt(row.spent_at),
        row.category,
        str(row.amount_cents),
        row.currency,
        row.property_label,
        _format_dt(row.decided_at),
        row.reimbursed_via,
    )
