"""Asset/workspace document endpoints.

Two surfaces live here:

- :func:`build_documents_router` — workspace-scoped ``/documents`` and
  ``/documents/{document_id}/extraction`` routes, plus the placeholder
  extraction endpoints. The extraction shapes return conservative
  pending metadata until ``cd-mo9e`` lands real ``file_extraction``
  rows; this module preserves the placeholder semantics.
- :func:`build_asset_documents_subrouter` — asset-scoped
  ``/{asset_id}/documents`` listing + upload and the workspace-level
  ``/documents/{document_id}`` DELETE. Mounted by the core asset
  router so the public paths stay unchanged.
"""

from __future__ import annotations

import hashlib
import io
from datetime import date
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.assets.models import Asset as AssetRow
from app.api.assets._shared import (
    ASSET_ERROR_RESPONSES,
    Ctx,
    Db,
    MimeSnifferDep,
    StorageDep,
    http_for_document_error,
    read_document_capped,
    sniff_document_mime,
)
from app.api.assets.schemas import (
    AssetDocumentListResponse,
    AssetDocumentResponse,
    DocumentExtractionPageResponse,
    DocumentExtractionResponse,
    WorkspaceDocumentListResponse,
    WorkspaceDocumentResponse,
)
from app.api.uploads import require_upload_content_type
from app.authz.dep import Permission
from app.domain.assets.assets import AssetNotFound, get_asset
from app.domain.assets.documents import (
    ASSET_DOCUMENT_CATEGORIES,
    AssetDocumentNotFound,
    AssetDocumentValidationError,
    AssetDocumentView,
    attach_document,
    delete_document,
    list_documents,
    list_workspace_documents,
)
from app.tenancy import WorkspaceContext, tenant_agnostic

__all__ = [
    "build_asset_documents_subrouter",
    "build_documents_router",
]


def _workspace_document_response(
    view: AssetDocumentView,
    *,
    asset_property_ids: dict[str, str],
) -> WorkspaceDocumentResponse:
    property_id = view.property_id
    if property_id is None and view.asset_id is not None:
        property_id = asset_property_ids.get(view.asset_id)
    if property_id is None:
        raise AssetDocumentValidationError("property_id", "required")
    return WorkspaceDocumentResponse(
        id=view.id,
        asset_id=view.asset_id,
        property_id=property_id,
        kind=view.kind,
        title=view.title,
        filename=view.filename or view.title,
        size_kb=0,
        uploaded_at=view.created_at,
        expires_on=view.expires_on,
        amount_cents=view.amount_cents,
        amount_currency=view.amount_currency,
        extraction_status="pending",
        extracted_at=None,
    )


def _asset_property_ids(
    session: Session,
    ctx: WorkspaceContext,
    views: list[AssetDocumentView],
) -> dict[str, str]:
    asset_ids = {view.asset_id for view in views if view.asset_id is not None}
    if not asset_ids:
        return {}
    with tenant_agnostic():
        rows = session.execute(
            select(AssetRow.id, AssetRow.property_id).where(
                AssetRow.workspace_id == ctx.workspace_id,
                AssetRow.id.in_(asset_ids),
            )
        ).all()
    return {asset_id: property_id for asset_id, property_id in rows}


def _load_workspace_document(
    session: Session,
    ctx: WorkspaceContext,
    document_id: str,
) -> AssetDocumentView:
    matches = list_workspace_documents(session, ctx)
    for view in matches:
        if view.id == document_id:
            return view
    raise AssetDocumentNotFound(document_id)


