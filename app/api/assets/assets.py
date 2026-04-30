"""Tracked asset HTTP router."""

from __future__ import annotations

import hashlib
import io
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.routing import NoMatchFound

from app.adapters.db.assets.models import AssetAction as AssetActionRow
from app.adapters.db.places.models import Area, Property, PropertyWorkspace
from app.adapters.qr import render_qr
from app.adapters.storage.ports import MimeSniffer, Storage
from app.api.deps import (
    current_workspace_context,
    db_session,
    get_mime_sniffer,
    get_storage,
)
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.authz.dep import Permission
from app.domain.assets.actions import (
    AssetActionAccessDenied,
    AssetActionNotFound,
    AssetActionValidationError,
    AssetActionView,
    AssetNextDueView,
    delete_action,
    list_actions,
    next_due,
    record_action,
    update_action,
)
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
from app.domain.assets.documents import (
    ASSET_DOCUMENT_CATEGORIES,
    AssetDocumentNotFound,
    AssetDocumentValidationError,
    AssetDocumentView,
    attach_document,
    delete_document,
    list_documents,
)
from app.domain.assets.types import AssetTypeNotFound, AssetTypeView, get_type
from app.tenancy import WorkspaceContext, tenant_agnostic

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
_Storage = Annotated[Storage, Depends(get_storage)]
_MimeSniffer = Annotated[MimeSniffer, Depends(get_mime_sniffer)]

_ASSET_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "Permission denied or CSRF mismatch"},
    404: {"description": "Asset resource not found"},
    409: {"description": "Asset conflict"},
    410: {"description": "Archived asset"},
}

_MAX_ASSET_DOCUMENT_BYTES = 25 * 1024 * 1024
_ASSET_DOCUMENT_ALLOWED_MIME = {
    "application/pdf",
    "application/zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/plain",
}


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


class AssetActionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["service", "repair", "replace", "inspect", "read"]
    performed_at: datetime | None = None
    notes_md: str | None = Field(default=None, max_length=20_000)
    meter_reading: Decimal | None = Field(default=None, ge=0)
    evidence_blob_hash: str | None = Field(default=None, min_length=64, max_length=64)


class AssetActionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=200)
    performed_at: datetime | None = None
    notes_md: str | None = Field(default=None, max_length=20_000)
    meter_reading: Decimal | None = Field(default=None, ge=0)
    evidence_blob_hash: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def _validate_sparse(self) -> AssetActionUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("PATCH body must include at least one field")
        return self


class AssetActionResponse(BaseModel):
    id: str
    workspace_id: str
    asset_id: str
    key: str | None
    kind: str
    label: str
    description_md: str | None
    interval_days: int | None
    last_performed_at: datetime | None
    performed_at: datetime | None
    performed_by: str | None
    notes_md: str | None
    meter_reading: Decimal | None
    evidence_blob_hash: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    @classmethod
    def from_view(cls, view: AssetActionView) -> AssetActionResponse:
        return cls(**asdict(view))


class AssetActionListResponse(BaseModel):
    data: list[AssetActionResponse]


class AssetNextDueResponse(BaseModel):
    key: str | None
    kind: str
    label: str
    due_at: datetime
    interval_days: int
    last_performed_at: datetime | None
    action_id: str | None

    @classmethod
    def from_view(cls, view: AssetNextDueView) -> AssetNextDueResponse:
        return cls(**asdict(view))


class AssetDocumentResponse(BaseModel):
    id: str
    workspace_id: str
    file_id: str | None
    blob_hash: str | None
    filename: str | None
    asset_id: str | None
    property_id: str | None
    kind: str
    category: str
    title: str
    notes_md: str | None
    expires_on: date | None
    amount_cents: int | None
    amount_currency: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    @classmethod
    def from_view(cls, view: AssetDocumentView) -> AssetDocumentResponse:
        return cls(**asdict(view))


class AssetDocumentListResponse(BaseModel):
    data: list[AssetDocumentResponse]


