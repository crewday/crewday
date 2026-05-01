"""Asset QR scan endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.assets._shared import (
    ASSET_ERROR_RESPONSES,
    Ctx,
    Db,
    http_for_asset_error,
)
from app.api.assets.schemas import AssetResponse
from app.authz.dep import Permission
from app.domain.assets.assets import (
    AssetNotFound,
    AssetScanArchived,
    get_asset_by_qr_token,
)
from app.tenancy import WorkspaceContext

__all__ = ["build_asset_scan_router", "scan_asset"]


def scan_asset(
    qr_token: str,
    ctx: WorkspaceContext,
    session: Session,
) -> AssetResponse:
    try:
        view = get_asset_by_qr_token(session, ctx, qr_token=qr_token)
    except (AssetNotFound, AssetScanArchived) as exc:
        raise http_for_asset_error(exc) from exc
    return AssetResponse.from_view(view)


def build_asset_scan_router() -> APIRouter:
    api = APIRouter(tags=["assets"], responses=ASSET_ERROR_RESPONSES)
    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))

    @api.get(
        "/scan/{qr_token}",
        response_model=AssetResponse,
        operation_id="asset.scan",
        name="asset.scan",
        summary="Resolve an asset QR token",
        dependencies=[view_gate],
    )
    def scan(qr_token: str, ctx: Ctx, session: Db) -> AssetResponse:
        return scan_asset(qr_token, ctx, session)

    return api
