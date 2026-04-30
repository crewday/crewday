"""Assets API subrouters."""

from __future__ import annotations

from app.api.assets.assets import (
    build_asset_scan_router,
    build_assets_alias_router,
    build_assets_router,
    build_documents_router,
)
from app.api.assets.types import (
    build_asset_types_alias_router,
    build_asset_types_router,
)

__all__ = [
    "build_asset_scan_router",
    "build_asset_types_alias_router",
    "build_asset_types_router",
    "build_assets_alias_router",
    "build_assets_router",
    "build_documents_router",
]
