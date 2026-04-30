"""Assets context router."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.assets import (
    build_asset_scan_router,
    build_asset_types_router,
    build_assets_router,
    build_documents_router,
)

router = APIRouter(tags=["assets"])
router.include_router(build_asset_types_router())
router.include_router(build_assets_router())

scan_router = APIRouter(tags=["assets"])
scan_router.include_router(build_asset_scan_router())

documents_router = APIRouter(tags=["assets"])
documents_router.include_router(build_documents_router())

__all__ = ["documents_router", "router", "scan_router"]
