"""Unit tests for billing vendor invoices."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.billing.models import Organization
from app.adapters.db.billing.repositories import SqlAlchemyVendorInvoiceRepository
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.billing.vendor_invoices import (
    VendorInvoiceCreate,
    VendorInvoiceInvalid,
    VendorInvoiceMarkPaid,
    VendorInvoiceService,
)
from app.events import EventBus, VendorInvoicePaid
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)
_ISSUED = date(2026, 4, 20)
_DUE = date(2026, 5, 20)
_PDF_HASH = "a" * 64
_PROOF_HASH = "b" * 64


def _load_all_models() -> None:
    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def _ctx(workspace_id: str, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="vendor-invoices",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _seed_billing(s: Session) -> tuple[str, str, str]:
    workspace_id = new_ulid()
    user_id = new_ulid()
    vendor_org_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"vi-{workspace_id[-6:].lower()}",
            name="Vendor Invoices",
            plan="free",
            quota_json={},
            settings_json={},
            default_currency="EUR",
            created_at=_PINNED,
        )
    )
    s.flush()
    email = f"manager-{user_id[-6:]}@example.com"
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name="manager",
            created_at=_PINNED,
        )
    )
    s.flush()
    s.add(
        Organization(
            id=vendor_org_id,
            workspace_id=workspace_id,
            kind="vendor",
            display_name="Acme Services",
            billing_address={},
            tax_id=None,
            default_currency="EUR",
            contact_email=None,
            contact_phone=None,
            notes_md=None,
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id, user_id, vendor_org_id


def _create_body(
    vendor_org_id: str, invoice_number: str = "INV-1001"
) -> VendorInvoiceCreate:
    return VendorInvoiceCreate(
        vendor_org_id=vendor_org_id,
        invoice_number=invoice_number,
        issued_at=_ISSUED,
        due_at=_DUE,
        total_cents=12_500,
        currency="eur",
        notes_md="  install materials  ",
    )


def _service(
    ctx: WorkspaceContext, bus: EventBus | None = None
) -> VendorInvoiceService:
    return VendorInvoiceService(ctx, clock=FrozenClock(_PINNED), event_bus=bus)


def test_create_attach_approve_mark_paid_audits_and_publishes_event(
    factory: sessionmaker[Session],
) -> None:
    events: list[VendorInvoicePaid] = []
    event_bus = EventBus()
    event_bus.subscribe(VendorInvoicePaid)(events.append)
    with factory() as s:
        workspace_id, user_id, vendor_org_id = _seed_billing(s)
        ctx = _ctx(workspace_id, user_id)
        repo = SqlAlchemyVendorInvoiceRepository(s)
        service = _service(ctx, event_bus)

        created = service.create(repo, _create_body(vendor_org_id))
        with pytest.raises(VendorInvoiceInvalid, match="without a PDF"):
            service.approve(repo, created.id)
        attached = service.attach_pdf(repo, created.id, _PDF_HASH)
        approved = service.approve(repo, created.id)
        proofed = service.upload_proof(repo, created.id, _PROOF_HASH)
        paid = service.mark_paid(
            repo,
            created.id,
            VendorInvoiceMarkPaid(
                paid_at=_PINNED,
                payment_method="bank_transfer",
                proof_blob_hash=_PROOF_HASH,
            ),
        )

        assert created.status == "received"
        assert created.currency == "EUR"
        assert created.reminder_windows == (date(2026, 5, 13), date(2026, 5, 19), _DUE)
        assert attached.pdf_blob_hash == _PDF_HASH
        assert approved.approved_at == _PINNED
        assert proofed.status == "approved"
        assert proofed.paid_at is None
        assert proofed.proof_of_payment_file_ids == (_PROOF_HASH,)
        assert paid.status == "paid"
        assert paid.payment_method == "bank_transfer"
        assert [event.vendor_invoice_id for event in events] == [created.id]
        assert [row.action for row in s.scalars(select(AuditLog)).all()] == [
            "billing.vendor_invoice.created",
            "billing.vendor_invoice.pdf_attached",
            "billing.vendor_invoice.approved",
            "billing.vendor_invoice.proof_uploaded",
            "billing.vendor_invoice.paid",
        ]


def test_duplicate_invoice_number_for_vendor_is_clear(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id, user_id, vendor_org_id = _seed_billing(s)
        repo = SqlAlchemyVendorInvoiceRepository(s)
        service = _service(_ctx(workspace_id, user_id))

        service.create(repo, _create_body(vendor_org_id))
        with pytest.raises(VendorInvoiceInvalid, match="duplicates invoice_number"):
            service.create(repo, _create_body(vendor_org_id))


def test_invalid_transitions_and_payment_requirements(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id, user_id, vendor_org_id = _seed_billing(s)
        repo = SqlAlchemyVendorInvoiceRepository(s)
        service = _service(_ctx(workspace_id, user_id))
        invoice = service.create(repo, _create_body(vendor_org_id))

        with pytest.raises(VendorInvoiceInvalid, match="approved vendor invoices"):
            service.upload_proof(repo, invoice.id, _PROOF_HASH)
        with pytest.raises(VendorInvoiceInvalid, match="to 'paid'"):
            service.mark_paid(
                repo,
                invoice.id,
                VendorInvoiceMarkPaid(paid_at=_PINNED, payment_method="cash"),
            )
        service.attach_pdf(repo, invoice.id, _PDF_HASH)
        service.approve(repo, invoice.id)
        with pytest.raises(VendorInvoiceInvalid, match="payment_method is required"):
            service.mark_paid(
                repo,
                invoice.id,
                VendorInvoiceMarkPaid(paid_at=_PINNED, payment_method=" "),
            )
