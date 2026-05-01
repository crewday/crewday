"""Pydantic request/response schemas for the asset HTTP routers.

Pure DTO module: every router (``assets``, ``actions``, ``documents``,
``scan``) consumes these. Keeping them in one place avoids cyclic
imports between the router modules and lets the routers stay focused
on their endpoint glue.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.assets.actions import AssetActionView, AssetNextDueView
from app.domain.assets.assets import AssetCreate, AssetUpdate, AssetView
from app.domain.assets.documents import AssetDocumentView
from app.domain.assets.types import AssetTypeView

__all__ = [
    "AssetActionCreateRequest",
    "AssetActionListResponse",
    "AssetActionResponse",
    "AssetActionUpdateRequest",
    "AssetCreateRequest",
    "AssetDetailActionResponse",
    "AssetDetailAssetResponse",
    "AssetDetailAssetTypeResponse",
    "AssetDetailDocumentResponse",
    "AssetDetailPropertyResponse",
    "AssetDetailResponse",
    "AssetDocumentListResponse",
    "AssetDocumentResponse",
    "AssetListResponse",
    "AssetMoveRequest",
    "AssetNextDueResponse",
    "AssetResponse",
    "AssetUpdateRequest",
    "DocumentExtractionPageResponse",
    "DocumentExtractionResponse",
    "WorkspaceDocumentListResponse",
    "WorkspaceDocumentResponse",
]


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


class WorkspaceDocumentResponse(BaseModel):
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


class WorkspaceDocumentListResponse(BaseModel):
    data: list[WorkspaceDocumentResponse]


class DocumentExtractionResponse(BaseModel):
    document_id: str
    status: Literal[
        "pending",
        "extracting",
        "succeeded",
        "failed",
        "unsupported",
        "empty",
    ]
    extractor: str | None
    body_preview: str
    page_count: int
    token_count: int
    has_secret_marker: bool
    last_error: str | None
    extracted_at: datetime | None


class DocumentExtractionPageResponse(BaseModel):
    page: int
    char_start: int
    char_end: int
    body: str
    more_pages: bool


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
