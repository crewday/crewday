"""HTTP tests for billing vendor-invoice routes."""

from __future__ import annotations

import hashlib
import importlib
import io
import pkgutil
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.billing.models import Organization, VendorInvoice
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import (
    current_workspace_context,
    db_session,
    get_mime_sniffer,
    get_storage,
)
from app.api.v1.billing import build_billing_router
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests._fakes.storage import InMemoryStorage

_PINNED = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)
_PDF_BYTES = b"%PDF-1.7\nvendor invoice\n%%EOF\n"
_PDF_HASH = hashlib.sha256(_PDF_BYTES).hexdigest()
_UPLOAD_PROOF_BYTES = b"%PDF-1.7\npayment proof\n%%EOF\n"
_UPLOAD_PROOF_HASH = hashlib.sha256(_UPLOAD_PROOF_BYTES).hexdigest()
_PROOF_HASH = "c" * 64


class _PdfSniffer:
    def sniff(self, payload: bytes, *, hint: str | None = None) -> str | None:
        if payload.startswith(b"%PDF-"):
            return "application/pdf"
        return None


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
def api_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(api_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=api_engine, expire_on_commit=False, class_=Session)


def _bootstrap(
    s: Session, *, grant_role: Literal["manager", "worker"] = "manager"
) -> tuple[str, str, str]:
    workspace_id = new_ulid()
    manager_id = new_ulid()
    vendor_org_id = new_ulid()
    email = f"manager-{manager_id[-6:]}@example.com"
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"vi-api-{workspace_id[-6:].lower()}",
            name="Vendor Invoices API",
            plan="free",
            quota_json={},
            settings_json={},
            default_currency="EUR",
            created_at=_PINNED,
        )
    )
    s.flush()
    s.add(
        User(
            id=manager_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name="manager",
            created_at=_PINNED,
        )
    )
    s.flush()
    s.add(
        UserWorkspace(
            user_id=manager_id,
            workspace_id=workspace_id,
            source="workspace_grant",
            added_at=_PINNED,
        )
    )
    s.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=manager_id,
            grant_role=grant_role,
            scope_kind="workspace",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
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
    return workspace_id, manager_id, vendor_org_id


def _ctx(
    workspace_id: str,
    manager_id: str,
    *,
    role: Literal["manager", "worker"] = "manager",
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="vendor-invoices-api",
        actor_id=manager_id,
        actor_kind="user",
        actor_grant_role=role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _build_app(
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
    storage: InMemoryStorage,
) -> FastAPI:
    app = FastAPI()
    app.include_router(build_billing_router(), prefix="/billing")

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    app.dependency_overrides[current_workspace_context] = lambda: ctx
    app.dependency_overrides[db_session] = _override_db
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_mime_sniffer] = lambda: _PdfSniffer()
    return app


def _create_payload(
    vendor_org_id: str, invoice_number: str = "INV-1001"
) -> dict[str, object]:
    return {
        "vendor_org_id": vendor_org_id,
        "invoice_number": invoice_number,
        "issued_at": date(2026, 4, 20).isoformat(),
        "due_at": date(2026, 5, 20).isoformat(),
        "total_cents": 12_500,
        "currency": "EUR",
    }


