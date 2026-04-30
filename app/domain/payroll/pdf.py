"""Payslip PDF rendering and storage."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from babel import Locale, UnknownLocaleError
from babel.dates import format_date, format_datetime
from babel.numbers import format_currency, format_decimal
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from app.adapters.storage.ports import Storage
from app.audit import write_audit
from app.domain.payroll.ports import PayslipPdfRepository, PayslipPdfRow
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock

__all__ = [
    "PayslipPdfNotFound",
    "PayslipPdfRendered",
    "PayslipPdfRenderer",
    "WeasyPrintPayslipPdfRenderer",
    "render_payslip",
]


_TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "adapters" / "pdf" / "templates"
_PDF_CONTENT_TYPE = "application/pdf"

_COMPONENT_LABELS: Mapping[str, str] = {
    "base_pay": "Base pay",
    "overtime_150": "Overtime 150%",
    "holiday_bonus": "Holiday bonus",
    "piecework": "Piecework credits",
    "task_credit": "Task credits",
    "adjustment": "Adjustment",
}


class PayslipPdfNotFound(Exception):
    """Raised when the requested payslip is absent from the workspace."""


@dataclass(frozen=True, slots=True)
class PayslipPdfRendered:
    """Result of ensuring a payslip PDF exists in storage."""

    payslip_id: str
    content_hash: str
    rendered: bool


@dataclass(frozen=True, slots=True)
class PdfLine:
    """Rendered line item for the PDF template."""

    label: str
    amount: str
    detail: str


@dataclass(frozen=True, slots=True)
class PayslipPdfDocument:
    """Template-ready payslip document view."""

    title: str
    workspace_name: str
    workspace_registered_name: str
    workspace_address: str
    worker_name: str
    worker_email: str
    period_label: str
    status: str
    issued_label: str
    paid_label: str
    gross_lines: Sequence[PdfLine]
    statutory_lines: Sequence[PdfLine]
    deduction_lines: Sequence[PdfLine]
    payout_lines: Sequence[str]
    regular_hours: str
    overtime_hours: str
    gross_total: str
    net_total: str
    locale: str
    jurisdiction: str
    currency: str


class PayslipPdfRenderer(Protocol):
    """Port for rendering a payslip projection into PDF bytes."""

    def render(self, row: PayslipPdfRow) -> bytes:
        """Return the rendered PDF payload."""
        ...


@dataclass(frozen=True, slots=True)
class WeasyPrintPayslipPdfRenderer:
    """Jinja + WeasyPrint renderer for the base v1 payslip template."""

    env: Environment

    @classmethod
    def default(cls) -> WeasyPrintPayslipPdfRenderer:
        env = Environment(
            loader=FileSystemLoader(str(_TEMPLATE_ROOT)),
            autoescape=select_autoescape(["html"]),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        return cls(env=env)

    def render(self, row: PayslipPdfRow) -> bytes:
        from weasyprint import HTML

        template = self.env.get_template("payslip_base.html")
        html = template.render(document=_document_from_row(row))
        pdf = HTML(string=html, base_url=str(_TEMPLATE_ROOT)).write_pdf()
        if not isinstance(pdf, bytes):
            raise RuntimeError("weasyprint did not return PDF bytes")
        return pdf


def render_payslip(
    repo: PayslipPdfRepository,
    ctx: WorkspaceContext,
    storage: Storage,
    *,
    payslip_id: str,
    force: bool = False,
    renderer: PayslipPdfRenderer | None = None,
    clock: Clock | None = None,
) -> PayslipPdfRendered:
    """Render and store a payslip PDF unless an existing hash is reusable."""

    row = repo.get_payslip_pdf_row(
        workspace_id=ctx.workspace_id,
        payslip_id=payslip_id,
    )
    if row is None:
        raise PayslipPdfNotFound(payslip_id)

    if row.pdf_blob_hash is not None and not force:
        return PayslipPdfRendered(
            payslip_id=payslip_id,
            content_hash=row.pdf_blob_hash,
            rendered=False,
        )

    active_renderer = renderer or WeasyPrintPayslipPdfRenderer.default()
    pdf = active_renderer.render(row)
    content_hash = hashlib.sha256(pdf).hexdigest()
    stored = storage.put(content_hash, io.BytesIO(pdf), content_type=_PDF_CONTENT_TYPE)
    if stored.content_hash != content_hash:
        raise RuntimeError("storage returned a mismatched content hash")
    previous_hash = row.pdf_blob_hash
    repo.set_payslip_pdf_blob_hash(
        workspace_id=ctx.workspace_id,
        payslip_id=payslip_id,
        pdf_blob_hash=stored.content_hash,
    )
    active_clock = clock or SystemClock()
    write_audit(
        repo.session,
        ctx,
        entity_kind="payslip",
        entity_id=payslip_id,
        action="payslip.pdf_rendered",
        diff={
            "before": {"pdf_blob_hash": previous_hash},
            "after": {"pdf_blob_hash": stored.content_hash},
            "force": force,
        },
        clock=active_clock,
    )
    return PayslipPdfRendered(
        payslip_id=payslip_id,
        content_hash=stored.content_hash,
        rendered=True,
    )


def _document_from_row(row: PayslipPdfRow) -> PayslipPdfDocument:
    locale = _normalise_locale(row.locale)
    return PayslipPdfDocument(
        title=f"Payslip {row.id}",
        workspace_name=row.workspace_name,
        workspace_registered_name=_workspace_setting(
            row.workspace_settings,
            keys=(
                "payroll.payslip.registered_name",
                "payroll.registered_name",
                "workspace.registered_name",
            ),
            fallback=row.workspace_name,
        ),
        workspace_address=_workspace_setting(
            row.workspace_settings,
            keys=("payroll.payslip.address", "payroll.address", "workspace.address"),
            fallback="",
        ),
        worker_name=row.worker_name,
        worker_email=row.worker_email,
        period_label=_format_period(
            starts_at=row.period_starts_at,
            ends_at=row.period_ends_at,
            locale=locale,
        ),
        status=row.status.title(),
        issued_label=_format_optional_datetime(row.issued_at, locale=locale),
        paid_label=_format_optional_datetime(row.paid_at, locale=locale),
        gross_lines=_component_lines(
            row.components_json.get("gross_breakdown"),
            locale=locale,
            currency=row.currency,
        ),
        statutory_lines=_component_lines(
            row.components_json.get("statutory"),
            locale=locale,
            currency=row.currency,
        ),
        deduction_lines=_deduction_lines(
            row,
            locale=locale,
            currency=row.currency,
        ),
        payout_lines=_payout_lines(row.payout_snapshot_json),
        regular_hours=format_decimal(row.shift_hours_decimal, locale=locale),
        overtime_hours=format_decimal(row.overtime_hours_decimal, locale=locale),
        gross_total=_format_cents(row.gross_cents, row.currency, locale=locale),
        net_total=_format_cents(row.net_cents, row.currency, locale=locale),
        locale=locale,
        jurisdiction=row.jurisdiction,
        currency=row.currency,
    )


def _normalise_locale(locale: str) -> str:
    candidate = locale or "en-US"
    try:
        parsed = Locale.parse(candidate, sep="-")
    except UnknownLocaleError, ValueError:
        return "en_US"
    return str(parsed)


def _workspace_setting(
    settings: Mapping[str, object],
    *,
    keys: Sequence[str],
    fallback: str,
) -> str:
    for key in keys:
        value = settings.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _format_period(*, starts_at: datetime, ends_at: datetime, locale: str) -> str:
    starts_on = starts_at.date()
    ends_on = _inclusive_end_date(ends_at)
    if starts_on == ends_on:
        return format_date(starts_on, format="long", locale=locale)
    return (
        f"{format_date(starts_on, format='long', locale=locale)} - "
        f"{format_date(ends_on, format='long', locale=locale)}"
    )


def _inclusive_end_date(ends_at: datetime) -> date:
    if ends_at.hour == 0 and ends_at.minute == 0 and ends_at.second == 0:
        return date.fromordinal(ends_at.date().toordinal() - 1)
    return ends_at.date()


def _format_optional_datetime(value: datetime | None, *, locale: str) -> str:
    if value is None:
        return "Not recorded"
    return format_datetime(value, format="medium", locale=locale)


def _component_lines(
    raw_lines: object,
    *,
    locale: str,
    currency: str,
) -> list[PdfLine]:
    if not isinstance(raw_lines, list):
        return []
    lines: list[PdfLine] = []
    for raw in raw_lines:
        if not isinstance(raw, dict):
            continue
        key = raw.get("key")
        cents = raw.get("cents")
        if not isinstance(key, str) or not isinstance(cents, int):
            continue
        lines.append(
            PdfLine(
                label=_COMPONENT_LABELS.get(key, key.replace("_", " ").title()),
                amount=_format_cents(cents, currency, locale=locale),
                detail=_line_detail(raw, locale=locale, currency=currency),
            )
        )
    return lines


def _deduction_lines(
    row: PayslipPdfRow,
    *,
    locale: str,
    currency: str,
) -> list[PdfLine]:
    lines = _component_lines(
        row.components_json.get("deductions"),
        locale=locale,
        currency=currency,
    )
    if lines:
        return lines
    return [
        PdfLine(
            label=key.replace("_", " ").title(),
            amount=_format_cents(cents, currency, locale=locale),
            detail="",
        )
        for key, cents in sorted(row.deductions_cents.items())
    ]


def _line_detail(
    raw: Mapping[object, object],
    *,
    locale: str,
    currency: str,
) -> str:
    details: list[str] = []
    reason = raw.get("reason")
    if isinstance(reason, str) and reason.strip():
        details.append(reason.strip())
    rate = raw.get("rate")
    if isinstance(rate, int | float | Decimal):
        details.append(f"Rate {format_decimal(rate, locale=locale)}")
    base_cents = raw.get("base_cents")
    if isinstance(base_cents, int):
        details.append(f"Base {_format_cents(base_cents, currency, locale=locale)}")
    return "; ".join(details)


def _payout_lines(snapshot: Mapping[str, object] | None) -> list[str]:
    if snapshot is None:
        return ["Payout destination not snapshotted"]
    raw_destinations = snapshot.get("destinations")
    if not isinstance(raw_destinations, list) or not raw_destinations:
        return ["Payout arranged manually"]
    lines: list[str] = []
    for raw in raw_destinations:
        if not isinstance(raw, dict):
            continue
        label = _first_string(raw, ("label", "kind")) or "Payout"
        stub = _first_string(raw, ("display_stub",))
        if stub is None:
            lines.append(label)
        else:
            lines.append(f"{label}: {stub}")
    return lines or ["Payout arranged manually"]


def _first_string(raw: Mapping[object, object], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _format_cents(cents: int, currency: str, *, locale: str) -> str:
    amount = Decimal(cents) / Decimal(100)
    return format_currency(amount, currency, locale=locale)
