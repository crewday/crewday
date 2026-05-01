"""Tracked asset HTTP router — core CRUD + listing.

Owns the asset CRUD surface and pulls in the asset-action and
asset-document sub-routers so the public ``/assets/...`` paths are
unchanged.

Sibling modules:

- ``actions``: asset-action endpoints (record/list/complete/...).
- ``documents``: asset/document endpoints + workspace documents +
  extraction placeholder.
- ``scan``: ``/scan/{qr_token}`` resolver.
- ``detail``: detail composition shared by ``GET /{asset_id}``.
- ``schemas``: pydantic request/response models.
- ``_shared``: cross-module helpers (deps, error mappers, MIME utils).
"""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Sequence
from html import escape

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.orm import Session
from weasyprint import HTML

from app.adapters.qr import render_qr
from app.api.assets._shared import (
    ASSET_ERROR_RESPONSES,
    Ctx,
    Db,
    asset_scan_web_url,
    http_for_asset_error,
)
from app.api.assets.actions import build_asset_actions_subrouter
from app.api.assets.detail import asset_detail, asset_type_categories
from app.api.assets.documents import (
    build_asset_documents_subrouter,
    build_documents_router,  # re-exported for back-compat: see ``__all__``
)
from app.api.assets.scan import (
    build_asset_scan_router,  # re-exported for back-compat: see ``__all__``
    scan_asset,
)
from app.api.assets.schemas import (
    AssetCreateRequest,
    AssetDetailResponse,
    AssetListResponse,
    AssetMoveRequest,
    AssetResponse,
    AssetUpdateRequest,
)
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.authz.dep import Permission
from app.domain.assets.assets import (
    AssetNotFound,
    AssetPlacementInvalid,
    AssetQrTokenExhausted,
    AssetTypeUnavailable,
    AssetValidationError,
    AssetView,
    archive_asset,
    create_asset,
    get_asset,
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
    "build_assets_alias_router",
    "build_assets_router",
    "build_documents_router",
]