def test_vendor_invoice_flow(factory: sessionmaker[Session]) -> None:
    storage = InMemoryStorage()
    storage.put(_PROOF_HASH, io.BytesIO(b"proof"), content_type="application/pdf")
    with factory() as s:
        workspace_id, manager_id, vendor_org_id = _bootstrap(s)
        s.commit()
    client = TestClient(
        _build_app(factory, _ctx(workspace_id, manager_id), storage),
        raise_server_exceptions=False,
    )

    created = client.post(
        "/billing/vendor-invoices",
        json=_create_payload(vendor_org_id),
    )
    assert created.status_code == 201
    invoice = created.json()
    assert invoice["status"] == "received"
    assert invoice["reminder_windows"] == ["2026-05-13", "2026-05-19", "2026-05-20"]

    bad_approve = client.post(f"/billing/vendor-invoices/{invoice['id']}/approve")
    assert bad_approve.status_code == 422
    assert bad_approve.json()["detail"]["error"] == "vendor_invoice_invalid"

    uploaded = client.post(
        f"/billing/vendor-invoices/{invoice['id']}/pdf",
        files={"file": ("invoice.pdf", _PDF_BYTES, "application/pdf")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["pdf_blob_hash"] == _PDF_HASH
    assert storage.exists(_PDF_HASH)

    approved = client.post(f"/billing/vendor-invoices/{invoice['id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    proof = client.post(
        f"/billing/vendor-invoices/{invoice['id']}/proof",
        files={"file": ("proof.pdf", _UPLOAD_PROOF_BYTES, "application/pdf")},
    )
    assert proof.status_code == 200
    proof_body = proof.json()
    assert proof_body["status"] == "approved"
    assert proof_body["paid_at"] is None
    assert proof_body["proof_of_payment_file_ids"] == [_UPLOAD_PROOF_HASH]
    assert storage.exists(_UPLOAD_PROOF_HASH)

    missing_payment_fields = client.post(
        f"/billing/vendor-invoices/{invoice['id']}/paid",
        json={"payment_method": "bank_transfer"},
    )
    assert missing_payment_fields.status_code == 422

    paid = client.post(
        f"/billing/vendor-invoices/{invoice['id']}/paid",
        json={
            "paid_at": _PINNED.isoformat(),
            "payment_method": "bank_transfer",
            "proof_blob_hash": _PROOF_HASH,
        },
    )
    assert paid.status_code == 200
    assert paid.json()["status"] == "paid"
    assert paid.json()["payment_method"] == "bank_transfer"


def test_duplicate_invoice_number_surfaces_clear_error(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id, manager_id, vendor_org_id = _bootstrap(s)
        s.commit()
    client = TestClient(
        _build_app(factory, _ctx(workspace_id, manager_id), InMemoryStorage()),
        raise_server_exceptions=False,
    )

    assert (
        client.post(
            "/billing/vendor-invoices", json=_create_payload(vendor_org_id)
        ).status_code
        == 201
    )
    duplicate = client.post(
        "/billing/vendor-invoices", json=_create_payload(vendor_org_id)
    )
    assert duplicate.status_code == 422
    assert "duplicates invoice_number" in duplicate.json()["detail"]["message"]


def test_worker_cannot_approve_or_mark_paid(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        workspace_id, worker_id, vendor_org_id = _bootstrap(s, grant_role="worker")
        invoice_id = new_ulid()
        s.add(
            VendorInvoice(
                id=invoice_id,
                workspace_id=workspace_id,
                vendor_org_id=vendor_org_id,
                invoice_number="INV-LOCKED",
                issued_at=date(2026, 4, 20),
                due_at=None,
                total_cents=10_000,
                currency="EUR",
                status="approved",
                pdf_blob_hash=_PDF_HASH,
                approved_at=_PINNED,
                paid_at=None,
                payment_method=None,
                proof_blob_hash=None,
                disputed_at=None,
                notes_md=None,
            )
        )
        s.commit()
    client = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id, worker_id, role="worker"),
            InMemoryStorage(),
        ),
        raise_server_exceptions=False,
    )

    approve = client.post(f"/billing/vendor-invoices/{invoice_id}/approve")
    proof = client.post(
        f"/billing/vendor-invoices/{invoice_id}/proof",
        files={"file": ("proof.pdf", _UPLOAD_PROOF_BYTES, "application/pdf")},
    )
    paid = client.post(
        f"/billing/vendor-invoices/{invoice_id}/paid",
        json={"paid_at": _PINNED.isoformat(), "payment_method": "cash"},
    )
    assert approve.status_code == 403
    assert proof.status_code == 403
    assert paid.status_code == 403
