"""Billing vendor-invoice HTTP routes."""

from __future__ import annotations

import hashlib
import io
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.adapters.db.billing.repositories import (
    SqlAlchemyClientPortalRepository,
    SqlAlchemyVendorInvoiceRepository,
)
from app.adapters.storage.ports import MimeSniffer, Storage
from app.api.deps import (
    current_workspace_context,
    db_session,
    get_mime_sniffer,
    get_storage,
)
from app.authz.dep import Permission
from app.domain.billing.vendor_invoices import (
    VendorInvoiceCreate,
    VendorInvoiceInvalid,
    VendorInvoiceMarkPaid,
    VendorInvoiceNotFound,
    VendorInvoiceService,
    VendorInvoiceView,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "VendorInvoiceCreateRequest",
    "VendorInvoiceListResponse",
    "VendorInvoiceMarkPaidRequest",
    "VendorInvoiceResponse",
    "build_vendor_invoices_router",
]

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_Storage = Annotated[Storage, Depends(get_storage)]
_MimeSniffer = Annotated[MimeSniffer, Depends(get_mime_sniffer)]
_Status = Literal["received", "approved", "paid", "disputed"]
_MAX_PDF_BYTES = 25 * 1024 * 1024
_MAX_PDF_BODY_BYTES = _MAX_PDF_BYTES + 1
_MAX_PROOF_BYTES = 25 * 1024 * 1024
_MAX_PROOF_BODY_BYTES = _MAX_PROOF_BYTES + 1
_PROOF_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)


class VendorInvoiceResponse(BaseModel):
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

    @classmethod
    def from_view(cls, view: VendorInvoiceView) -> VendorInvoiceResponse:
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            vendor_org_id=view.vendor_org_id,
            invoice_number=view.invoice_number,
            issued_at=view.issued_at,
            due_at=view.due_at,
            total_cents=view.total_cents,
            currency=view.currency,
            status=view.status,
            pdf_blob_hash=view.pdf_blob_hash,
            approved_at=view.approved_at,
            paid_at=view.paid_at,
            payment_method=view.payment_method,
            proof_blob_hash=view.proof_blob_hash,
            proof_of_payment_file_ids=view.proof_of_payment_file_ids,
            disputed_at=view.disputed_at,
            notes_md=view.notes_md,
            reminder_windows=view.reminder_windows,
        )


class VendorInvoiceListResponse(BaseModel):
    data: list[VendorInvoiceResponse]


class VendorInvoiceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_org_id: str = Field(min_length=1, max_length=64)
    invoice_number: str = Field(min_length=1, max_length=100)
    issued_at: date
    due_at: date | None = None
    total_cents: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    notes_md: str | None = Field(default=None, max_length=20_000)

    def to_domain(self) -> VendorInvoiceCreate:
        return VendorInvoiceCreate(
            vendor_org_id=self.vendor_org_id,
            invoice_number=self.invoice_number,
            issued_at=self.issued_at,
            due_at=self.due_at,
            total_cents=self.total_cents,
            currency=self.currency,
            notes_md=self.notes_md,
        )


class VendorInvoiceMarkPaidRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paid_at: datetime
    payment_method: str = Field(min_length=1, max_length=100)
    proof_blob_hash: str | None = Field(default=None, min_length=64, max_length=64)

    def to_domain(self) -> VendorInvoiceMarkPaid:
        return VendorInvoiceMarkPaid(
            paid_at=self.paid_at,
            payment_method=self.payment_method,
            proof_blob_hash=self.proof_blob_hash,
        )


def _http_for_vendor_invoice_error(exc: Exception) -> HTTPException:
    if isinstance(exc, VendorInvoiceNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "vendor_invoice_not_found", "message": str(exc)},
        )
    if isinstance(exc, VendorInvoiceInvalid):
        return HTTPException(
            status_code=422,
            detail={"error": "vendor_invoice_invalid", "message": str(exc)},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


async def _read_pdf_capped(upload: UploadFile) -> bytes:
    chunk_size = 64 * 1024
    total = 0
    pieces: list[bytes] = []
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_PDF_BYTES:
            await upload.close()
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"error": "vendor_invoice_pdf_too_large"},
            )
        pieces.append(chunk)
    await upload.close()
    return b"".join(pieces)


def _sniff_pdf(mime_sniffer: MimeSniffer, payload: bytes, declared_type: str) -> str:
    sniffed = mime_sniffer.sniff(payload, hint=declared_type)
    if sniffed != "application/pdf":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"error": "vendor_invoice_pdf_invalid_type"},
        )
    return sniffed


def _check_pdf_content_length(request: Request) -> None:
    cl = request.headers.get("content-length")
    if cl is None:
        return
    try:
        size = int(cl)
    except ValueError:
        return
    if size > _MAX_PDF_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={"error": "vendor_invoice_pdf_too_large"},
        )


_PdfContentLengthGuard = Annotated[None, Depends(_check_pdf_content_length)]


