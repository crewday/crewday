"""Asset-type HTTP router — ``/assets/asset_types``."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema
from sqlalchemy.orm import Session

from app.api.assets._shared import PROBLEM_JSON_RESPONSES
from app.api.assets.schemas import refine_request_schema
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.authz.dep import Permission
from app.domain.assets.types import (
    AssetTypeCreate,
    AssetTypeKeyConflict,
    AssetTypeNotFound,
    AssetTypeReadOnly,
    AssetTypeUpdate,
    AssetTypeView,
    DefaultAssetAction,
    create_type,
    delete_type,
    get_type,
    list_types,
    update_type,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "AssetTypeCreateRequest",
    "AssetTypeListResponse",
    "AssetTypeResponse",
    "AssetTypeUpdateRequest",
    "DefaultAssetActionRequest",
    "build_asset_types_alias_router",
    "build_asset_types_router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]

# Asset-type error envelope. Mirrors the asset router's
# ``ASSET_ERROR_RESPONSES`` shape so the OpenAPI contract describes
# what the server actually emits (``application/problem+json``), not
# FastAPI's default ``HTTPValidationError`` shape which the runtime
# never returns.
_ASSET_TYPE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {
        "description": "Permission denied or read-only asset type",
        "content": PROBLEM_JSON_RESPONSES[422]["content"],
    },
    404: {
        "description": "Asset type not found",
        "content": PROBLEM_JSON_RESPONSES[422]["content"],
    },
    409: {
        "description": "Asset type key conflict",
        "content": PROBLEM_JSON_RESPONSES[422]["content"],
    },
    422: PROBLEM_JSON_RESPONSES[422],
}


class DefaultAssetActionRequest(DefaultAssetAction):
    """Wire-facing default action shape."""


class AssetTypeCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str | None = Field(default=None, min_length=1, max_length=80)
    slug: str | None = Field(default=None, min_length=1, max_length=80)
    name: str = Field(..., min_length=1, max_length=160)
    category: Literal[
        "climate",
        "appliance",
        "plumbing",
        "pool",
        "heating",
        "outdoor",
        "safety",
        "security",
        "vehicle",
        "other",
    ] = "other"
    icon_name: str | None = Field(default=None, max_length=64)
    icon: str | None = Field(default=None, max_length=64)
    description_md: str | None = Field(default=None, max_length=20_000)
    default_lifespan_years: int | None = Field(default=None, ge=1)
    default_actions: list[DefaultAssetActionRequest] = Field(default_factory=list)
    default_actions_json: list[DefaultAssetActionRequest] | None = None

    @model_validator(mode="after")
    def _resolve_aliases(self) -> AssetTypeCreateRequest:
        if (self.key is None) == (self.slug is None):
            raise ValueError("send exactly one of key or slug")
        if self.icon_name is not None and self.icon is not None:
            raise ValueError("send only one of icon_name or icon")
        if (
            "default_actions" in self.model_fields_set
            and self.default_actions_json is not None
        ):
            raise ValueError("send only one of default_actions or default_actions_json")
        return self

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # Public schema advertises only the canonical fields; the
        # legacy aliases (``slug``, ``icon``, ``default_actions_json``)
        # still ride through the runtime ``model_validator`` for
        # back-compat but are not part of the documented surface.
        return refine_request_schema(
            cls,
            schema,
            handler,
            drop_aliases=("slug", "icon", "default_actions_json"),
            promote_to_required=("key",),
        )

    def to_domain(self) -> AssetTypeCreate:
        return AssetTypeCreate(
            key=self.key if self.key is not None else self.slug,
            name=self.name,
            category=self.category,
            icon_name=self.icon_name if self.icon_name is not None else self.icon,
            description_md=self.description_md,
            default_lifespan_years=self.default_lifespan_years,
            default_actions=list(
                self.default_actions_json
                if self.default_actions_json is not None
                else self.default_actions
            ),
        )


class AssetTypeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str | None = Field(default=None, min_length=1, max_length=80)
    slug: str | None = Field(default=None, min_length=1, max_length=80)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    category: (
        Literal[
            "climate",
            "appliance",
            "plumbing",
            "pool",
            "heating",
            "outdoor",
            "safety",
            "security",
            "vehicle",
            "other",
        ]
        | None
    ) = None
    icon_name: str | None = Field(default=None, max_length=64)
    icon: str | None = Field(default=None, max_length=64)
    description_md: str | None = Field(default=None, max_length=20_000)
    default_lifespan_years: int | None = Field(default=None, ge=1)
    default_actions: list[DefaultAssetActionRequest] | None = None
    default_actions_json: list[DefaultAssetActionRequest] | None = None

    @model_validator(mode="after")
    def _resolve_aliases(self) -> AssetTypeUpdateRequest:
        if self.key is not None and self.slug is not None:
            raise ValueError("send only one of key or slug")
        if self.icon_name is not None and self.icon is not None:
            raise ValueError("send only one of icon_name or icon")
        if self.default_actions is not None and self.default_actions_json is not None:
            raise ValueError("send only one of default_actions or default_actions_json")
        if not self.model_fields_set:
            raise ValueError("PATCH body must include at least one field")
        return self

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # PATCH body must carry at least one field; legacy aliases are
        # not advertised on the public surface (back-compat only).
        return refine_request_schema(
            cls,
            schema,
            handler,
            drop_aliases=("slug", "icon", "default_actions_json"),
            min_properties=1,
        )

    def to_domain(self) -> AssetTypeUpdate:
        payload: dict[str, object | None] = {}
        sent = self.model_fields_set
        if "key" in sent or "slug" in sent:
            payload["key"] = self.key if self.key is not None else self.slug
        for field_name in (
            "name",
            "category",
            "description_md",
            "default_lifespan_years",
        ):
            if field_name in sent:
                payload[field_name] = getattr(self, field_name)
        if "icon_name" in sent or "icon" in sent:
            payload["icon_name"] = self.icon_name if "icon_name" in sent else self.icon
        if "default_actions" in sent or "default_actions_json" in sent:
            payload["default_actions"] = (
                self.default_actions
                if self.default_actions is not None
                else self.default_actions_json
            )
        return AssetTypeUpdate.model_validate(payload)


class AssetTypeResponse(BaseModel):
    id: str
    workspace_id: str | None
    key: str
    name: str
    category: str
    icon_name: str | None
    description_md: str | None
    default_lifespan_years: int | None
    default_actions: list[dict[str, object]]
    default_actions_json: list[dict[str, object]]
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    archived_at: datetime | None
    is_system: bool

    @classmethod
    def from_view(cls, view: AssetTypeView) -> AssetTypeResponse:
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            key=view.key,
            name=view.name,
            category=view.category,
            icon_name=view.icon_name,
            description_md=view.description_md,
            default_lifespan_years=view.default_lifespan_years,
            default_actions=view.default_actions,
            default_actions_json=view.default_actions,
            created_at=view.created_at,
            updated_at=view.updated_at,
            deleted_at=view.deleted_at,
            archived_at=view.deleted_at,
            is_system=view.is_system,
        )


class AssetTypeListResponse(BaseModel):
    data: list[AssetTypeResponse]
    next_cursor: str | None = None
    has_more: bool = False


def _http_for_type_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AssetTypeNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "asset_type_not_found"},
        )
    if isinstance(exc, AssetTypeReadOnly):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "asset_type_read_only", "message": str(exc)},
        )
    if isinstance(exc, AssetTypeKeyConflict):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "asset_type_key_conflict", "message": str(exc)},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


def _list_type_response(
    session: Session,
    ctx: WorkspaceContext,
    *,
    category: str | None,
    workspace_only: bool,
    include_archived: bool,
    cursor: str | None,
    limit: int,
) -> AssetTypeListResponse:
    views = list_types(
        session,
        ctx,
        category=category,
        workspace_only=workspace_only,
        include_archived=include_archived,
        after_id=decode_cursor(cursor),
        limit=limit + 1,
    )
    page = paginate(views, limit=limit, key_getter=lambda view: view.id)
    return AssetTypeListResponse(
        data=[AssetTypeResponse.from_view(view) for view in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


def build_asset_types_alias_router() -> APIRouter:
    api = APIRouter(tags=["assets", "asset_types"], responses=PROBLEM_JSON_RESPONSES)
    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))

    @api.get(
        "/asset_types",
        response_model=AssetTypeListResponse,
        operation_id="asset_types.list_flat",
        summary="List asset types visible to the caller's workspace",
        dependencies=[view_gate],
    )
    def list_flat(
        ctx: _Ctx,
        session: _Db,
        category: str | None = Query(default=None),
        workspace_only: bool = Query(default=False),
        include_archived: bool = Query(default=False),
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> AssetTypeListResponse:
        return _list_type_response(
            session,
            ctx,
            category=category,
            workspace_only=workspace_only,
            include_archived=include_archived,
            cursor=cursor,
            limit=limit,
        )

    return api


def build_asset_types_router() -> APIRouter:
    api = APIRouter(
        prefix="/asset_types",
        tags=["assets", "asset_types"],
        responses=_ASSET_TYPE_ERROR_RESPONSES,
    )

    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))
    manage_gate = Depends(Permission("assets.manage_types", scope_kind="workspace"))

    @api.get(
        "",
        response_model=AssetTypeListResponse,
        operation_id="asset_types.list",
        summary="List asset types visible to the caller's workspace",
        dependencies=[view_gate],
    )
    def list_(
        ctx: _Ctx,
        session: _Db,
        category: str | None = Query(default=None),
        workspace_only: bool = Query(default=False),
        include_archived: bool = Query(default=False),
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> AssetTypeListResponse:
        return _list_type_response(
            session,
            ctx,
            category=category,
            workspace_only=workspace_only,
            include_archived=include_archived,
            cursor=cursor,
            limit=limit,
        )

    @api.post(
        "",
        response_model=AssetTypeResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="asset_types.create",
        summary="Create a workspace-custom asset type",
        dependencies=[manage_gate],
    )
    def create(
        body: AssetTypeCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> AssetTypeResponse:
        try:
            view = create_type(session, ctx, body=body.to_domain())
        except AssetTypeKeyConflict as exc:
            raise _http_for_type_error(exc) from exc
        return AssetTypeResponse.from_view(view)

    @api.get(
        "/{type_id}",
        response_model=AssetTypeResponse,
        operation_id="asset_types.get",
        summary="Get one asset type",
        dependencies=[view_gate],
    )
    def get(
        type_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> AssetTypeResponse:
        try:
            view = get_type(session, ctx, type_id=type_id)
        except AssetTypeNotFound as exc:
            raise _http_for_type_error(exc) from exc
        return AssetTypeResponse.from_view(view)

    @api.patch(
        "/{type_id}",
        response_model=AssetTypeResponse,
        operation_id="asset_types.update",
        summary="Update a workspace-custom asset type",
        dependencies=[manage_gate],
    )
    def patch(
        type_id: str,
        body: AssetTypeUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> AssetTypeResponse:
        try:
            view = update_type(session, ctx, type_id=type_id, body=body.to_domain())
        except (AssetTypeKeyConflict, AssetTypeNotFound, AssetTypeReadOnly) as exc:
            raise _http_for_type_error(exc) from exc
        return AssetTypeResponse.from_view(view)

    @api.delete(
        "/{type_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="asset_types.delete",
        summary="Delete or archive a workspace-custom asset type",
        dependencies=[manage_gate],
    )
    def delete_(type_id: str, ctx: _Ctx, session: _Db) -> Response:
        try:
            delete_type(session, ctx, type_id=type_id)
        except (AssetTypeNotFound, AssetTypeReadOnly) as exc:
            raise _http_for_type_error(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return api
