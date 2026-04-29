"""Assets context router."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.assets import (
    build_asset_scan_router,
    build_asset_types_router,
    build_assets_router,
)

router = APIRouter(tags=["assets"])
router.include_router(build_asset_types_router())
router.include_router(build_assets_router())

scan_router = APIRouter(tags=["assets"])
scan_router.include_router(build_asset_scan_router())

__all__ = ["router", "scan_router"]