def build_documents_router() -> APIRouter:
    api = APIRouter(tags=["assets"], responses=ASSET_ERROR_RESPONSES)
    manage_documents_gate = Depends(
        Permission("assets.manage_documents", scope_kind="workspace")
    )

    @api.get(
        "/documents",
        response_model=WorkspaceDocumentListResponse,
        operation_id="documents.list",
        summary="List workspace documents",
        dependencies=[manage_documents_gate],
        openapi_extra={"x-cli": {"group": "documents", "verb": "list"}},
    )
    def documents(
        ctx: Ctx,
        session: Db,
        asset_id: str | None = Query(default=None),
        property_id: str | None = Query(default=None),
        kind: (
            Literal[
                "manual",
                "warranty",
                "invoice",
                "receipt",
                "photo",
                "certificate",
                "contract",
                "permit",
                "insurance",
                "other",
            ]
            | None
        ) = Query(default=None),
        expires_before: Annotated[date | None, Query()] = None,
    ) -> WorkspaceDocumentListResponse:
        try:
            views = list_workspace_documents(
                session,
                ctx,
                asset_id=asset_id,
                category=kind,
                expires_before=expires_before,
            )
            asset_property_ids = _asset_property_ids(session, ctx, views)
            rows = [
                _workspace_document_response(
                    view,
                    asset_property_ids=asset_property_ids,
                )
                for view in views
            ]
        except AssetDocumentValidationError as exc:
            raise http_for_document_error(exc) from exc
        if property_id is not None:
            rows = [row for row in rows if row.property_id == property_id]
        return WorkspaceDocumentListResponse(data=rows)

    @api.get(
        "/documents/{document_id}",
        response_model=WorkspaceDocumentResponse,
        operation_id="documents.get",
        summary="Get one workspace document",
        dependencies=[manage_documents_gate],
    )
    def document(
        document_id: str,
        ctx: Ctx,
        session: Db,
    ) -> WorkspaceDocumentResponse:
        try:
            view = _load_workspace_document(session, ctx, document_id)
            return _workspace_document_response(
                view,
                asset_property_ids=_asset_property_ids(session, ctx, [view]),
            )
        except (AssetDocumentNotFound, AssetDocumentValidationError) as exc:
            raise http_for_document_error(exc) from exc

    @api.get(
        "/documents/{document_id}/extraction",
        response_model=DocumentExtractionResponse,
        operation_id="documents.extraction.get",
        summary="Get document extraction status",
        dependencies=[manage_documents_gate],
    )
    def extraction(
        document_id: str,
        ctx: Ctx,
        session: Db,
    ) -> DocumentExtractionResponse:
        try:
            _load_workspace_document(session, ctx, document_id)
        except AssetDocumentNotFound as exc:
            raise http_for_document_error(exc) from exc
        # Temporary cd-mo9e boundary: the file_extraction table and worker do
        # not exist yet, so expose conservative pending metadata only.
        return DocumentExtractionResponse(
            document_id=document_id,
            status="pending",
            extractor=None,
            body_preview="",
            page_count=0,
            token_count=0,
            has_secret_marker=False,
            last_error=None,
            extracted_at=None,
        )

    @api.get(
        "/documents/{document_id}/extraction/pages/{page}",
        response_model=DocumentExtractionPageResponse,
        operation_id="documents.extraction.page",
        summary="Get one document extraction page",
        dependencies=[manage_documents_gate],
    )
    def extraction_page(
        document_id: str,
        ctx: Ctx,
        session: Db,
        page: Annotated[int, Path(ge=1)],
    ) -> DocumentExtractionPageResponse:
        try:
            _load_workspace_document(session, ctx, document_id)
        except AssetDocumentNotFound as exc:
            raise http_for_document_error(exc) from exc
        # Temporary cd-mo9e boundary: no extracted page bodies are persisted yet.
        return DocumentExtractionPageResponse(
            page=page,
            char_start=0,
            char_end=0,
            body="",
            more_pages=False,
        )

    @api.post(
        "/documents/{document_id}/extraction/retry",
        status_code=status.HTTP_202_ACCEPTED,
        operation_id="documents.extraction.retry",
        summary="Retry document extraction",
        dependencies=[manage_documents_gate],
    )
    def retry_extraction(
        document_id: str,
        ctx: Ctx,
        session: Db,
    ) -> Response:
        try:
            _load_workspace_document(session, ctx, document_id)
        except AssetDocumentNotFound as exc:
            raise http_for_document_error(exc) from exc
        return Response(status_code=status.HTTP_202_ACCEPTED)

    return api