class AssetDetailAssetResponse(BaseModel):
    id: str
    property_id: str
    asset_type_id: str | None
    name: str
    area: str | None
    condition: str
    status: str
    make: str | None
    model: str | None
    serial_number: str | None
    installed_on: date | None
    purchased_on: date | None
    purchase_price_cents: int | None
    purchase_currency: str | None
    purchase_vendor: str | None
    warranty_expires_on: date | None
    expected_lifespan_years: int | None
    guest_visible: bool
    guest_instructions: str | None
    notes: str | None
    qr_token: str

    @classmethod
    def from_view(
        cls, view: AssetView, *, area_label: str | None
    ) -> AssetDetailAssetResponse:
        return cls(
            id=view.id,
            property_id=view.property_id,
            asset_type_id=view.asset_type_id,
            name=view.name,
            area=area_label,
            condition=view.condition,
            status=view.status,
            make=view.make,
            model=view.model,
            serial_number=view.serial_number,
            installed_on=view.installed_on,
            purchased_on=view.purchased_on,
            purchase_price_cents=view.purchase_price_cents,
            purchase_currency=view.purchase_currency,
            purchase_vendor=view.purchase_vendor,
            warranty_expires_on=view.warranty_expires_on,
            expected_lifespan_years=view.expected_lifespan_years,
            guest_visible=view.guest_visible,
            guest_instructions=view.guest_instructions_md,
            notes=view.notes_md,
            qr_token=view.qr_token,
        )


class AssetDetailAssetTypeResponse(BaseModel):
    id: str
    key: str
    name: str
    category: str
    icon_name: str | None
    default_actions: list[dict[str, object]]
    default_lifespan_years: int | None

    @classmethod
    def from_view(cls, view: AssetTypeView) -> AssetDetailAssetTypeResponse:
        return cls(
            id=view.id,
            key=view.key,
            name=view.name,
            category=view.category,
            icon_name=view.icon_name,
            default_actions=view.default_actions,
            default_lifespan_years=view.default_lifespan_years,
        )


class AssetDetailPropertyResponse(BaseModel):
    id: str
    name: str
    city: str
    timezone: str
    color: Literal["moss", "sky", "rust"]
    kind: Literal["str", "vacation", "residence", "mixed"]
    areas: list[str]
    evidence_policy: Literal["inherit", "require", "optional", "forbid"]
    country: str
    locale: str
    settings_override: dict[str, object]
    client_org_id: str | None
    owner_user_id: str | None


class AssetDetailActionResponse(BaseModel):
    id: str
    asset_id: str
    key: str | None
    kind: str
    label: str
    interval_days: int | None
    last_performed_at: datetime | None
    next_due_on: date | None
    linked_task_id: str | None
    linked_schedule_id: str | None
    description: str | None
    estimated_duration_minutes: int | None


class AssetDetailDocumentResponse(BaseModel):
    id: str
    asset_id: str | None
    property_id: str
    kind: str
    title: str
    filename: str
    size_kb: int
    uploaded_at: datetime
    expires_on: date | None
    amount_cents: int | None
    amount_currency: str | None
    extraction_status: str
    extracted_at: datetime | None

    @classmethod
    def from_view(
        cls,
        view: AssetDocumentView,
        *,
        property_id: str,
    ) -> AssetDetailDocumentResponse:
        return cls(
            id=view.id,
            asset_id=view.asset_id,
            property_id=view.property_id or property_id,
            kind=view.category,
            title=view.title,
            filename=view.filename or view.title,
            size_kb=0,
            uploaded_at=view.created_at,
            expires_on=view.expires_on,
            amount_cents=view.amount_cents,
            amount_currency=view.amount_currency,
            extraction_status="pending",
            extracted_at=None,
        )