def _asset_list_response(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str | None,
    area_id: str | None,
    status_: str | None,
    condition: str | None,
    asset_type_id: str | None,
    q: str | None,
    include_archived: bool,
    cursor: str | None,
    limit: int,
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


def _render_qr_sheet_pdf(
    request: Request,
    ctx: WorkspaceContext,
    views: Sequence[AssetView],
) -> bytes:
    cards = "\n".join(_qr_sheet_card(request, ctx, view) for view in views)
    html = f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <style>
      @page {{ size: A4; margin: 12mm; }}
      body {{ font-family: sans-serif; color: #1f2933; }}
      .sheet {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8mm; }}
      .label {{ border: 1px solid #ccd3ca; padding: 6mm; break-inside: avoid; }}
      .label img {{ width: 34mm; height: 34mm; display: block; margin-bottom: 3mm; }}
      .label strong {{ display: block; font-size: 12pt; }}
      .label span {{ display: block; font-size: 8pt; color: #5f6b5b; }}
    </style>
  </head>
  <body>
    <main class="sheet">{cards}</main>
  </body>
</html>
"""
    return bytes(HTML(string=html).write_pdf())


def _qr_sheet_card(request: Request, ctx: WorkspaceContext, view: AssetView) -> str:
    scan_url = asset_scan_web_url(request, ctx, view.qr_token)
    qr_png = b64encode(render_qr(scan_url, label=view.name)).decode("ascii")
    return (
        '<article class="label">'
        f'<img alt="" src="data:image/png;base64,{qr_png}">'
        f"<strong>{escape(view.name)}</strong>"
        f"<span>{escape(ctx.workspace_slug)} / {escape(view.id)}</span>"
        "</article>"
    )


def build_assets_alias_router() -> APIRouter:
    api = APIRouter(tags=["assets"], responses=ASSET_ERROR_RESPONSES)
    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))
    edit_gate = Depends(Permission("assets.edit", scope_kind="workspace"))

    @api.get(
        "/assets",
        response_model=AssetListResponse,
        operation_id="assets.list_flat",
        summary="List tracked assets",
        dependencies=[view_gate],
    )
    def list_flat(
        ctx: Ctx,
        session: Db,
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
        return _asset_list_response(
            session,
            ctx,
            property_id=property_id,
            area_id=area_id,
            status_=status_,
            condition=condition,
            asset_type_id=asset_type_id,
            q=q,
            include_archived=include_archived,
            cursor=cursor,
            limit=limit,
        )

    @api.post(
        "/assets",
        response_model=AssetResponse,
        status_code=status.HTTP_201_CREATED,
        include_in_schema=False,
        dependencies=[edit_gate],
    )
    def create_flat(
        body: AssetCreateRequest,
        ctx: Ctx,
        session: Db,
    ) -> AssetResponse:
        try:
            view = create_asset(session, ctx, body=body.to_domain())
        except (
            AssetPlacementInvalid,
            AssetQrTokenExhausted,
            AssetTypeUnavailable,
            AssetValidationError,
        ) as exc:
            raise http_for_asset_error(exc) from exc
        return AssetResponse.from_view(view)

    return api


def build_assets_router() -> APIRouter:
    api = APIRouter(tags=["assets"], responses=ASSET_ERROR_RESPONSES)

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
        ctx: Ctx,
        session: Db,
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
        return _asset_list_response(
            session,
            ctx,
            property_id=property_id,
            area_id=area_id,
            status_=status_,
            condition=condition,
            asset_type_id=asset_type_id,
            q=q,
            include_archived=include_archived,
            cursor=cursor,
            limit=limit,
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
        ctx: Ctx,
        session: Db,
    ) -> AssetResponse:
        try:
            view = create_asset(session, ctx, body=body.to_domain())
        except (
            AssetPlacementInvalid,
            AssetQrTokenExhausted,
            AssetTypeUnavailable,
            AssetValidationError,
        ) as exc:
            raise http_for_asset_error(exc) from exc
        return AssetResponse.from_view(view)

    @api.get(
        "/qr-sheet",
        operation_id="assets.qr_sheet",
        summary="Render asset QR labels as a print-ready PDF",
        dependencies=[view_gate],
        # Document the actual ``application/pdf`` body so the contract
        # gate stops flagging the response Content-Type as undocumented.
        responses={
            200: {
                "description": "PDF QR-label sheet",
                "content": {
                    "application/pdf": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            },
        },
    )
    def qr_sheet(
        request: Request,
        ctx: Ctx,
        session: Db,
        property_id: str | None = Query(default=None),
        category: str | None = Query(default=None),
    ) -> Response:
        views = list_assets(
            session,
            ctx,
            property_id=property_id,
            limit=500,
        )
        if category:
            type_categories = asset_type_categories(session, ctx.workspace_id)
            views = [
                view
                for view in views
                if view.asset_type_id
                and type_categories.get(view.asset_type_id) == category
            ]
        pdf = _render_qr_sheet_pdf(request, ctx, views)
        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": 'inline; filename="asset-qr-labels.pdf"',
            },
        )

    @api.get(
        "/scan/{qr_token}",
        response_model=AssetResponse,
        operation_id="assets.scan",
        name="assets.scan",
        summary="Resolve an asset QR token",
        dependencies=[view_gate],
    )
    def scan(qr_token: str, ctx: Ctx, session: Db) -> AssetResponse:
        return scan_asset(qr_token, ctx, session)

    # Action and document sub-routers — paths and operation IDs match
    # the pre-split layout because both sub-routers are mounted under
    # the same prefix as ``api`` (no extra prefix).
    api.include_router(build_asset_actions_subrouter())
    api.include_router(build_asset_documents_subrouter())

    @api.get(
        "/{asset_id}",
        response_model=AssetDetailResponse,
        operation_id="assets.get",
        summary="Get one tracked asset",
        dependencies=[view_gate],
    )
    def get(
        asset_id: str,
        ctx: Ctx,
        session: Db,
        include_archived: bool = Query(default=False),
    ) -> AssetDetailResponse:
        try:
            return asset_detail(
                session,
                ctx,
                asset_id=asset_id,
                include_archived=include_archived,
            )
        except AssetNotFound as exc:
            raise http_for_asset_error(exc) from exc

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
        ctx: Ctx,
        session: Db,
    ) -> AssetResponse:
        try:
            view = update_asset(session, ctx, asset_id, body=body.to_domain())
        except (
            AssetNotFound,
            AssetPlacementInvalid,
            AssetTypeUnavailable,
            AssetValidationError,
        ) as exc:
            raise http_for_asset_error(exc) from exc
        return AssetResponse.from_view(view)

    @api.delete(
        "/{asset_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="assets.delete",
        summary="Archive a tracked asset",
        dependencies=[edit_gate],
    )
    def delete_(asset_id: str, ctx: Ctx, session: Db) -> Response:
        try:
            archive_asset(session, ctx, asset_id=asset_id)
        except AssetNotFound as exc:
            raise http_for_asset_error(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.put(
        "/{asset_id}/restore",
        response_model=AssetResponse,
        operation_id="assets.restore",
        summary="Restore an archived asset",
        dependencies=[edit_gate],
    )
    def restore(asset_id: str, ctx: Ctx, session: Db) -> AssetResponse:
        try:
            view = restore_asset(session, ctx, asset_id=asset_id)
        except (
            AssetNotFound,
            AssetPlacementInvalid,
            AssetTypeUnavailable,
        ) as exc:
            raise http_for_asset_error(exc) from exc
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
        ctx: Ctx,
        session: Db,
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
            raise http_for_asset_error(exc) from exc
        return AssetResponse.from_view(view)

    @api.post(
        "/{asset_id}/regenerate_qr",
        response_model=AssetResponse,
        operation_id="assets.regenerate_qr",
        summary="Regenerate an asset QR token",
        dependencies=[edit_gate],
    )
    def regenerate(asset_id: str, ctx: Ctx, session: Db) -> AssetResponse:
        try:
            view = regenerate_qr(session, ctx, asset_id)
        except (AssetNotFound, AssetQrTokenExhausted, AssetValidationError) as exc:
            raise http_for_asset_error(exc) from exc
        return AssetResponse.from_view(view)

    @api.get(
        "/{asset_id}/qr.png",
        operation_id="assets.qr_png",
        summary="Render an asset QR code as PNG",
        dependencies=[view_gate],
        responses={
            200: {
                "description": "PNG QR code",
                "content": {
                    "image/png": {"schema": {"type": "string", "format": "binary"}}
                },
            },
        },
    )
    def qr_png(asset_id: str, request: Request, ctx: Ctx, session: Db) -> Response:
        try:
            view = get_asset(session, ctx, asset_id=asset_id)
        except AssetNotFound as exc:
            raise http_for_asset_error(exc) from exc
        return Response(
            content=render_qr(
                asset_scan_web_url(request, ctx, view.qr_token),
                label=view.name,
            ),
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    return api
