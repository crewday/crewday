"""Billing vendor-invoice service."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from app.audit import write_audit
from app.events import EventBus, VendorInvoicePaid
from app.events import bus as default_bus
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "VendorInvoiceCreate",
    "VendorInvoiceInvalid",
    "VendorInvoiceMarkPaid",
    "VendorInvoiceNotFound",
    "VendorInvoiceOrganizationRow",
    "VendorInvoiceRepository",
    "VendorInvoiceRow",
    "VendorInvoiceService",
    "VendorInvoiceView",
]

_STATUS_VALUES = frozenset({"received", "approved", "paid", "disputed"})
_BLOB_HASH_LEN = 64


class VendorInvoiceInvalid(ValueError):
    """The requested vendor-invoice mutation violates the billing contract."""


class VendorInvoiceNotFound(LookupError):
    """The vendor invoice or one of its scoped parents does not exist."""


@dataclass(frozen=True, slots=True)
class VendorInvoiceOrganizationRow:
    id: str
    workspace_id: str
    kind: str
    default_currency: str


@dataclass(frozen=True, slots=True)
class VendorInvoiceRow:
    id: str
    workspace_id: str
    vendor_org_id: str
    invoice_number: str
    issued_at: date
    due_at: date | None
    total_cents: int
    currency: str
    status: str
    pdf_blob_hash: str | None
    approved_at: datetime | None
    paid_at: datetime | None
    payment_method: str | None
    proof_blob_hash: str | None
    proof_of_payment_file_ids: tuple[str, ...]
    disputed_at: datetime | None
    notes_md: str | None


@dataclass(frozen=True, slots=True)
class VendorInvoiceView:
    id: str
    workspace_id: str
    vendor_org_id: str
    invoice_number: str
    issued_at: date
    due_at: date | None
    total_cents: int
    currency: str
    status: str
    pdf_blob_hash: str | None
    approved_at: datetime | None
    paid_at: datetime | None
    payment_method: str | None
    proof_blob_hash: str | None
    proof_of_payment_file_ids: tuple[str, ...]
    disputed_at: datetime | None
    notes_md: str | None
    reminder_windows: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class VendorInvoiceCreate:
    vendor_org_id: str
    invoice_number: str
    issued_at: date
    due_at: date | None
    total_cents: int
    currency: str
    notes_md: str | None = None


@dataclass(frozen=True, slots=True)
class VendorInvoiceMarkPaid:
    paid_at: datetime
    payment_method: str
    proof_blob_hash: str | None = None


class VendorInvoiceRepository(Protocol):
    @property
    def session(self) -> Session: ...

    def get_organization(
        self, *, workspace_id: str, organization_id: str, for_update: bool = False
    ) -> VendorInvoiceOrganizationRow | None: ...

    def insert(
        self,
        *,
        invoice_id: str,
        workspace_id: str,
        vendor_org_id: str,
        invoice_number: str,
        issued_at: date,
        due_at: date | None,
        total_cents: int,
        currency: str,
        status: str,
        notes_md: str | None,
    ) -> VendorInvoiceRow: ...

    def get(
        self, *, workspace_id: str, invoice_id: str, for_update: bool = False
    ) -> VendorInvoiceRow | None: ...

    def list(
        self,
        *,
        workspace_id: str,
        vendor_org_id: str | None,
        status: str | None,
    ) -> Sequence[VendorInvoiceRow]: ...

    def update_fields(
        self,
        *,
        workspace_id: str,
        invoice_id: str,
        fields: Mapping[str, object | None],
    ) -> VendorInvoiceRow: ...


class VendorInvoiceService:
    """Workspace-scoped vendor-invoice use cases."""

    def __init__(
        self,
        ctx: WorkspaceContext,
        *,
        clock: Clock | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._ctx = ctx
        self._clock = clock if clock is not None else SystemClock()
        self._bus = event_bus if event_bus is not None else default_bus

    def create(
        self, repo: VendorInvoiceRepository, body: VendorInvoiceCreate
    ) -> VendorInvoiceView:
        vendor = self._get_vendor_organization(
            repo, body.vendor_org_id, for_update=True
        )
        invoice_number = _clean_required(body.invoice_number, field="invoice_number")
        currency = _clean_currency(body.currency)
        if body.total_cents < 0:
            raise VendorInvoiceInvalid("total_cents must be non-negative")
        if body.due_at is not None and body.due_at < body.issued_at:
            raise VendorInvoiceInvalid("due_at must be on or after issued_at")
        row = repo.insert(
            invoice_id=new_ulid(),
            workspace_id=self._ctx.workspace_id,
            vendor_org_id=vendor.id,
            invoice_number=invoice_number,
            issued_at=body.issued_at,
            due_at=body.due_at,
            total_cents=body.total_cents,
            currency=currency,
            status="received",
            notes_md=_clean_optional(body.notes_md),
        )
        view = _to_view(row)
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="vendor_invoice",
            entity_id=view.id,
            action="billing.vendor_invoice.created",
            diff={"after": _audit_shape(view)},
            clock=self._clock,
        )
        return view

    def list(
        self,
        repo: VendorInvoiceRepository,
        *,
        vendor_org_id: str | None = None,
        status: str | None = None,
    ) -> list[VendorInvoiceView]:
        clean_status = _validate_status(status) if status is not None else None
        rows = repo.list(
            workspace_id=self._ctx.workspace_id,
            vendor_org_id=_clean_optional(vendor_org_id),
            status=clean_status,
        )
        return [_to_view(row) for row in rows]

    def get(self, repo: VendorInvoiceRepository, invoice_id: str) -> VendorInvoiceView:
        return _to_view(self._get(repo, invoice_id))

    def attach_pdf(
        self, repo: VendorInvoiceRepository, invoice_id: str, blob_hash: str
    ) -> VendorInvoiceView:
        current = self._get(repo, invoice_id, for_update=True)
        if current.status == "paid":
            raise VendorInvoiceInvalid("paid invoices are locked")
        clean_hash = _clean_blob_hash(blob_hash, field="pdf_blob_hash")
        if current.pdf_blob_hash == clean_hash:
            return _to_view(current)
        updated = repo.update_fields(
            workspace_id=self._ctx.workspace_id,
            invoice_id=current.id,
            fields={"pdf_blob_hash": clean_hash},
        )
        self._audit_change(
            repo, current, updated, "billing.vendor_invoice.pdf_attached"
        )
        return _to_view(updated)

    def approve(
        self, repo: VendorInvoiceRepository, invoice_id: str
    ) -> VendorInvoiceView:
        current = self._get(repo, invoice_id, for_update=True)
        if current.status == "approved":
            return _to_view(current)
        if current.pdf_blob_hash is None:
            raise VendorInvoiceInvalid("cannot approve an invoice without a PDF")
        if current.status != "received":
            raise VendorInvoiceInvalid(
                "cannot transition vendor invoice from "
                f"{current.status!r} to 'approved'"
            )
        updated = repo.update_fields(
            workspace_id=self._ctx.workspace_id,
            invoice_id=current.id,
            fields={"status": "approved", "approved_at": self._clock.now()},
        )
        self._audit_state(
            repo, current, updated, action="billing.vendor_invoice.approved"
        )
        return _to_view(updated)

    def mark_paid(
        self,
        repo: VendorInvoiceRepository,
        invoice_id: str,
        body: VendorInvoiceMarkPaid,
    ) -> VendorInvoiceView:
        current = self._get(repo, invoice_id, for_update=True)
        if current.status == "paid":
            return _to_view(current)
        if current.status != "approved":
            raise VendorInvoiceInvalid(
                f"cannot transition vendor invoice from {current.status!r} to 'paid'"
            )
        payment_method = _clean_required(body.payment_method, field="payment_method")
        paid_at = _as_utc(body.paid_at)
        proof_blob_hash = (
            _clean_blob_hash(body.proof_blob_hash, field="proof_blob_hash")
            if body.proof_blob_hash is not None
            else None
        )
        updated = repo.update_fields(
            workspace_id=self._ctx.workspace_id,
            invoice_id=current.id,
            fields={
                "status": "paid",
                "paid_at": paid_at,
                "payment_method": payment_method,
                "proof_blob_hash": proof_blob_hash,
            },
        )
        view = _to_view(updated)
        self._audit_state(repo, current, updated, action="billing.vendor_invoice.paid")
        self._bus.publish(
            VendorInvoicePaid(
                workspace_id=self._ctx.workspace_id,
                actor_id=self._ctx.actor_id,
                correlation_id=self._ctx.audit_correlation_id,
                occurred_at=self._clock.now(),
                vendor_invoice_id=view.id,
                vendor_org_id=view.vendor_org_id,
                total_cents=view.total_cents,
                currency=view.currency,
                paid_at=paid_at,
                payment_method=view.payment_method or payment_method,
            )
        )
        return view

    def upload_proof(
        self, repo: VendorInvoiceRepository, invoice_id: str, blob_hash: str
    ) -> VendorInvoiceView:
        current = self._get(repo, invoice_id, for_update=True)
        if current.status != "approved":
            raise VendorInvoiceInvalid(
                "proof can only be uploaded for approved vendor invoices"
            )
        clean_hash = _clean_blob_hash(blob_hash, field="proof_of_payment_file_ids")
        if clean_hash in current.proof_of_payment_file_ids:
            return _to_view(current)
        updated = repo.update_fields(
            workspace_id=self._ctx.workspace_id,
            invoice_id=current.id,
            fields={
                "proof_of_payment_file_ids": [
                    *current.proof_of_payment_file_ids,
                    clean_hash,
                ]
            },
        )
        self._audit_change(
            repo, current, updated, "billing.vendor_invoice.proof_uploaded"
        )
        return _to_view(updated)

    def dispute(
        self, repo: VendorInvoiceRepository, invoice_id: str
    ) -> VendorInvoiceView:
        current = self._get(repo, invoice_id, for_update=True)
        if current.status == "disputed":
            return _to_view(current)
        if current.status not in {"received", "approved"}:
            raise VendorInvoiceInvalid(
                "cannot transition vendor invoice from "
                f"{current.status!r} to 'disputed'"
            )
        updated = repo.update_fields(
            workspace_id=self._ctx.workspace_id,
            invoice_id=current.id,
            fields={"status": "disputed", "disputed_at": self._clock.now()},
        )
        self._audit_state(
            repo, current, updated, action="billing.vendor_invoice.disputed"
        )
        return _to_view(updated)

    def _get(
        self,
        repo: VendorInvoiceRepository,
        invoice_id: str,
        *,
        for_update: bool = False,
    ) -> VendorInvoiceRow:
        clean_id = _clean_required(invoice_id, field="invoice_id")
        row = repo.get(
            workspace_id=self._ctx.workspace_id,
            invoice_id=clean_id,
            for_update=for_update,
        )
        if row is None:
            raise VendorInvoiceNotFound("vendor invoice not found")
        return row

    def _get_vendor_organization(
        self,
        repo: VendorInvoiceRepository,
        organization_id: str,
        *,
        for_update: bool = False,
    ) -> VendorInvoiceOrganizationRow:
        clean_id = _clean_required(organization_id, field="vendor_org_id")
        row = repo.get_organization(
            workspace_id=self._ctx.workspace_id,
            organization_id=clean_id,
            for_update=for_update,
        )
        if row is None:
            raise VendorInvoiceNotFound("vendor organization not found")
        if row.kind == "client":
            raise VendorInvoiceInvalid("client-only organizations cannot bill invoices")
        return row

    def _audit_change(
        self,
        repo: VendorInvoiceRepository,
        before: VendorInvoiceRow,
        after: VendorInvoiceRow,
        action: str,
    ) -> None:
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="vendor_invoice",
            entity_id=after.id,
            action=action,
            diff={
                "before": _audit_shape(_to_view(before)),
                "after": _audit_shape(_to_view(after)),
            },
            clock=self._clock,
        )

    def _audit_state(
        self,
        repo: VendorInvoiceRepository,
        before: VendorInvoiceRow,
        after: VendorInvoiceRow,
        *,
        action: str,
    ) -> None:
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="vendor_invoice",
            entity_id=after.id,
            action=action,
            diff={
                "from": before.status,
                "to": after.status,
                "before": _audit_shape(_to_view(before)),
                "after": _audit_shape(_to_view(after)),
            },
            clock=self._clock,
        )


def _clean_required(value: str, *, field: str) -> str:
    clean = value.strip()
    if not clean:
        raise VendorInvoiceInvalid(f"{field} is required")
    return clean


def _clean_optional(value: object | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise VendorInvoiceInvalid("optional text fields must be strings")
    clean = value.strip()
    return clean or None


def _clean_currency(value: str) -> str:
    clean = _clean_required(value, field="currency").upper()
    if len(clean) != 3 or not clean.isalpha():
        raise VendorInvoiceInvalid("currency must be a 3-letter ISO code")
    return clean


def _clean_blob_hash(value: str, *, field: str) -> str:
    clean = _clean_required(value, field=field)
    if len(clean) != _BLOB_HASH_LEN:
        raise VendorInvoiceInvalid(f"{field} must be a 64-character blob hash")
    if clean.lower() != clean or any(char not in "0123456789abcdef" for char in clean):
        raise VendorInvoiceInvalid(f"{field} must be lowercase SHA-256 hex")
    return clean


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise VendorInvoiceInvalid("paid_at must include a timezone")
    return value.astimezone(UTC)


def _validate_status(value: str) -> str:
    clean = value.strip()
    if clean not in _STATUS_VALUES:
        raise VendorInvoiceInvalid(f"unknown vendor-invoice status {value!r}")
    return clean


def _reminder_windows(due_at: date | None) -> tuple[date, ...]:
    if due_at is None:
        return ()
    return (due_at - timedelta(days=7), due_at - timedelta(days=1), due_at)


def _to_view(row: VendorInvoiceRow) -> VendorInvoiceView:
    return VendorInvoiceView(
        id=row.id,
        workspace_id=row.workspace_id,
        vendor_org_id=row.vendor_org_id,
        invoice_number=row.invoice_number,
        issued_at=row.issued_at,
        due_at=row.due_at,
        total_cents=row.total_cents,
        currency=row.currency,
        status=row.status,
        pdf_blob_hash=row.pdf_blob_hash,
        approved_at=row.approved_at,
        paid_at=row.paid_at,
        payment_method=row.payment_method,
        proof_blob_hash=row.proof_blob_hash,
        proof_of_payment_file_ids=row.proof_of_payment_file_ids,
        disputed_at=row.disputed_at,
        notes_md=row.notes_md,
        reminder_windows=_reminder_windows(row.due_at),
    )


def _audit_shape(view: VendorInvoiceView) -> dict[str, object]:
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "vendor_org_id": view.vendor_org_id,
        "invoice_number": view.invoice_number,
        "issued_at": view.issued_at.isoformat(),
        "due_at": view.due_at.isoformat() if view.due_at is not None else None,
        "total_cents": view.total_cents,
        "currency": view.currency,
        "status": view.status,
        "pdf_blob_hash": view.pdf_blob_hash,
        "approved_at": (
            view.approved_at.isoformat() if view.approved_at is not None else None
        ),
        "paid_at": view.paid_at.isoformat() if view.paid_at is not None else None,
        "payment_method": view.payment_method,
        "proof_blob_hash": view.proof_blob_hash,
        "proof_of_payment_file_ids": list(view.proof_of_payment_file_ids),
        "disputed_at": (
            view.disputed_at.isoformat() if view.disputed_at is not None else None
        ),
        "notes_md": view.notes_md,
    }
