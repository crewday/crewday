"""Pydantic request/response schemas for the asset HTTP routers.

Pure DTO module: every router (``assets``, ``actions``, ``documents``,
``scan``) consumes these. Keeping them in one place avoids cyclic
imports between the router modules and lets the routers stay focused
on their endpoint glue.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

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
    "refine_request_schema",
]


def refine_request_schema(
    cls: type[BaseModel],
    schema: CoreSchema,
    handler: GetJsonSchemaHandler,
    *,
    drop_aliases: Iterable[str] = (),
    promote_to_required: Iterable[str] = (),
    min_properties: int | None = None,
    non_negative_decimals: Iterable[str] = (),
) -> JsonSchemaValue:
    """Tighten a pydantic-generated request schema to match runtime invariants.

    Helper for the asset request DTOs: pydantic produces a permissive
    ``str | null`` shape for every nullable optional field, but the
    ``model_validator`` on these models enforces stricter invariants
    at runtime (``exactly one of name or label``, ``PATCH body must
    include at least one field``, …).

    The contract gate (`schemathesis run --include-tag assets`) treats
    "schema-valid input that the server rejects" as a contract bug.
    Bringing the public schema into line with the validators keeps the
    public surface honest while preserving back-compat for the
    legacy aliases the validators still accept (``label``,
    ``purchased_at``, ``warranty_ends_at``, ``metadata``, ``slug``,
    ``icon``, ``default_actions_json``).

    Operations:

    * ``drop_aliases`` — remove transitional alias fields from the
      public properties map. The runtime ``model_validator`` still
      accepts them so existing clients keep working; we just stop
      *advertising* them as alternatives. The spec (``docs/specs/21-assets.md``)
      only documents the canonical names.
    * ``promote_to_required`` — list canonical fields that must
      appear in the body. Strips the ``null`` branch from each
      field's ``anyOf``, drops the ``default`` value, and adds the
      name to the schema's ``required`` array. Mirrors the runtime
      "name or label must be present, and not both" invariant.
    * ``min_properties`` — when set, adds ``minProperties`` to the
      object schema. Used on PATCH bodies that the runtime rejects
      with "PATCH body must include at least one field".
    * ``non_negative_decimals`` — ``Decimal | None`` fields whose
      runtime ``Field(ge=0)`` constraint is dropped from the
      pydantic-emitted JSON Schema (pydantic ships a permissive
      ``[+-]?…`` regex for the string variant that also matches
      ``"-1"``). Replaces the field's ``anyOf`` with a tight
      ``number | string-of-digits | null`` shape that excludes the
      negative branch.
    """

    out = dict(handler(schema))
    properties = dict(out.get("properties") or {})
    for alias in drop_aliases:
        properties.pop(alias, None)

    required = list(out.get("required") or [])
    for canonical in promote_to_required:
        if canonical not in required:
            required.append(canonical)
        if canonical in properties:
            field_schema: dict[str, Any] = dict(properties[canonical])
            any_of = field_schema.get("anyOf")
            if isinstance(any_of, list):
                non_null = [b for b in any_of if b.get("type") != "null"]
                if len(non_null) == 1:
                    field_schema.pop("anyOf", None)
                    field_schema.update(non_null[0])
                elif non_null:
                    field_schema["anyOf"] = non_null
            field_schema.pop("default", None)
            properties[canonical] = field_schema

    for decimal_field in non_negative_decimals:
        if decimal_field not in properties:
            continue
        field_schema = dict(properties[decimal_field])
        # Replace the permissive pydantic union with a non-negative
        # number-or-numeric-string variant (plus null). The string
        # branch keeps wire compatibility with clients that send
        # decimals as strings to dodge JSON's float lossiness.
        title = field_schema.get("title")
        new_branches: list[dict[str, Any]] = [
            {"type": "number", "minimum": 0},
            {"type": "string", "pattern": r"^\d+(\.\d+)?$"},
            {"type": "null"},
        ]
        field_schema = {"anyOf": new_branches}
        if title is not None:
            field_schema["title"] = title
        properties[decimal_field] = field_schema

    if properties:
        out["properties"] = properties
    if required:
        out["required"] = required
    if min_properties is not None:
        out["minProperties"] = min_properties
    return out


class AssetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type_id: str | None = None
    property_id: str = Field(..., min_length=1)
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

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # Public schema advertises only the canonical fields; the
        # legacy aliases (``label``, ``purchased_at``, …) still ride
        # through the runtime ``model_validator`` for back-compat but
        # are not part of the documented surface (§21 only spells the
        # canonical names). Promotes ``name`` to required so the
        # contract gate stops generating bodies with both ``name`` and
        # ``label`` null.
        return refine_request_schema(
            cls,
            schema,
            handler,
            drop_aliases=("label", "purchased_at", "warranty_ends_at", "metadata"),
            promote_to_required=("name",),
        )

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

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # PATCH bodies must carry at least one field (the runtime
        # validator rejects ``{}``); aliases stay accepted runtime-side
        # but are not part of the public schema.
        return refine_request_schema(
            cls,
            schema,
            handler,
            drop_aliases=("label", "purchased_at", "warranty_ends_at", "metadata"),
            min_properties=1,
        )

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

    property_id: str = Field(..., min_length=1)
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

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # Pydantic's emitted schema for ``Decimal | None`` with
        # ``ge=0`` carries a permissive ``[+-]?…`` regex on the string
        # variant that also matches ``"-1"`` — the contract gate then
        # generates negatives we reject at runtime. Encode the
        # non-negative invariant on the public schema instead.
        return refine_request_schema(
            cls,
            schema,
            handler,
            non_negative_decimals=("meter_reading",),
        )


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

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # Mirrors :class:`AssetUpdateRequest` — PATCH body cannot be
        # empty. Also encodes the non-negative ``meter_reading``
        # invariant (see :class:`AssetActionCreateRequest`).
        return refine_request_schema(
            cls,
            schema,
            handler,
            min_properties=1,
            non_negative_decimals=("meter_reading",),
        )


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
