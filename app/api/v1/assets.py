"""Assets context router."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.assets import build_asset_types_router

router = APIRouter(tags=["assets"])
router.include_router(build_asset_types_router())

__all__ = ["router"]