async def _read_proof_capped(upload: UploadFile) -> bytes:
    chunk_size = 64 * 1024
    total = 0
    pieces: list[bytes] = []
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_PROOF_BYTES:
            await upload.close()
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"error": "vendor_invoice_proof_too_large"},
            )
        pieces.append(chunk)
    await upload.close()
    return b"".join(pieces)


def _sniff_proof(mime_sniffer: MimeSniffer, payload: bytes, declared_type: str) -> str:
    sniffed = mime_sniffer.sniff(payload, hint=declared_type)
    if sniffed not in _PROOF_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"error": "vendor_invoice_proof_invalid_type"},
        )
    return sniffed


def _check_proof_content_length(request: Request) -> None:
    cl = request.headers.get("content-length")
    if cl is None:
        return
    try:
        size = int(cl)
    except ValueError:
        return
    if size > _MAX_PROOF_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={"error": "vendor_invoice_proof_too_large"},
        )


_ProofContentLengthGuard = Annotated[None, Depends(_check_proof_content_length)]


def _service(ctx: WorkspaceContext) -> VendorInvoiceService:
    return VendorInvoiceService(ctx)


def _repo(session: Session) -> SqlAlchemyVendorInvoiceRepository:
    return SqlAlchemyVendorInvoiceRepository(session)


def _ensure_client_upload_scope(
    ctx: WorkspaceContext, session: Session, invoice_id: str
) -> None:
    if ctx.actor_grant_role != "client":
        return
    row = _repo(session).get(
        workspace_id=ctx.workspace_id, invoice_id=invoice_id, for_update=False
    )
    if row is None:
        raise _http_for_vendor_invoice_error(
            VendorInvoiceNotFound("vendor invoice not found")
        )
    scope = SqlAlchemyClientPortalRepository(session).client_scope(
        workspace_id=ctx.workspace_id,
        user_id=ctx.actor_id,
    )
    if row.vendor_org_id not in scope.workspace_org_ids:
        raise _http_for_vendor_invoice_error(
            VendorInvoiceNotFound("vendor invoice not found")
        )


