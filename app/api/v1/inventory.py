"""Inventory HTTP router."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.api.pagination import DEFAULT_LIMIT, LimitQuery, decode_cursor, encode_cursor
from app.authz.dep import Permission
from app.services.inventory import item_service, movement_service
from app.services.inventory.item_service import (
    InventoryItemCreate,
    InventoryItemUpdate,
    InventoryItemView,
)
from app.services.inventory.movement_service import InventoryMovementView
from app.tenancy import WorkspaceContext

__all__ = [
    "InventoryItemCreateRequest",
    "InventoryItemListResponse",
    "InventoryItemResponse",
    "InventoryItemUpdateRequest",
    "InventoryMovementCreateRequest",
    "InventoryMovementListResponse",
    "InventoryMovementResponse",
    "build_inventory_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]

_MAX_TEXT = 20_000
_MAX_SHORT = 500
_IdempotencyKey = Annotated[
    str | None, Header(alias="Idempotency-Key", max_length=_MAX_SHORT)
]


class InventoryItemCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=_MAX_SHORT)
    sku: str | None = Field(default=None, max_length=_MAX_SHORT)
    unit: str = Field(min_length=1, max_length=_MAX_SHORT)
    reorder_point: Decimal | None = None
    reorder_target: Decimal | None = None
    vendor: str | None = Field(default=None, max_length=_MAX_SHORT)
    vendor_url: str | None = Field(default=None, max_length=2_000)
    unit_cost_cents: int | None = Field(default=None, ge=0)
    barcode_ean13: str | None = Field(default=None, max_length=_MAX_SHORT)
    tags: list[str] = Field(default_factory=list)
    notes_md: str | None = Field(default=None, max_length=_MAX_TEXT)

    def to_service(self) -> InventoryItemCreate:
        return InventoryItemCreate(
            name=self.name,
            sku=self.sku,
            unit=self.unit,
            reorder_point=self.reorder_point,
            reorder_target=self.reorder_target,
            vendor=self.vendor,
            vendor_url=self.vendor_url,
            unit_cost_cents=self.unit_cost_cents,
            barcode_ean13=self.barcode_ean13,
            tags=tuple(self.tags),
            notes_md=self.notes_md,
        )


class InventoryItemUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=_MAX_SHORT)
    sku: str | None = Field(default=None, max_length=_MAX_SHORT)
    unit: str | None = Field(default=None, min_length=1, max_length=_MAX_SHORT)
    reorder_point: Decimal | None = None
    reorder_target: Decimal | None = None
    vendor: str | None = Field(default=None, max_length=_MAX_SHORT)
    vendor_url: str | None = Field(default=None, max_length=2_000)
    unit_cost_cents: int | None = Field(default=None, ge=0)
    barcode_ean13: str | None = Field(default=None, max_length=_MAX_SHORT)
    tags: list[str] = Field(default_factory=list)
    notes_md: str | None = Field(default=None, max_length=_MAX_TEXT)

    def to_service(self) -> InventoryItemUpdate:
        return InventoryItemUpdate(
            fields_set=frozenset(self.model_fields_set),
            name=self.name,
            sku=self.sku,
            unit=self.unit,
            reorder_point=self.reorder_point,
            reorder_target=self.reorder_target,
            vendor=self.vendor,
            vendor_url=self.vendor_url,
            unit_cost_cents=self.unit_cost_cents,
            barcode_ean13=self.barcode_ean13,
            tags=tuple(self.tags),
            notes_md=self.notes_md,
        )


class InventoryItemResponse(BaseModel):
    id: str
    workspace_id: str
    property_id: str
    name: str
    sku: str | None
    unit: str
    on_hand: Decimal
    reorder_point: Decimal | None
    reorder_target: Decimal | None
    vendor: str | None
    vendor_url: str | None
    unit_cost_cents: int | None
    barcode_ean13: str | None
    tags: list[str]
    notes_md: str | None
    created_at: str
    updated_at: str | None
    deleted_at: str | None

    @field_serializer("on_hand", "reorder_point", "reorder_target")
    def _decimal_as_number(self, value: Decimal | None) -> int | float | None:
        if value is None:
            return None
        if value == value.to_integral_value():
            return int(value)
        return float(value)

    @classmethod
    def from_view(cls, view: InventoryItemView) -> InventoryItemResponse:
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            property_id=view.property_id,
            name=view.name,
            sku=view.sku,
            unit=view.unit,
            on_hand=view.on_hand,
            reorder_point=view.reorder_point,
            reorder_target=view.reorder_target,
            vendor=view.vendor,
            vendor_url=view.vendor_url,
            unit_cost_cents=view.unit_cost_cents,
            barcode_ean13=view.barcode_ean13,
            tags=list(view.tags),
            notes_md=view.notes_md,
            created_at=view.created_at.isoformat(),
            updated_at=view.updated_at.isoformat() if view.updated_at else None,
            deleted_at=view.deleted_at.isoformat() if view.deleted_at else None,
        )


class InventoryItemListResponse(BaseModel):
    data: list[InventoryItemResponse]


class InventoryMovementCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delta: Decimal
    reason: movement_service.MovementReason
    source_task_id: str | None = Field(default=None, max_length=_MAX_SHORT)
    occurrence_id: str | None = Field(default=None, max_length=_MAX_SHORT)
    note: str | None = Field(default=None, max_length=_MAX_TEXT)

    @model_validator(mode="after")
    def _task_aliases_agree(self) -> InventoryMovementCreateRequest:
        if (
            self.source_task_id is not None
            and self.occurrence_id is not None
            and self.source_task_id != self.occurrence_id
        ):
            raise ValueError("source_task_id and occurrence_id must match")
        return self

    def resolved_source_task_id(self) -> str | None:
        return (
            self.source_task_id
            if self.source_task_id is not None
            else self.occurrence_id
        )


class InventoryAdjustRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_on_hand: Decimal
    reason: movement_service.MovementReason = "audit_correction"
    note: str | None = Field(default=None, max_length=_MAX_TEXT)


class InventoryMovementResponse(BaseModel):
    id: str
    workspace_id: str
    item_id: str
    delta: Decimal
    reason: str
    source_task_id: str | None
    occurrence_id: str | None
    source_stocktake_id: str | None
    actor_kind: str
    actor_id: str | None
    occurred_at: str
    note: str | None
    on_hand_after: Decimal

    @field_serializer("delta", "on_hand_after")
    def _decimal_as_number(self, value: Decimal) -> int | float:
        if value == value.to_integral_value():
            return int(value)
        return float(value)

    @classmethod
    def from_view(cls, view: InventoryMovementView) -> InventoryMovementResponse:
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            item_id=view.item_id,
            delta=view.delta,
            reason=view.reason,
            source_task_id=view.source_task_id,
            occurrence_id=view.source_task_id,
            source_stocktake_id=view.source_stocktake_id,
            actor_kind=view.actor_kind,
            actor_id=view.actor_id,
            occurred_at=view.at.isoformat(),
            note=view.note,
            on_hand_after=view.on_hand_after,
        )


class InventoryMovementListResponse(BaseModel):
    data: list[InventoryMovementResponse]
    next_cursor: str | None
    has_more: bool


def _http_for_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "inventory_item_not_found"},
    )


def _http_for_property_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "property_not_found"},
    )


def _http_for_conflict(exc: item_service.InventoryItemConflict) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "inventory_item_conflict", "field": exc.field},
    )


def _http_for_validation(
    exc: item_service.InventoryItemValidationError,
) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": exc.error, "field": exc.field},
    )


def _http_for_movement_validation(
    exc: movement_service.InventoryMovementValidationError,
) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": exc.error, "field": exc.field},
    )


def _decode_movement_cursor(
    before: str | None,
) -> tuple[datetime, str | None] | None:
    if before is None:
        return None
    raw = before
    if "|" not in raw:
        try:
            decoded = decode_cursor(raw)
        except HTTPException:
            decoded = None
        if decoded is not None:
            raw = decoded
    if "|" not in raw:
        try:
            return datetime.fromisoformat(raw), None
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_cursor",
                    "message": "movement cursor timestamp is not ISO-8601",
                },
            ) from exc
    iso, movement_id = raw.split("|", 1)
    if not movement_id:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_cursor",
                "message": "movement cursor missing movement id",
            },
        )
    try:
        return datetime.fromisoformat(iso), movement_id
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_cursor",
                "message": "movement cursor timestamp is not ISO-8601",
            },
        ) from exc


def _movement_cursor(view: InventoryMovementView) -> str:
    return f"{view.at.isoformat()}|{view.id}"


def build_inventory_router() -> APIRouter:
    api = APIRouter(tags=["inventory"])
    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))
    edit_gate = Depends(Permission("scope.edit_settings", scope_kind="workspace"))
    adjust_gate = Depends(Permission("inventory.adjust", scope_kind="workspace"))

    @api.get(
        "/properties/{property_id}/items",
        operation_id="inventory.items.list",
        openapi_extra={"x-cli": {"group": "inventory", "verb": "list"}},
        dependencies=[view_gate],
    )
    def list_items(
        property_id: str,
        ctx: _Ctx,
        session: _Db,
        barcode: Annotated[str | None, Query(max_length=_MAX_SHORT)] = None,
        below_reorder: bool = False,
    ) -> InventoryItemListResponse | InventoryItemResponse:
        try:
            if barcode is not None:
                return InventoryItemResponse.from_view(
                    item_service.get_by_barcode(
                        session,
                        ctx,
                        property_id=property_id,
                        barcode_ean13=barcode,
                    )
                )
            views = (
                item_service.list_low_stock(session, ctx, property_id=property_id)
                if below_reorder
                else item_service.list(session, ctx, property_id=property_id)
            )
        except item_service.InventoryPropertyNotFound as exc:
            raise _http_for_property_not_found() from exc
        except item_service.InventoryItemNotFound as exc:
            raise _http_for_not_found() from exc
        except item_service.InventoryItemValidationError as exc:
            raise _http_for_validation(exc) from exc
        return InventoryItemListResponse(
            data=[InventoryItemResponse.from_view(view) for view in views]
        )

    @api.get(
        "/reports/low_stock",
        operation_id="inventory.reports.low_stock",
        openapi_extra={"x-cli": {"group": "inventory", "verb": "low-stock"}},
        dependencies=[view_gate],
    )
    def low_stock_report(
        ctx: _Ctx,
        session: _Db,
        property_id: Annotated[str | None, Query(max_length=_MAX_SHORT)] = None,
    ) -> InventoryItemListResponse:
        try:
            views = item_service.list_low_stock(
                session,
                ctx,
                property_id=property_id,
            )
        except item_service.InventoryPropertyNotFound as exc:
            raise _http_for_property_not_found() from exc
        return InventoryItemListResponse(
            data=[InventoryItemResponse.from_view(view) for view in views]
        )

    @api.post(
        "/{item_id}/movements",
        status_code=status.HTTP_201_CREATED,
        operation_id="inventory.movements.create",
        openapi_extra={"x-cli": {"group": "inventory", "verb": "movement-create"}},
        dependencies=[adjust_gate],
    )
    def create_movement(
        item_id: str,
        body: InventoryMovementCreateRequest,
        ctx: _Ctx,
        session: _Db,
        _idempotency_key: _IdempotencyKey = None,
    ) -> InventoryMovementResponse:
        _ = _idempotency_key
        try:
            view = movement_service.record(
                session,
                ctx,
                item_id=item_id,
                delta=body.delta,
                reason=body.reason,
                source_task_id=body.resolved_source_task_id(),
                note=body.note,
            )
        except movement_service.InventoryItemNotFound as exc:
            raise _http_for_not_found() from exc
        except movement_service.InventoryMovementValidationError as exc:
            raise _http_for_movement_validation(exc) from exc
        return InventoryMovementResponse.from_view(view)

    @api.get(
        "/{item_id}/movements",
        operation_id="inventory.movements.list",
        openapi_extra={"x-cli": {"group": "inventory", "verb": "movements-list"}},
        dependencies=[view_gate],
    )
    def list_movements(
        item_id: str,
        ctx: _Ctx,
        session: _Db,
        before: Annotated[str | None, Query(max_length=1_024)] = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> InventoryMovementListResponse:
        try:
            views = list(
                movement_service.list_movements(
                    session,
                    ctx,
                    item_id=item_id,
                    before=_decode_movement_cursor(before),
                    limit=limit + 1,
                )
            )
        except movement_service.InventoryItemNotFound as exc:
            raise _http_for_not_found() from exc
        has_more = len(views) > limit
        items = views[:limit]
        next_cursor = (
            encode_cursor(_movement_cursor(items[-1])) if has_more and items else None
        )
        return InventoryMovementListResponse(
            data=[InventoryMovementResponse.from_view(view) for view in items],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    @api.post(
        "/{item_id}/adjust",
        status_code=status.HTTP_201_CREATED,
        operation_id="inventory.adjust",
        dependencies=[adjust_gate],
        openapi_extra={
            "x-cli": {"group": "inventory", "verb": "adjust"},
            "x-agent-confirm": {
                "message": "Record an inventory adjustment for this item?"
            },
        },
    )
    def adjust_item(
        item_id: str,
        body: InventoryAdjustRequest,
        ctx: _Ctx,
        session: _Db,
        _idempotency_key: _IdempotencyKey = None,
    ) -> InventoryMovementResponse:
        _ = _idempotency_key
        try:
            view = movement_service.adjust_to_observed(
                session,
                ctx,
                item_id=item_id,
                observed_qty=body.observed_on_hand,
                reason=body.reason,
                note=body.note,
            )
        except movement_service.InventoryItemNotFound as exc:
            raise _http_for_not_found() from exc
        except movement_service.InventoryMovementValidationError as exc:
            raise _http_for_movement_validation(exc) from exc
        return InventoryMovementResponse.from_view(view)

    @api.get(
        "/properties/{property_id}/items/by_sku/{sku}",
        operation_id="inventory.items.get_by_sku",
        openapi_extra={"x-cli": {"group": "inventory", "verb": "get-by-sku"}},
        dependencies=[view_gate],
    )
    def get_item_by_sku(
        property_id: str,
        sku: str,
        ctx: _Ctx,
        session: _Db,
    ) -> InventoryItemResponse:
        try:
            return InventoryItemResponse.from_view(
                item_service.get_by_sku(session, ctx, property_id=property_id, sku=sku)
            )
        except item_service.InventoryPropertyNotFound as exc:
            raise _http_for_property_not_found() from exc
        except item_service.InventoryItemNotFound as exc:
            raise _http_for_not_found() from exc
        except item_service.InventoryItemValidationError as exc:
            raise _http_for_validation(exc) from exc

    @api.get(
        "/properties/{property_id}/items/by_barcode/{barcode_ean13}",
        operation_id="inventory.items.get_by_barcode",
        openapi_extra={"x-cli": {"group": "inventory", "verb": "get-by-barcode"}},
        dependencies=[view_gate],
    )
    def get_item_by_barcode(
        property_id: str,
        barcode_ean13: str,
        ctx: _Ctx,
        session: _Db,
    ) -> InventoryItemResponse:
        try:
            return InventoryItemResponse.from_view(
                item_service.get_by_barcode(
                    session,
                    ctx,
                    property_id=property_id,
                    barcode_ean13=barcode_ean13,
                )
            )
        except item_service.InventoryPropertyNotFound as exc:
            raise _http_for_property_not_found() from exc
        except item_service.InventoryItemNotFound as exc:
            raise _http_for_not_found() from exc
        except item_service.InventoryItemValidationError as exc:
            raise _http_for_validation(exc) from exc

    @api.post(
        "/properties/{property_id}/items",
        status_code=status.HTTP_201_CREATED,
        operation_id="inventory.items.create",
        openapi_extra={"x-cli": {"group": "inventory", "verb": "add"}},
        dependencies=[edit_gate],
    )
    def create_item(
        property_id: str,
        body: InventoryItemCreateRequest,
        ctx: _Ctx,
        session: _Db,
        _idempotency_key: _IdempotencyKey = None,
    ) -> InventoryItemResponse:
        _ = _idempotency_key
        try:
            view = item_service.create(
                session,
                ctx,
                property_id=property_id,
                body=body.to_service(),
            )
        except item_service.InventoryPropertyNotFound as exc:
            raise _http_for_property_not_found() from exc
        except item_service.InventoryItemConflict as exc:
            raise _http_for_conflict(exc) from exc
        except item_service.InventoryItemValidationError as exc:
            raise _http_for_validation(exc) from exc
        return InventoryItemResponse.from_view(view)

    @api.patch(
        "/properties/{property_id}/items/{item_id}",
        operation_id="inventory.items.update",
        openapi_extra={"x-cli": {"group": "inventory", "verb": "update"}},
        dependencies=[edit_gate],
    )
    def update_item(
        property_id: str,
        item_id: str,
        body: InventoryItemUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> InventoryItemResponse:
        try:
            view = item_service.update(
                session,
                ctx,
                property_id=property_id,
                item_id=item_id,
                body=body.to_service(),
            )
        except item_service.InventoryItemNotFound as exc:
            raise _http_for_not_found() from exc
        except item_service.InventoryItemConflict as exc:
            raise _http_for_conflict(exc) from exc
        except item_service.InventoryItemValidationError as exc:
            raise _http_for_validation(exc) from exc
        return InventoryItemResponse.from_view(view)

    @api.delete(
        "/properties/{property_id}/items/{item_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="inventory.items.delete",
        openapi_extra={"x-cli": {"group": "inventory", "verb": "delete"}},
        dependencies=[edit_gate],
    )
    def archive_item(
        property_id: str,
        item_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        try:
            item_service.archive(
                session,
                ctx,
                property_id=property_id,
                item_id=item_id,
            )
        except item_service.InventoryItemNotFound as exc:
            raise _http_for_not_found() from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post(
        "/properties/{property_id}/items/{item_id}/restore",
        operation_id="inventory.items.restore",
        openapi_extra={"x-cli": {"group": "inventory", "verb": "restore"}},
        dependencies=[edit_gate],
    )
    def restore_item(
        property_id: str,
        item_id: str,
        ctx: _Ctx,
        session: _Db,
        _idempotency_key: _IdempotencyKey = None,
    ) -> InventoryItemResponse:
        _ = _idempotency_key
        try:
            view = item_service.restore(
                session,
                ctx,
                property_id=property_id,
                item_id=item_id,
            )
        except item_service.InventoryItemNotFound as exc:
            raise _http_for_not_found() from exc
        except item_service.InventoryItemConflict as exc:
            raise _http_for_conflict(exc) from exc
        return InventoryItemResponse.from_view(view)

    return api


router = build_inventory_router()