def build_asset_documents_subrouter() -> APIRouter:
    """Sub-router mounted under the core asset router prefix.

    Owns the asset-scoped document listing/upload and the workspace
    document DELETE. Paths are unchanged when this is included into
    the asset router; operation IDs preserve the ``assets.documents.*``
    namespace.
    """

    # Parent router already carries ``tags=["assets"]`` and
    # ``ASSET_ERROR_RESPONSES``; sub-router intentionally bare so the
    # OpenAPI per-route tag list stays byte-identical.
    api = APIRouter()
    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))
    manage_documents_gate = Depends(
        Permission("assets.manage_documents", scope_kind="workspace")
    )

    @api.get(
        "/{asset_id}/documents",
        response_model=AssetDocumentListResponse,
        operation_id="assets.documents.list",
        summary="List asset documents",
        dependencies=[view_gate],
    )
    def documents(
        asset_id: str,
        ctx: Ctx,
        session: Db,
        category: (
            Literal[
                "manual",
                "warranty",
                "invoice",
                "receipt",
                "photo",
                "certificate",
                "contract",
                "permit",
                "insurance",
                "other",
            ]
            | None
        ) = Query(default=None),
    ) -> AssetDocumentListResponse:
        try:
            views = list_documents(session, ctx, asset_id, category=category)
        except (AssetNotFound, AssetDocumentValidationError) as exc:
            raise http_for_document_error(exc) from exc
        return AssetDocumentListResponse(
            data=[AssetDocumentResponse.from_view(view) for view in views]
        )

    @api.post(
        "/{asset_id}/documents",
        response_model=AssetDocumentResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="assets.documents.upload",
        summary="Upload an asset document",
        dependencies=[manage_documents_gate],
    )
    async def upload_document(
        asset_id: str,
        ctx: Ctx,
        session: Db,
        storage: StorageDep,
        mime_sniffer: MimeSnifferDep,
        category: Annotated[str, Form()],
        title: Annotated[str | None, Form(max_length=200)] = None,
        notes_md: Annotated[str | None, Form(max_length=20_000)] = None,
        file: Annotated[UploadFile | None, File()] = None,
    ) -> AssetDocumentResponse:
        if file is None:
            raise HTTPException(
                status_code=422,
                detail={"error": "asset_document_file_required"},
            )
        try:
            declared_type = require_upload_content_type(
                file,
                missing=lambda: HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail={"error": "asset_document_content_type_missing"},
                ),
            )
        except HTTPException:
            await file.close()
            raise
        try:
            get_asset(session, ctx, asset_id=asset_id)
            if category not in ASSET_DOCUMENT_CATEGORIES:
                raise AssetDocumentValidationError("category", "invalid")
        except (AssetNotFound, AssetDocumentValidationError) as exc:
            await file.close()
            raise http_for_document_error(exc) from exc
        payload = await read_document_capped(file)
        sniffed_type = sniff_document_mime(
            mime_sniffer, payload, declared_type=declared_type
        )
        blob_hash = hashlib.sha256(payload).hexdigest()
        storage.put(blob_hash, io.BytesIO(payload), content_type=sniffed_type)
        try:
            view = attach_document(
                session,
                ctx,
                asset_id,
                blob_hash=blob_hash,
                filename=file.filename or "upload.bin",
                category=category,
                title=title,
                notes_md=notes_md,
                storage=storage,
            )
        except (AssetNotFound, AssetDocumentValidationError) as exc:
            raise http_for_document_error(exc) from exc
        return AssetDocumentResponse.from_view(view)

    @api.delete(
        "/documents/{document_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="assets.documents.delete",
        summary="Delete an asset document",
        dependencies=[manage_documents_gate],
    )
    def delete_asset_document(
        document_id: str,
        ctx: Ctx,
        session: Db,
    ) -> Response:
        try:
            delete_document(session, ctx, document_id)
        except (AssetNotFound, AssetDocumentNotFound) as exc:
            raise http_for_document_error(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return api
