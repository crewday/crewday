"""Shared helpers for the asset HTTP routers.

Type aliases, error-response shapes, exception → domain-error
mappers, MIME-sniff constants, and a couple of cross-module utilities
used by every focused asset router (``assets``, ``actions``,
``documents``, ``scan``).
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, Request, UploadFile
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
from app.domain.assets.extraction import ExtractionRetryNotAllowed
from app.domain.errors import (
    Conflict,
    DomainError,
    Forbidden,
    Gone,
    Internal,
    NotFound,
    PayloadTooLarge,
    ServiceUnavailable,
    UnsupportedMediaType,
    Validation,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "ASSET_DOCUMENT_ALLOWED_MIME",
    "ASSET_ERROR_RESPONSES",
    "MAX_ASSET_DOCUMENT_BYTES",
    "PROBLEM_JSON_RESPONSES",
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


# RFC 7807 problem+json envelope schema. Every asset route emits 4xx
# errors via :mod:`app.api.errors` (the FastAPI exception handlers
# rewrite domain errors into ``application/problem+json``); the
# default FastAPI 422 schema documents ``application/json`` +
# ``HTTPValidationError`` which is a lie on this codebase. Declaring
# the envelope on each route lets the schemathesis contract gate
# accept the actual response shape.
_PROBLEM_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "detail": {"type": "string"},
        "instance": {"type": "string"},
        "errors": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["type", "title", "status", "instance"],
    "additionalProperties": True,
}


def _problem_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/problem+json": {"schema": _PROBLEM_JSON_SCHEMA}},
    }


# Asset error responses: every route inherits this set so 403/404/409/
# 410/422 are documented with the true ``application/problem+json``
# envelope. Routes append codes (e.g. 503 for QR exhaustion) via the
# ``responses=`` kwarg on the individual decorator.
ASSET_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: _problem_response("Permission denied or CSRF mismatch"),
    404: _problem_response("Asset resource not found"),
    409: _problem_response("Asset conflict"),
    410: _problem_response("Archived asset"),
    422: _problem_response("Validation error"),
}


# Subset of :data:`ASSET_ERROR_RESPONSES` covering only the validation
# response. Used by routes that want to override their FastAPI-default
# 422 schema without inheriting the full asset 4xx set (asset_types
# router carries its own ``responses=`` map).
PROBLEM_JSON_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: _problem_response("Validation error"),
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


def http_for_asset_error(exc: Exception) -> DomainError:
    if isinstance(exc, AssetNotFound):
        return NotFound(extra={"error": "asset_not_found"})
    if isinstance(exc, AssetScanArchived):
        return Gone(extra={"error": "asset_archived"})
    if isinstance(exc, AssetTypeUnavailable):
        message = str(exc)
        return Validation(
            message,
            extra={"error": "asset_type_unavailable", "message": message},
        )
    if isinstance(exc, AssetPlacementInvalid):
        # Property / area lookup miss: surface as 404 (parent resource
        # not addressable from this workspace) rather than 422 so the
        # contract gate's positive-data-acceptance check sees the
        # documented "missing parent" branch instead of treating the
        # rejection as a schema-validation failure (schemathesis pins
        # 422 for schema-validation only).
        message = str(exc)
        return NotFound(
            message,
            extra={"error": "asset_placement_invalid", "message": message},
        )
    if isinstance(exc, AssetValidationError):
        return Validation(extra={"error": exc.error, "field": exc.field})
    if isinstance(exc, AssetQrTokenExhausted):
        return ServiceUnavailable(extra={"error": "qr_token_exhausted"})
    return Internal(extra={"error": "internal"})


def http_for_action_error(exc: Exception) -> DomainError:
    if isinstance(exc, AssetNotFound | AssetActionNotFound):
        return NotFound(extra={"error": "asset_action_not_found"})
    if isinstance(exc, AssetActionAccessDenied):
        return Forbidden(
            extra={"error": "permission_denied", "action_key": "assets.record_action"}
        )
    if isinstance(exc, AssetActionValidationError):
        return Validation(extra={"error": exc.error, "field": exc.field})
    return Internal(extra={"error": "internal"})


def http_for_document_error(exc: Exception) -> DomainError:
    if isinstance(exc, AssetNotFound | AssetDocumentNotFound):
        return NotFound(extra={"error": "asset_document_not_found"})
    if isinstance(exc, AssetDocumentValidationError):
        return Validation(extra={"error": exc.error, "field": exc.field})
    if isinstance(exc, ExtractionRetryNotAllowed):
        # Retry endpoint hit on a row that is not in ``failed`` (the
        # only retry-eligible state). 409 matches the §21 contract
        # ("Retrying a non-failed row returns 409 with
        # ``error=asset_document_extraction_not_retryable``"). The
        # ``message`` key flows into the envelope's ``detail``; the
        # ``error`` key carries the structured symbol.
        message = str(exc)
        return Conflict(
            message,
            extra={
                "error": "asset_document_extraction_not_retryable",
                "message": message,
            },
        )
    return Internal(extra={"error": "internal"})


def storage_from_request(request: Request) -> Storage:
    storage: Storage | None = getattr(request.app.state, "storage", None)
    if storage is None:
        raise ServiceUnavailable(extra={"error": "storage_unavailable"})
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
        too_large=lambda: PayloadTooLarge(
            f"upload exceeds the {MAX_ASSET_DOCUMENT_BYTES}-byte cap",
            extra={
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
        rejected=lambda sniffed: UnsupportedMediaType(
            extra={
                "error": "asset_document_content_type_rejected",
                "content_type": sniffed,
                "declared_type": declared_type,
            },
        ),
        fallback=_plain_text_fallback,
    )
