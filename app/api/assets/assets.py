"""Tracked asset HTTP router."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session
from starlette.routing import NoMatchFound

from app.adapters.qr import render_qr
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.authz.dep import Permission
from app.domain.assets.assets import (
    AssetCreate,
    AssetNotFound,
    AssetPlacementInvalid,
    AssetQrTokenExhausted,
    AssetScanArchived,
    AssetTypeUnavailable,
    AssetUpdate,
    AssetValidationError,
    AssetView,
    archive_asset,
    create_asset,
    get_asset,
    get_asset_by_qr_token,
    list_assets,
    move_asset,
    regenerate_qr,
    restore_asset,
    update_asset,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "AssetCreateRequest",
    "AssetListResponse",
    "AssetMoveRequest",
    "AssetResponse",
    "AssetUpdateRequest",
    "build_asset_scan_router",
    "build_assets_router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


class AssetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type_id: str | None = None
    property_id: str
    area_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    label: str | None = Field(default=None, min_length=1, max_length=200)
    make: str | None = Field(default=None, max_length=160)
    model: str | None = Field(default=None, max_length=160)
    serial_number: str | None = Field(default=None, max_length=160)
    condition: Literal["new", "good", "fair", "poor", "needs_replacement"] = "good"
    status: Literal["active", "in_repair", "decommissioned", "disposed"] = "active"
    installed_on: date | None = None
    purchased_on: date | None = None
    purchased_at: date | None = None
    purchase_price_cents: int | None = Field(default=None, ge=0)
    purchase_currency: str | None = Field(default=None, min_length=3, max_length=3)
    purchase_vendor: str | None = Field(default=None, max_length=160)
    warranty_expires_on: date | None = None
    warranty_ends_at: date | None = None
    expected_lifespan_years: int | None = Field(default=None, ge=1)
    estimated_replacement_on: date | None = None
    cover_photo_file_id: str | None = None
    guest_visible: bool = False
    guest_instructions_md: str | None = Field(default=None, max_length=20_000)
    notes_md: str | None = Field(default=None, max_length=20_000)
    settings_override_json: dict[str, object] | None = None
    metadata: dict[str, object] | None = None

    @model_validator(mode="after")
    def _resolve_aliases(self) -> AssetCreateRequest:
        if (self.name is None) == (self.label is None):
            raise ValueError("send exactly one of name or label")
        if self.purchased_on is not None and self.purchased_at is not None:
            raise ValueError("send only one of purchased_on or purchased_at")
        if self.warranty_expires_on is not None and self.warranty_ends_at is not None:
            raise ValueError("send only one of warranty_expires_on or warranty_ends_at")
        if self.settings_override_json is not None and self.metadata is not None:
            raise ValueError("send only one of settings_override_json or metadata")
        return self

    def to_domain(self) -> AssetCreate:
        return AssetCreate(
            asset_type_id=self.asset_type_id,
            property_id=self.property_id,
            area_id=self.area_id,
            name=self.name if self.name is not None else self.label,
            make=self.make,
            model=self.model,
            serial_number=self.serial_number,
            condition=self.condition,
            status=self.status,
            installed_on=self.installed_on,
            purchased_on=(
                self.purchased_on
                if self.purchased_on is not None
                else self.purchased_at
            ),
            purchase_price_cents=self.purchase_price_cents,
            purchase_currency=self.purchase_currency,
            purchase_vendor=self.purchase_vendor,
            warranty_expires_on=(
                self.warranty_expires_on
                if self.warranty_expires_on is not None
                else self.warranty_ends_at
            ),
            expected_lifespan_years=self.expected_lifespan_years,
            estimated_replacement_on=self.estimated_replacement_on,
            cover_photo_file_id=self.cover_photo_file_id,
            guest_visible=self.guest_visible,
            guest_instructions_md=self.guest_instructions_md,
            notes_md=self.notes_md,
            settings_override_json=(
                self.settings_override_json
                if self.settings_override_json is not None
                else self.metadata
            ),
        )


class AssetUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type_id: str | None = None
    area_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    label: str | None = Field(default=None, min_length=1, max_length=200)
    make: str | None = Field(default=None, max_length=160)
    model: str | None = Field(default=None, max_length=160)
    serial_number: str | None = Field(default=None, max_length=160)
    condition: Literal["new", "good", "fair", "poor", "needs_replacement"] | None = None
    status: Literal["active", "in_repair", "decommissioned", "disposed"] | None = None
    installed_on: date | None = None
    purchased_on: date | None = None
    purchased_at: date | None = None
    purchase_price_cents: int | None = Field(default=None, ge=0)
    purchase_currency: str | None = Field(default=None, min_length=3, max_length=3)
    purchase_vendor: str | None = Field(default=None, max_length=160)
    warranty_expires_on: date | None = None
    warranty_ends_at: date | None = None
    expected_lifespan_years: int | None = Field(default=None, ge=1)
    estimated_replacement_on: date | None = None
    cover_photo_file_id: str | None = None
    guest_visible: bool | None = None
    guest_instructions_md: str | None = Field(default=None, max_length=20_000)
    notes_md: str | None = Field(default=None, max_length=20_000)
    settings_override_json: dict[str, object] | None = None
    metadata: dict[str, object] | None = None

    @model_validator(mode="after")
    def _resolve_aliases(self) -> AssetUpdateRequest:
        sent = self.model_fields_set
        if not sent:
            raise ValueError("PATCH body must include at least one field")
        if "name" in sent and "label" in sent:
            raise ValueError("send only one of name or label")
        if "purchased_on" in sent and "purchased_at" in sent:
            raise ValueError("send only one of purchased_on or purchased_at")
        if "warranty_expires_on" in sent and "warranty_ends_at" in sent:
            raise ValueError("send only one of warranty_expires_on or warranty_ends_at")
        if "settings_override_json" in sent and "metadata" in sent:
            raise ValueError("send only one of settings_override_json or metadata")
        return self

    def to_domain(self) -> AssetUpdate:
        payload: dict[str, object | None] = {}
        sent = self.model_fields_set
        if "name" in sent or "label" in sent:
            payload["name"] = self.name if "name" in sent else self.label
        if "purchased_on" in sent or "purchased_at" in sent:
            payload["purchased_on"] = (
                self.purchased_on if "purchased_on" in sent else self.purchased_at
            )
        if "warranty_expires_on" in sent or "warranty_ends_at" in sent:
            payload["warranty_expires_on"] = (
                self.warranty_expires_on
                if "warranty_expires_on" in sent
                else self.warranty_ends_at
            )
        if "settings_override_json" in sent or "metadata" in sent:
            payload["settings_override_json"] = (
                self.settings_override_json
                if "settings_override_json" in sent
                else self.metadata
            )
        for field_name in (
            "asset_type_id",
            "area_id",
            "make",
            "model",
            "serial_number",
            "condition",
            "status",
            "installed_on",
            "purchase_price_cents",
            "purchase_currency",
            "purchase_vendor",
            "expected_lifespan_years",
            "estimated_replacement_on",
            "cover_photo_file_id",
            "guest_visible",
            "guest_instructions_md",
            "notes_md",
        ):
            if field_name in sent:
                payload[field_name] = getattr(self, field_name)
        return AssetUpdate.model_validate(payload)


class AssetMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_id: str
    area_id: str | None = None


class AssetResponse(BaseModel):
    id: str
    workspace_id: str
    property_id: str
    area_id: str | None
    asset_type_id: str | None
    name: str
    label: str
    make: str | None
    model: str | None
    serial_number: str | None
    condition: str
    status: str
    installed_on: date | None
    purchased_on: date | None
    purchased_at: date | None
    purchase_price_cents: int | None
    purchase_currency: str | None
    purchase_vendor: str | None
    warranty_expires_on: date | None
    warranty_ends_at: date | None
    expected_lifespan_years: int | None
    estimated_replacement_on: date | None
    cover_photo_file_id: str | None
    qr_token: str
    qr_code: str
    guest_visible: bool
    guest_instructions_md: str | None
    notes_md: str | None
    settings_override_json: dict[str, object] | None
    metadata: dict[str, object] | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    archived_at: datetime | None

    @classmethod
    def from_view(cls, view: AssetView) -> AssetResponse:
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            property_id=view.property_id,
            area_id=view.area_id,
            asset_type_id=view.asset_type_id,
            name=view.name,
            label=view.name,
            make=view.make,
            model=view.model,
            serial_number=view.serial_number,
            condition=view.condition,
            status=view.status,
            installed_on=view.installed_on,
            purchased_on=view.purchased_on,
            purchased_at=view.purchased_on,
            purchase_price_cents=view.purchase_price_cents,
            purchase_currency=view.purchase_currency,
            purchase_vendor=view.purchase_vendor,
            warranty_expires_on=view.warranty_expires_on,
            warranty_ends_at=view.warranty_expires_on,
            expected_lifespan_years=view.expected_lifespan_years,
            estimated_replacement_on=view.estimated_replacement_on,
            cover_photo_file_id=view.cover_photo_file_id,
            qr_token=view.qr_token,
            qr_code=view.qr_token,
            guest_visible=view.guest_visible,
            guest_instructions_md=view.guest_instructions_md,
            notes_md=view.notes_md,
            settings_override_json=view.settings_override_json,
            metadata=view.settings_override_json,
            created_at=view.created_at,
            updated_at=view.updated_at,
            deleted_at=view.deleted_at,
            archived_at=view.deleted_at,
        )


class AssetListResponse(BaseModel):
    data: list[AssetResponse]
    next_cursor: str | None = None
    has_more: bool = False


def _http_for_asset_error(exc: Exception) -> HTTPException:
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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "asset_type_unavailable", "message": str(exc)},
        )
    if isinstance(exc, AssetPlacementInvalid):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "asset_placement_invalid", "message": str(exc)},
        )
    if isinstance(exc, AssetValidationError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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


def _scan_asset(
    qr_token: str,
    ctx: WorkspaceContext,
    session: Session,
) -> AssetResponse:
    try:
        view = get_asset_by_qr_token(session, ctx, qr_token=qr_token)
    except (AssetNotFound, AssetScanArchived) as exc:
        raise _http_for_asset_error(exc) from exc
    return AssetResponse.from_view(view)


def build_asset_scan_router() -> APIRouter:
    api = APIRouter(tags=["assets"])
    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))

    @api.get(
        "/scan/{qr_token}",
        response_model=AssetResponse,
        operation_id="asset.scan",
        name="asset.scan",
        summary="Resolve an asset QR token",
        dependencies=[view_gate],
    )
    def scan(qr_token: str, ctx: _Ctx, session: _Db) -> AssetResponse:
        return _scan_asset(qr_token, ctx, session)

    return api


def build_assets_router() -> APIRouter:
    api = APIRouter(tags=["assets"])

    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))
    edit_gate = Depends(Permission("assets.edit", scope_kind="workspace"))

    @api.get(
        "/",
        response_model=AssetListResponse,
        operation_id="assets.list",
        summary="List tracked assets",
        dependencies=[view_gate],
    )
    def list_(
        ctx: _Ctx,
        session: _Db,
        property_id: str | None = Query(default=None),
        area_id: str | None = Query(default=None),
        status_: str | None = Query(default=None, alias="status"),
        condition: str | None = Query(default=None),
        asset_type_id: str | None = Query(default=None),
        q: str | None = Query(default=None),
        include_archived: bool = Query(default=False),
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> AssetListResponse:
        views = list_assets(
            session,
            ctx,
            property_id=property_id,
            area_id=area_id,
            status=status_,
            condition=condition,
            asset_type_id=asset_type_id,
            q=q,
            include_archived=include_archived,
            after_id=decode_cursor(cursor),
            limit=limit + 1,
        )
        page = paginate(views, limit=limit, key_getter=lambda view: view.id)
        return AssetListResponse(
            data=[AssetResponse.from_view(view) for view in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @api.post(
        "/",
        response_model=AssetResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="assets.create",
        summary="Create a tracked asset",
        dependencies=[edit_gate],
    )
    def create(
        body: AssetCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> AssetResponse:
        try:
            view = create_asset(session, ctx, body=body.to_domain())
        except (
            AssetPlacementInvalid,
            AssetQrTokenExhausted,
            AssetTypeUnavailable,
            AssetValidationError,
        ) as exc:
            raise _http_for_asset_error(exc) from exc
        return AssetResponse.from_view(view)

    @api.get(
        "/scan/{qr_token}",
        response_model=AssetResponse,
        operation_id="assets.scan",
        name="assets.scan",
        summary="Resolve an asset QR token",
        dependencies=[view_gate],
    )
    def scan(qr_token: str, ctx: _Ctx, session: _Db) -> AssetResponse:
        return _scan_asset(qr_token, ctx, session)

    @api.get(
        "/{asset_id}",
        response_model=AssetResponse,
        operation_id="assets.get",
        summary="Get one tracked asset",
        dependencies=[view_gate],
    )
    def get(
        asset_id: str,
        ctx: _Ctx,
        session: _Db,
        include_archived: bool = Query(default=False),
    ) -> AssetResponse:
        try:
            view = get_asset(
                session,
                ctx,
                asset_id=asset_id,
                include_archived=include_archived,
            )
        except AssetNotFound as exc:
            raise _http_for_asset_error(exc) from exc
        return AssetResponse.from_view(view)

    @api.patch(
        "/{asset_id}",
        response_model=AssetResponse,
        operation_id="assets.update",
        summary="Update a tracked asset",
        dependencies=[edit_gate],
    )
    def patch(
        asset_id: str,
        body: AssetUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> AssetResponse:
        try:
            view = update_asset(session, ctx, asset_id, body=body.to_domain())
        except (
            AssetNotFound,
            AssetPlacementInvalid,
            AssetTypeUnavailable,
            AssetValidationError,
        ) as exc:
            raise _http_for_asset_error(exc) from exc
        return AssetResponse.from_view(view)

    @api.delete(
        "/{asset_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="assets.delete",
        summary="Archive a tracked asset",
        dependencies=[edit_gate],
    )
    def delete_(asset_id: str, ctx: _Ctx, session: _Db) -> Response:
        try:
            archive_asset(session, ctx, asset_id=asset_id)
        except AssetNotFound as exc:
            raise _http_for_asset_error(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.put(
        "/{asset_id}/restore",
        response_model=AssetResponse,
        operation_id="assets.restore",
        summary="Restore an archived asset",
        dependencies=[edit_gate],
    )
    def restore(asset_id: str, ctx: _Ctx, session: _Db) -> AssetResponse:
        try:
            view = restore_asset(session, ctx, asset_id=asset_id)
        except (
            AssetNotFound,
            AssetPlacementInvalid,
            AssetTypeUnavailable,
        ) as exc:
            raise _http_for_asset_error(exc) from exc
        return AssetResponse.from_view(view)

    @api.post(
        "/{asset_id}/move",
        response_model=AssetResponse,
        operation_id="assets.move",
        summary="Move an asset to a property or area",
        dependencies=[edit_gate],
    )
    def move(
        asset_id: str,
        body: AssetMoveRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> AssetResponse:
        try:
            view = move_asset(
                session,
                ctx,
                asset_id,
                property_id=body.property_id,
                area_id=body.area_id,
            )
        except (AssetNotFound, AssetPlacementInvalid) as exc:
            raise _http_for_asset_error(exc) from exc
        return AssetResponse.from_view(view)

    @api.post(
        "/{asset_id}/regenerate_qr",
        response_model=AssetResponse,
        operation_id="assets.regenerate_qr",
        summary="Regenerate an asset QR token",
        dependencies=[edit_gate],
    )
    def regenerate(asset_id: str, ctx: _Ctx, session: _Db) -> AssetResponse:
        try:
            view = regenerate_qr(session, ctx, asset_id)
        except (AssetNotFound, AssetQrTokenExhausted, AssetValidationError) as exc:
            raise _http_for_asset_error(exc) from exc
        return AssetResponse.from_view(view)

    @api.get(
        "/{asset_id}/qr.png",
        operation_id="assets.qr_png",
        summary="Render an asset QR code as PNG",
        dependencies=[view_gate],
    )
    def qr_png(asset_id: str, request: Request, ctx: _Ctx, session: _Db) -> Response:
        try:
            view = get_asset(session, ctx, asset_id=asset_id)
        except AssetNotFound as exc:
            raise _http_for_asset_error(exc) from exc
        url_params = {"qr_token": view.qr_token}
        if "slug" in request.path_params:
            url_params["slug"] = request.path_params["slug"]
        try:
            scan_url = str(request.url_for("asset.scan", **url_params))
        except NoMatchFound:
            scan_url = str(request.url_for("assets.scan", **url_params))
        return Response(
            content=render_qr(scan_url, label=view.name),
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    return api