class AssetDetailResponse(BaseModel):
    asset: AssetDetailAssetResponse
    asset_type: AssetDetailAssetTypeResponse | None
    property: AssetDetailPropertyResponse
    actions: list[AssetDetailActionResponse]
    documents: list[AssetDetailDocumentResponse]
    linked_tasks: list[dict[str, object]]


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
            status_code=422,
            detail={"error": "asset_type_unavailable", "message": str(exc)},
        )
    if isinstance(exc, AssetPlacementInvalid):
        return HTTPException(
            status_code=422,
            detail={"error": "asset_placement_invalid", "message": str(exc)},
        )
    if isinstance(exc, AssetValidationError):
        return HTTPException(
            status_code=422,
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


def _http_for_action_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AssetNotFound | AssetActionNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "asset_action_not_found"},
        )
    if isinstance(exc, AssetActionAccessDenied):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "permission_denied", "action_key": "assets.record_action"},
        )
    if isinstance(exc, AssetActionValidationError):
        return HTTPException(
            status_code=422,
            detail={"error": exc.error, "field": exc.field},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


def _http_for_document_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AssetNotFound | AssetDocumentNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "asset_document_not_found"},
        )
    if isinstance(exc, AssetDocumentValidationError):
        return HTTPException(
            status_code=422,
            detail={"error": exc.error, "field": exc.field},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


_PROPERTY_COLOR_PALETTE: tuple[Literal["moss", "sky", "rust"], ...] = (
    "moss",
    "sky",
    "rust",
)
_DEFAULT_ACTION_PREFIX = "default__"


@dataclass(frozen=True, slots=True)
class _CompletionActionSpec:
    kind: str
    tracking_key: str | None
    label: str | None


def _asset_detail(
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
    asset_type = _asset_type_for(session, ctx, asset)
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
        actions=_asset_detail_actions(asset, asset_type, action_views),
        documents=[
            AssetDetailDocumentResponse.from_view(
                document,
                property_id=asset.property_id,
            )
            for document in documents
        ],
        linked_tasks=[],
    )


def _asset_type_for(
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


def _asset_detail_actions(
    asset: AssetView,
    asset_type: AssetTypeView | None,
    action_views: list[AssetActionView],
) -> list[AssetDetailActionResponse]:
    if asset_type is None:
        return [_detail_action_from_view(asset, action) for action in action_views]
    default_keys = _default_action_tracking_keys(asset_type)
    actions = [
        _detail_action_from_view(asset, action)
        for action in action_views
        if action.key not in default_keys
    ]
    return _default_detail_actions(asset, asset_type, action_views) + actions


def _detail_action_from_view(
    asset: AssetView,
    action: AssetActionView,
) -> AssetDetailActionResponse:
    return AssetDetailActionResponse(
        id=action.id,
        asset_id=action.asset_id,
        key=action.key,
        kind=action.kind,
        label=action.label,
        interval_days=action.interval_days,
        last_performed_at=action.last_performed_at,
        next_due_on=_next_due_date(
            asset,
            action.last_performed_at,
            action.interval_days,
        ),
        linked_task_id=None,
        linked_schedule_id=None,
        description=action.description_md,
        estimated_duration_minutes=None,
    )


def _default_detail_actions(
    asset: AssetView,
    asset_type: AssetTypeView,
    action_views: list[AssetActionView],
) -> list[AssetDetailActionResponse]:
    actions: list[AssetDetailActionResponse] = []
    for index, item in enumerate(asset_type.default_actions):
        interval_days = _positive_int(item.get("interval_days"))
        kind = item.get("kind")
        if interval_days is None or not isinstance(kind, str):
            continue
        key = item.get("key")
        key_str = key if isinstance(key, str) and key else None
        label = item.get("label")
        label_str = label.strip() if isinstance(label, str) and label.strip() else kind
        tracking_key = _default_action_tracking_key(
            index=index,
            key=key_str,
            kind=kind,
        )
        last = _latest_action(action_views, key=tracking_key, kind=kind)
        actions.append(
            AssetDetailActionResponse(
                id=_default_action_id(index=index, key=key_str, kind=kind),
                asset_id=asset.id,
                key=key_str,
                kind=kind,
                label=label_str,
                interval_days=interval_days,
                last_performed_at=last.last_performed_at if last is not None else None,
                next_due_on=_next_due_date(
                    asset,
                    last.last_performed_at if last is not None else None,
                    interval_days,
                ),
                linked_task_id=None,
                linked_schedule_id=None,
                description=None,
                estimated_duration_minutes=_positive_int(
                    item.get("estimated_duration_minutes")
                ),
            )
        )
    return actions


def _default_action_tracking_keys(asset_type: AssetTypeView) -> set[str]:
    keys: set[str] = set()
    for index, item in enumerate(asset_type.default_actions):
        kind = item.get("kind")
        if not isinstance(kind, str):
            continue
        key = item.get("key")
        key_str = key if isinstance(key, str) and key else None
        keys.add(_default_action_tracking_key(index=index, key=key_str, kind=kind))
    return keys


def _latest_action(
    actions: list[AssetActionView],
    *,
    key: str | None,
    kind: str,
) -> AssetActionView | None:
    if key is not None:
        matches = [
            action
            for action in actions
            if action.last_performed_at is not None and action.key == key
        ]
        if not matches:
            return None
        return max(matches, key=lambda action: _as_utc(action.last_performed_at))
    matches = [
        action
        for action in actions
        if action.last_performed_at is not None and action.kind == kind
    ]
    if not matches:
        return None
    return max(matches, key=lambda action: _as_utc(action.last_performed_at))


def _next_due_date(
    asset: AssetView,
    last_performed_at: datetime | None,
    interval_days: int | None,
) -> date | None:
    if interval_days is None:
        return None
    base = (
        _as_utc(last_performed_at)
        if last_performed_at is not None
        else _asset_start(asset)
    )
    return (base + timedelta(days=interval_days)).date()


def _asset_start(asset: AssetView) -> datetime:
    start = asset.installed_on or asset.purchased_on
    if start is not None:
        return datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    return _as_utc(asset.created_at)


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("datetime value is required")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _default_action_id(*, index: int, key: str | None, kind: str) -> str:
    return f"{_DEFAULT_ACTION_PREFIX}{index}__{key or kind}"


def _default_action_tracking_key(*, index: int, key: str | None, kind: str) -> str:
    return key or _default_action_id(index=index, key=None, kind=kind)


def _completion_action_spec(
    session: Session,
    ctx: WorkspaceContext,
    asset: AssetView,
    action_id: str,
) -> _CompletionActionSpec:
    if action_id.startswith(_DEFAULT_ACTION_PREFIX):
        asset_type = _asset_type_for(session, ctx, asset)
        if asset_type is None:
            raise AssetActionNotFound()
        for index, item in enumerate(asset_type.default_actions):
            kind = item.get("kind")
            key = item.get("key")
            key_str = key if isinstance(key, str) and key else None
            label = item.get("label")
            label_str = (
                label.strip() if isinstance(label, str) and label.strip() else None
            )
            if (
                isinstance(kind, str)
                and _default_action_id(
                    index=index,
                    key=key_str,
                    kind=kind,
                )
                == action_id
            ):
                return _CompletionActionSpec(
                    kind=kind,
                    tracking_key=_default_action_tracking_key(
                        index=index,
                        key=key_str,
                        kind=kind,
                    ),
                    label=label_str,
                )
        raise AssetActionNotFound()

    with tenant_agnostic():
        row = session.scalar(
            select(AssetActionRow).where(
                AssetActionRow.id == action_id,
                AssetActionRow.workspace_id == ctx.workspace_id,
                AssetActionRow.asset_id == asset.id,
                AssetActionRow.deleted_at.is_(None),
            )
        )
    if row is None:
        raise AssetActionNotFound()
    return _CompletionActionSpec(kind=row.kind, tracking_key=row.key, label=row.label)


def _stamp_completion_metadata(
    session: Session,
    view: AssetActionView,
    spec: _CompletionActionSpec,
) -> AssetActionView:
    if spec.tracking_key is None and spec.label is None:
        return view
    with tenant_agnostic():
        row = session.get(AssetActionRow, view.id)
    if row is None:
        return view
    if spec.tracking_key is not None:
        row.key = spec.tracking_key
    if spec.label is not None:
        row.label = spec.label
    session.flush()
    return AssetActionView(
        id=row.id,
        workspace_id=row.workspace_id,
        asset_id=row.asset_id,
        key=row.key,
        kind=row.kind,
        label=row.label,
        description_md=row.description_md,
        interval_days=row.interval_days,
        last_performed_at=row.last_performed_at,
        performed_at=row.performed_at,
        performed_by=row.performed_by,
        notes_md=row.notes_md,
        meter_reading=row.meter_reading,
        evidence_blob_hash=row.evidence_blob_hash,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


async def _read_document_capped(upload: UploadFile) -> bytes:
    chunk_size = 64 * 1024
    total = 0
    pieces: list[bytes] = []
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_ASSET_DOCUMENT_BYTES:
            await upload.close()
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={
                    "error": "asset_document_too_large",
                    "message": (
                        f"upload exceeds the {_MAX_ASSET_DOCUMENT_BYTES}-byte cap"
                    ),
                },
            )
        pieces.append(chunk)
    await upload.close()
    return b"".join(pieces)


def _sniff_document_mime(
    mime_sniffer: MimeSniffer,
    payload: bytes,
    *,
    declared_type: str,
) -> str:
    sniffed = mime_sniffer.sniff(payload, hint=declared_type)
    if sniffed is None and declared_type.lower().startswith("text/"):
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError:
            sniffed = None
        else:
            sniffed = "text/plain"
    if sniffed not in _ASSET_DOCUMENT_ALLOWED_MIME:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "error": "asset_document_content_type_rejected",
                "content_type": sniffed,
                "declared_type": declared_type,
            },
        )
    return sniffed


