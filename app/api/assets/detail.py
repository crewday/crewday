"""Asset detail composition.

Helpers that turn an asset row + related rows (asset type, area,
property, actions, documents) into the ``GET /assets/{asset_id}``
detail payload. Imported by ``assets.py`` for the detail endpoint and
by ``actions.py`` for the default-action seam in the complete-action
flow.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.assets.models import AssetType as AssetTypeRow
from app.adapters.db.places.models import Area, Property, PropertyWorkspace
from app.api.assets.actions import asset_detail_actions
from app.api.assets.schemas import (
    AssetDetailAssetResponse,
    AssetDetailAssetTypeResponse,
    AssetDetailDocumentResponse,
    AssetDetailPropertyResponse,
    AssetDetailResponse,
)
from app.domain.assets.actions import list_actions
from app.domain.assets.assets import AssetNotFound, AssetView, get_asset
from app.domain.assets.documents import list_documents
from app.domain.assets.types import AssetTypeNotFound, AssetTypeView, get_type
from app.tenancy import WorkspaceContext, tenant_agnostic

__all__ = [
    "asset_detail",
    "asset_type_categories",
    "asset_type_for",
]


_PROPERTY_COLOR_PALETTE: tuple[Literal["moss", "sky", "rust"], ...] = (
    "moss",
    "sky",
    "rust",
)


def asset_detail(
    session: Session,
    ctx: WorkspaceContext,
    *,
    asset_id: str,
    include_archived: bool = False,
) -> AssetDetailResponse:
    asset = get_asset(
        session,
        ctx,
        asset_id=asset_id,
        include_archived=include_archived,
    )
    asset_type = asset_type_for(session, ctx, asset)
    documents = list_documents(session, ctx, asset_id=asset.id)
    action_views = list_actions(session, ctx, asset.id)
    return AssetDetailResponse(
        asset=AssetDetailAssetResponse.from_view(
            asset,
            area_label=_area_label_for(session, asset.area_id),
        ),
        asset_type=(
            AssetDetailAssetTypeResponse.from_view(asset_type)
            if asset_type is not None
            else None
        ),
        property=_asset_property_for(session, ctx, asset.property_id),
        actions=asset_detail_actions(asset, asset_type, action_views),
        documents=[
            AssetDetailDocumentResponse.from_view(
                document,
                property_id=asset.property_id,
            )
            for document in documents
        ],
        linked_tasks=[],
    )


def asset_type_for(
    session: Session,
    ctx: WorkspaceContext,
    asset: AssetView,
) -> AssetTypeView | None:
    if asset.asset_type_id is None:
        return None
    try:
        return get_type(session, ctx, type_id=asset.asset_type_id)
    except AssetTypeNotFound:
        return None


def _area_label_for(session: Session, area_id: str | None) -> str | None:
    if area_id is None:
        return None
    with tenant_agnostic():
        row = session.get(Area, area_id)
    return row.label if row is not None else None


def _asset_property_for(
    session: Session,
    ctx: WorkspaceContext,
    property_id: str,
) -> AssetDetailPropertyResponse:
    stmt = (
        select(Property)
        .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
        .where(
            Property.id == property_id,
            PropertyWorkspace.workspace_id == ctx.workspace_id,
            Property.deleted_at.is_(None),
        )
    )
    row = session.scalar(stmt)
    if row is None:
        raise AssetNotFound()
    areas = _property_area_labels(session, property_id)
    return AssetDetailPropertyResponse(
        id=row.id,
        name=row.name if row.name is not None else row.address,
        city=_city_for(row.address_json),
        timezone=row.timezone,
        color=_property_color_for(row.id),
        kind=_property_kind(row.kind),
        areas=areas,
        evidence_policy="inherit",
        country=row.country if row.country else "XX",
        locale=row.locale if row.locale is not None else "",
        settings_override={},
        client_org_id=None,
        owner_user_id=None,
    )


def _property_area_labels(session: Session, property_id: str) -> list[str]:
    stmt = (
        select(Area.label)
        .where(Area.property_id == property_id)
        .order_by(Area.ordering.asc(), Area.label.asc())
    )
    return list(session.scalars(stmt).all())


def _city_for(address_json: dict[str, Any] | None) -> str:
    if not address_json:
        return ""
    raw = address_json.get("city")
    return raw if isinstance(raw, str) else ""


def _property_color_for(property_id: str) -> Literal["moss", "sky", "rust"]:
    digest = hashlib.sha256(property_id.encode("utf-8")).digest()
    return _PROPERTY_COLOR_PALETTE[digest[0] % len(_PROPERTY_COLOR_PALETTE)]


def _property_kind(value: str) -> Literal["str", "vacation", "residence", "mixed"]:
    if value == "str":
        return "str"
    if value == "vacation":
        return "vacation"
    if value == "residence":
        return "residence"
    if value == "mixed":
        return "mixed"
    raise ValueError(f"unknown property kind {value!r}")


def asset_type_categories(session: Session, workspace_id: str) -> dict[str, str]:
    with tenant_agnostic():
        rows = session.execute(
            select(AssetTypeRow.id, AssetTypeRow.category).where(
                (AssetTypeRow.workspace_id.is_(None))
                | (AssetTypeRow.workspace_id == workspace_id)
            )
        ).all()
    return {str(row.id): str(row.category) for row in rows}
