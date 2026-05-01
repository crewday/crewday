"""Shared helpers for the asset HTTP routers.

Type aliases, error-response shapes, exception → ``HTTPException``
mappers, MIME-sniff constants, and a couple of cross-module utilities
used by every focused asset router (``assets``, ``actions``,
``documents``, ``scan``).
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.adapters.storage.ports import MimeSniffer, Storage
from app.api.deps import (
    current_workspace_context,
    db_session,
    get_mime_sniffer,
    get_storage,
)
from app.api.uploads import (
    read_upload_capped,
    sniff_allowed_upload_mime,
)
from app.domain.assets.actions import (
    AssetActionAccessDenied,
    AssetActionNotFound,
    AssetActionValidationError,
)
from app.domain.assets.assets import (
    AssetNotFound,
    AssetPlacementInvalid,
    AssetQrTokenExhausted,
    AssetScanArchived,
    AssetTypeUnavailable,
    AssetValidationError,
)
from app.domain.assets.documents import (
    AssetDocumentNotFound,
    AssetDocumentValidationError,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "ASSET_DOCUMENT_ALLOWED_MIME",
    "ASSET_ERROR_RESPONSES",
    "MAX_ASSET_DOCUMENT_BYTES",
    "Ctx",
    "Db",
    "MimeSnifferDep",
    "StorageDep",
    "asset_scan_web_url",
    "http_for_action_error",
    "http_for_asset_error",
    "http_for_document_error",
    "read_document_capped",
    "sniff_document_mime",
    "storage_from_request",
]


Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
Db = Annotated[Session, Depends(db_session)]
StorageDep = Annotated[Storage, Depends(get_storage)]
MimeSnifferDep = Annotated[MimeSniffer, Depends(get_mime_sniffer)]


ASSET_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "Permission denied or CSRF mismatch"},
    404: {"description": "Asset resource not found"},
    409: {"description": "Asset conflict"},
    410: {"description": "Archived asset"},
}


MAX_ASSET_DOCUMENT_BYTES = 25 * 1024 * 1024
ASSET_DOCUMENT_ALLOWED_MIME = {
    "application/pdf",
    "application/zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/plain",
}


def http_for_asset_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AssetNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "asset_not_found"},
        )
    if isinstance(exc, AssetScanArchived):
        return HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"error": "asset_archived"},
        )
    if isinstance(exc, AssetTypeUnavailable):
        return HTTPException(
            status_code=422,
            detail={"error": "asset_type_unavailable", "message": str(exc)},
        )
    if isinstance(exc, AssetPlacementInvalid):
        return HTTPException(
            status_code=422,
            detail={"error": "asset_placement_invalid", "message": str(exc)},
        )
    if isinstance(exc, AssetValidationError):
        return HTTPException(
            status_code=422,
            detail={"error": exc.error, "field": exc.field},
        )
    if isinstance(exc, AssetQrTokenExhausted):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "qr_token_exhausted"},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


def http_for_action_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AssetNotFound | AssetActionNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "asset_action_not_found"},
        )
    if isinstance(exc, AssetActionAccessDenied):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "permission_denied", "action_key": "assets.record_action"},
        )
    if isinstance(exc, AssetActionValidationError):
        return HTTPException(
            status_code=422,
            detail={"error": exc.error, "field": exc.field},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


def http_for_document_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AssetNotFound | AssetDocumentNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "asset_document_not_found"},
        )
    if isinstance(exc, AssetDocumentValidationError):
        return HTTPException(
            status_code=422,
            detail={"error": exc.error, "field": exc.field},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


def storage_from_request(request: Request) -> Storage:
    storage: Storage | None = getattr(request.app.state, "storage", None)
    if storage is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "storage_unavailable"},
        )
    return storage


def asset_scan_web_url(request: Request, ctx: WorkspaceContext, qr_token: str) -> str:
    slug = str(request.path_params.get("slug") or ctx.workspace_slug)
    return (
        str(request.base_url).rstrip("/")
        + f"/w/{quote(slug, safe='')}/asset/scan/{quote(qr_token, safe='')}"
    )


async def read_document_capped(upload: UploadFile) -> bytes:
    return await read_upload_capped(
        upload,
        max_bytes=MAX_ASSET_DOCUMENT_BYTES,
        too_large=lambda: HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={
                "error": "asset_document_too_large",
                "message": f"upload exceeds the {MAX_ASSET_DOCUMENT_BYTES}-byte cap",
            },
        ),
    )


def sniff_document_mime(
    mime_sniffer: MimeSniffer,
    payload: bytes,
    *,
    declared_type: str,
) -> str:
    def _plain_text_fallback(payload: bytes, declared_type: str) -> str | None:
        if not declared_type.lower().startswith("text/"):
            return None
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return "text/plain"

    return sniff_allowed_upload_mime(
        mime_sniffer,
        payload,
        declared_type=declared_type,
        allowed=ASSET_DOCUMENT_ALLOWED_MIME,
        rejected=lambda sniffed: HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "error": "asset_document_content_type_rejected",
                "content_type": sniffed,
                "declared_type": declared_type,
            },
        ),
        fallback=_plain_text_fallback,
    )