def _storage_from_request(request: Request) -> Storage:
    storage: Storage | None = getattr(request.app.state, "storage", None)
    if storage is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "storage_unavailable"},
        )
    return storage


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
    api = APIRouter(tags=["assets"], responses=_ASSET_ERROR_RESPONSES)
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
    api = APIRouter(tags=["assets"], responses=_ASSET_ERROR_RESPONSES)

    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))
    edit_gate = Depends(Permission("assets.edit", scope_kind="workspace"))
    manage_documents_gate = Depends(
        Permission("assets.manage_documents", scope_kind="workspace")
    )

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
        "/{asset_id}/actions",
        response_model=AssetActionListResponse,
        operation_id="assets.actions.list",
        summary="List asset actions",
        dependencies=[view_gate],
    )
    def actions(
        asset_id: str,
        ctx: _Ctx,
        session: _Db,
        kind: Annotated[
            Literal["service", "repair", "replace", "inspect", "read"] | None, Query()
        ] = None,
        since: Annotated[datetime | None, Query()] = None,
        until: Annotated[datetime | None, Query()] = None,
    ) -> AssetActionListResponse:
        try:
            views = list_actions(
                session,
                ctx,
                asset_id,
                kind=kind,
                since=since,
                until=until,
            )
        except (AssetNotFound, AssetActionValidationError) as exc:
            raise _http_for_action_error(exc) from exc
        return AssetActionListResponse(
            data=[AssetActionResponse.from_view(view) for view in views]
        )

    @api.post(
        "/{asset_id}/actions",
        response_model=AssetActionResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="assets.actions.record",
        summary="Record an asset action",
    )
    def record(
        asset_id: str,
        body: AssetActionCreateRequest,
        ctx: _Ctx,
        session: _Db,
        request: Request,
    ) -> AssetActionResponse:
        try:
            view = record_action(
                session,
                ctx,
                asset_id,
                kind=body.kind,
                performed_at=body.performed_at,
                notes_md=body.notes_md,
                meter_reading=body.meter_reading,
                evidence_blob_hash=body.evidence_blob_hash,
                storage=(
                    _storage_from_request(request)
                    if body.evidence_blob_hash is not None
                    else None
                ),
            )
        except (
            AssetNotFound,
            AssetActionAccessDenied,
            AssetActionValidationError,
        ) as exc:
            raise _http_for_action_error(exc) from exc
        return AssetActionResponse.from_view(view)

    @api.post(
        "/{asset_id}/actions/{action_id}/complete",
        response_model=AssetActionResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="assets.actions.complete",
        summary="Mark an asset action as done",
    )
    def complete_action(
        asset_id: str,
        action_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> AssetActionResponse:
        try:
            asset = get_asset(session, ctx, asset_id=asset_id)
            spec = _completion_action_spec(session, ctx, asset, action_id)
            view = record_action(session, ctx, asset_id, kind=spec.kind)
            view = _stamp_completion_metadata(session, view, spec)
        except (
            AssetNotFound,
            AssetActionAccessDenied,
            AssetActionNotFound,
            AssetActionValidationError,
        ) as exc:
            raise _http_for_action_error(exc) from exc
        return AssetActionResponse.from_view(view)

    @api.get(
        "/{asset_id}/actions/next_due",
        response_model=AssetNextDueResponse | None,
        operation_id="assets.actions.next_due",
        summary="Return the next due asset action",
        dependencies=[view_gate],
    )
    def next_due_action(
        asset_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> AssetNextDueResponse | None:
        try:
            view = next_due(session, ctx, asset_id)
        except AssetNotFound as exc:
            raise _http_for_asset_error(exc) from exc
        return AssetNextDueResponse.from_view(view) if view is not None else None

    @api.patch(
        "/actions/{action_id}",
        response_model=AssetActionResponse,
        operation_id="assets.actions.update",
        summary="Update an asset action",
        dependencies=[edit_gate],
    )
    def patch_action(
        action_id: str,
        body: AssetActionUpdateRequest,
        ctx: _Ctx,
        session: _Db,
        request: Request,
    ) -> AssetActionResponse:
        try:
            view = update_action(
                session,
                ctx,
                action_id,
                label=body.label,
                performed_at=body.performed_at,
                notes_md=body.notes_md,
                meter_reading=body.meter_reading,
                evidence_blob_hash=body.evidence_blob_hash,
                storage=(
                    _storage_from_request(request)
                    if body.evidence_blob_hash is not None
                    else None
                ),
            )
        except (AssetNotFound, AssetActionNotFound, AssetActionValidationError) as exc:
            raise _http_for_action_error(exc) from exc
        return AssetActionResponse.from_view(view)

    @api.delete(
        "/actions/{action_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="assets.actions.delete",
        summary="Delete an asset action",
        dependencies=[edit_gate],
    )
    def delete_recorded_action(
        action_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        try:
            delete_action(session, ctx, action_id)
        except (AssetNotFound, AssetActionNotFound) as exc:
            raise _http_for_action_error(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get(
        "/{asset_id}/documents",
        response_model=AssetDocumentListResponse,
        operation_id="assets.documents.list",
        summary="List asset documents",
        dependencies=[view_gate],
    )
    def documents(
        asset_id: str,
        ctx: _Ctx,
        session: _Db,
        category: (
            Literal[
                "manual",
                "warranty",
                "invoice",
                "receipt",
                "photo",
                "certificate",
                "contract",
                "permit",
                "insurance",
                "other",
            ]
            | None
        ) = Query(default=None),
    ) -> AssetDocumentListResponse:
        try:
            views = list_documents(session, ctx, asset_id, category=category)
        except (AssetNotFound, AssetDocumentValidationError) as exc:
            raise _http_for_document_error(exc) from exc
        return AssetDocumentListResponse(
            data=[AssetDocumentResponse.from_view(view) for view in views]
        )

    @api.post(
        "/{asset_id}/documents",
        response_model=AssetDocumentResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="assets.documents.upload",
        summary="Upload an asset document",
        dependencies=[manage_documents_gate],
    )
    async def upload_document(
        asset_id: str,
        ctx: _Ctx,
        session: _Db,
        storage: _Storage,
        mime_sniffer: _MimeSniffer,
        category: Annotated[str, Form()],
        title: Annotated[str | None, Form(max_length=200)] = None,
        notes_md: Annotated[str | None, Form(max_length=20_000)] = None,
        file: Annotated[UploadFile | None, File()] = None,
    ) -> AssetDocumentResponse:
        if file is None:
            raise HTTPException(
                status_code=422,
                detail={"error": "asset_document_file_required"},
            )
        declared_type = file.content_type
        if declared_type is None or declared_type == "":
            await file.close()
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={"error": "asset_document_content_type_missing"},
            )
        try:
            get_asset(session, ctx, asset_id=asset_id)
            if category not in ASSET_DOCUMENT_CATEGORIES:
                raise AssetDocumentValidationError("category", "invalid")
        except (AssetNotFound, AssetDocumentValidationError) as exc:
            await file.close()
            raise _http_for_document_error(exc) from exc
        payload = await _read_document_capped(file)
        sniffed_type = _sniff_document_mime(
            mime_sniffer, payload, declared_type=declared_type
        )
        blob_hash = hashlib.sha256(payload).hexdigest()
        storage.put(blob_hash, io.BytesIO(payload), content_type=sniffed_type)
        try:
            view = attach_document(
                session,
                ctx,
                asset_id,
                blob_hash=blob_hash,
                filename=file.filename or "upload.bin",
                category=category,
                title=title,
                notes_md=notes_md,
                storage=storage,
            )
        except (AssetNotFound, AssetDocumentValidationError) as exc:
            raise _http_for_document_error(exc) from exc
        return AssetDocumentResponse.from_view(view)

    @api.delete(
        "/documents/{document_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="assets.documents.delete",
        summary="Delete an asset document",
        dependencies=[manage_documents_gate],
    )
    def delete_asset_document(
        document_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        try:
            delete_document(session, ctx, document_id)
        except (AssetNotFound, AssetDocumentNotFound) as exc:
            raise _http_for_document_error(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get(
        "/{asset_id}",
        response_model=AssetDetailResponse,
        operation_id="assets.get",
        summary="Get one tracked asset",
        dependencies=[view_gate],
    )
    def get(
        asset_id: str,
        ctx: _Ctx,
        session: _Db,
        include_archived: bool = Query(default=False),
    ) -> AssetDetailResponse:
        try:
            return _asset_detail(
                session,
                ctx,
                asset_id=asset_id,
                include_archived=include_archived,
            )
        except AssetNotFound as exc:
            raise _http_for_asset_error(exc) from exc

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
