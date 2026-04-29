"""assets — physical equipment, maintenance actions, and documents.

All asset tables are workspace-scoped. ``asset_type`` permits
``workspace_id = NULL`` for system-seeded catalog rows, but manager-
authored rows carry a workspace id and the table is still registered
so tenant-aware queries filter custom rows. System catalog queries
must opt into :func:`app.tenancy.tenant_agnostic` and add their own
``workspace_id IS NULL OR workspace_id = ...`` predicate.

See ``docs/specs/02-domain-model.md`` §"Assets" and
``docs/specs/21-assets.md``.
"""

from __future__ import annotations

from app.adapters.db.assets.models import Asset, AssetAction, AssetDocument, AssetType
from app.tenancy.registry import register

for _table in ("asset_type", "asset", "asset_action", "asset_document"):
    register(_table)

__all__ = [
    "BASE_ASSET_TYPE_CATALOG",
    "Asset",
    "AssetAction",
    "AssetDocument",
    "AssetType",
    "seed_asset_type_catalog",
]


def __getattr__(name: str) -> object:
    if name in {"BASE_ASSET_TYPE_CATALOG", "seed_asset_type_catalog"}:
        from app.adapters.db.assets import bootstrap

        return getattr(bootstrap, name)
    raise AttributeError(name)