def build_vendor_invoices_router() -> APIRouter:
    router = APIRouter(
        prefix="/vendor-invoices",
        tags=["billing", "vendor-invoices"],
    )

    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))
    submit_gate = Depends(Permission("vendor_invoices.submit", scope_kind="workspace"))
    approve_gate = Depends(
        Permission("vendor_invoices.approve", scope_kind="workspace")
    )
    pay_gate = Depends(Permission("vendor_invoices.mark_paid", scope_kind="workspace"))
    upload_proof_gate = Depends(
        Permission("vendor_invoices.upload_proof", scope_kind="workspace")
    )

    @router.get(
        "",
        response_model=VendorInvoiceListResponse,
        operation_id="billing.vendor_invoices.list",
        dependencies=[view_gate],
        summary="List vendor invoices",
    )
    def list_vendor_invoices(
        ctx: _Ctx,
        session: _Db,
        vendor_org_id: str | None = None,
        status: _Status | None = None,
    ) -> VendorInvoiceListResponse:
        try:
            views = _service(ctx).list(
                _repo(session),
                vendor_org_id=vendor_org_id,
                status=status,
            )
        except VendorInvoiceInvalid as exc:
            raise _http_for_vendor_invoice_error(exc) from exc
        return VendorInvoiceListResponse(
            data=[VendorInvoiceResponse.from_view(view) for view in views]
        )

    @router.post(
        "",
        response_model=VendorInvoiceResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="billing.vendor_invoices.create",
        dependencies=[submit_gate],
        summary="Create a vendor invoice",
    )
    def create_vendor_invoice(
        body: VendorInvoiceCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> VendorInvoiceResponse:
        try:
            view = _service(ctx).create(_repo(session), body.to_domain())
        except (VendorInvoiceInvalid, VendorInvoiceNotFound) as exc:
            raise _http_for_vendor_invoice_error(exc) from exc
        return VendorInvoiceResponse.from_view(view)

    @router.get(
        "/{invoice_id}",
        response_model=VendorInvoiceResponse,
        operation_id="billing.vendor_invoices.get",
        dependencies=[view_gate],
        summary="Get a vendor invoice",
    )
    def get_vendor_invoice(
        invoice_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> VendorInvoiceResponse:
        try:
            view = _service(ctx).get(_repo(session), invoice_id)
        except VendorInvoiceNotFound as exc:
            raise _http_for_vendor_invoice_error(exc) from exc
        return VendorInvoiceResponse.from_view(view)

    @router.post(
        "/{invoice_id}/pdf",
        response_model=VendorInvoiceResponse,
        operation_id="billing.vendor_invoices.attach_pdf",
        dependencies=[submit_gate],
        summary="Attach a PDF to a vendor invoice",
    )
    async def attach_vendor_invoice_pdf(
        invoice_id: str,
        ctx: _Ctx,
        session: _Db,
        storage: _Storage,
        mime_sniffer: _MimeSniffer,
        _: _PdfContentLengthGuard,
        file: Annotated[UploadFile | None, File()] = None,
    ) -> VendorInvoiceResponse:
        if file is None:
            raise HTTPException(
                status_code=422,
                detail={"error": "vendor_invoice_pdf_required"},
            )
        declared_type = file.content_type
        if declared_type is None or declared_type == "":
            await file.close()
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={"error": "vendor_invoice_pdf_content_type_missing"},
            )
        payload = await _read_pdf_capped(file)
        sniffed_type = _sniff_pdf(mime_sniffer, payload, declared_type)
        blob_hash = hashlib.sha256(payload).hexdigest()
        storage.put(blob_hash, io.BytesIO(payload), content_type=sniffed_type)
        try:
            view = _service(ctx).attach_pdf(_repo(session), invoice_id, blob_hash)
        except (VendorInvoiceInvalid, VendorInvoiceNotFound) as exc:
            raise _http_for_vendor_invoice_error(exc) from exc
        return VendorInvoiceResponse.from_view(view)

    @router.post(
        "/{invoice_id}/approve",
        response_model=VendorInvoiceResponse,
        operation_id="billing.vendor_invoices.approve",
        dependencies=[approve_gate],
        summary="Approve a vendor invoice",
    )
    def approve_vendor_invoice(
        invoice_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> VendorInvoiceResponse:
        try:
            view = _service(ctx).approve(_repo(session), invoice_id)
        except (VendorInvoiceInvalid, VendorInvoiceNotFound) as exc:
            raise _http_for_vendor_invoice_error(exc) from exc
        return VendorInvoiceResponse.from_view(view)

    @router.post(
        "/{invoice_id}/paid",
        response_model=VendorInvoiceResponse,
        operation_id="billing.vendor_invoices.mark_paid",
        dependencies=[pay_gate],
        summary="Mark a vendor invoice paid",
    )
    def mark_vendor_invoice_paid(
        invoice_id: str,
        body: VendorInvoiceMarkPaidRequest,
        ctx: _Ctx,
        session: _Db,
        storage: _Storage,
    ) -> VendorInvoiceResponse:
        if body.proof_blob_hash is not None and not storage.exists(
            body.proof_blob_hash
        ):
            raise HTTPException(
                status_code=422,
                detail={"error": "vendor_invoice_proof_blob_not_found"},
            )
        try:
            view = _service(ctx).mark_paid(_repo(session), invoice_id, body.to_domain())
        except (VendorInvoiceInvalid, VendorInvoiceNotFound) as exc:
            raise _http_for_vendor_invoice_error(exc) from exc
        return VendorInvoiceResponse.from_view(view)

    @router.post(
        "/{invoice_id}/proof",
        response_model=VendorInvoiceResponse,
        operation_id="billing.vendor_invoices.upload_proof",
        dependencies=[upload_proof_gate],
        summary="Upload proof of payment for a vendor invoice",
        openapi_extra={
            "x-agent-confirm": {
                "message": "Upload proof of payment for this vendor invoice?"
            }
        },
    )
    async def upload_vendor_invoice_proof(
        invoice_id: str,
        ctx: _Ctx,
        session: _Db,
        storage: _Storage,
        mime_sniffer: _MimeSniffer,
        _: _ProofContentLengthGuard,
        file: Annotated[UploadFile | None, File()] = None,
    ) -> VendorInvoiceResponse:
        _ensure_client_upload_scope(ctx, session, invoice_id)
        if file is None:
            raise HTTPException(
                status_code=422,
                detail={"error": "vendor_invoice_proof_required"},
            )
        declared_type = file.content_type
        if declared_type is None or declared_type == "":
            await file.close()
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={"error": "vendor_invoice_proof_content_type_missing"},
            )
        payload = await _read_proof_capped(file)
        sniffed_type = _sniff_proof(mime_sniffer, payload, declared_type)
        blob_hash = hashlib.sha256(payload).hexdigest()
        storage.put(blob_hash, io.BytesIO(payload), content_type=sniffed_type)
        try:
            view = _service(ctx).upload_proof(_repo(session), invoice_id, blob_hash)
        except (VendorInvoiceInvalid, VendorInvoiceNotFound) as exc:
            raise _http_for_vendor_invoice_error(exc) from exc
        return VendorInvoiceResponse.from_view(view)

    @router.post(
        "/{invoice_id}/dispute",
        response_model=VendorInvoiceResponse,
        operation_id="billing.vendor_invoices.dispute",
        dependencies=[submit_gate],
        summary="Dispute a vendor invoice",
    )
    def dispute_vendor_invoice(
        invoice_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> VendorInvoiceResponse:
        try:
            view = _service(ctx).dispute(_repo(session), invoice_id)
        except (VendorInvoiceInvalid, VendorInvoiceNotFound) as exc:
            raise _http_for_vendor_invoice_error(exc) from exc
        return VendorInvoiceResponse.from_view(view)

    return router
