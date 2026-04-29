"""Billing work-order HTTP routes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.adapters.db.billing.repositories import SqlAlchemyWorkOrderRepository
from app.api.deps import current_workspace_context, db_session
from app.authz.dep import Permission
from app.domain.billing.work_orders import (
    WorkOrderCreate,
    WorkOrderInvalid,
    WorkOrderNotFound,
    WorkOrderPatch,
    WorkOrderService,
    WorkOrderView,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "WorkOrderCreateRequest",
    "WorkOrderListResponse",
    "WorkOrderPatchRequest",
    "WorkOrderResponse",
    "build_work_orders_router",
]

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_Status = Literal["draft", "sent", "in_progress", "completed", "invoiced"]


class WorkOrderResponse(BaseModel):
    id: str
    workspace_id: str
    organization_id: str
    property_id: str
    title: str
    status: str
    starts_at: datetime
    ends_at: datetime | None
    rate_card_id: str | None
    total_hours_decimal: Decimal
    total_cents: int

    @classmethod
    def from_view(cls, view: WorkOrderView) -> WorkOrderResponse:
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            organization_id=view.organization_id,
            property_id=view.property_id,
            title=view.title,
            status=view.status,
            starts_at=view.starts_at,
            ends_at=view.ends_at,
            rate_card_id=view.rate_card_id,
            total_hours_decimal=view.total_hours_decimal,
            total_cents=view.total_cents,
        )


class WorkOrderListResponse(BaseModel):
    data: list[WorkOrderResponse]


class WorkOrderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: str = Field(min_length=1, max_length=64)
    property_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    starts_at: datetime
    ends_at: datetime | None = None
    rate_card_id: str | None = Field(default=None, min_length=1, max_length=64)

    def to_domain(self) -> WorkOrderCreate:
        return WorkOrderCreate(
            organization_id=self.organization_id,
            property_id=self.property_id,
            title=self.title,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            rate_card_id=self.rate_card_id,
        )


class WorkOrderPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    rate_card_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _has_mutation(self) -> WorkOrderPatchRequest:
        if not self.model_fields_set:
            raise ValueError("PATCH body must include at least one field")
        return self

    def to_domain(self) -> WorkOrderPatch:
        fields: dict[str, object | None] = {}
        for field in self.model_fields_set:
            fields[field] = getattr(self, field)
        return WorkOrderPatch(fields=fields)


def _http_for_work_order_error(exc: Exception) -> HTTPException:
    if isinstance(exc, WorkOrderNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "work_order_not_found", "message": str(exc)},
        )
    if isinstance(exc, WorkOrderInvalid):
        return HTTPException(
            status_code=422,
            detail={"error": "work_order_invalid", "message": str(exc)},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


def build_work_orders_router() -> APIRouter:
    router = APIRouter(prefix="/work-orders", tags=["billing", "work-orders"])

    view_gate = Depends(Permission("work_orders.view", scope_kind="workspace"))
    edit_gate = Depends(Permission("work_orders.create", scope_kind="workspace"))

    @router.get(
        "",
        response_model=WorkOrderListResponse,
        operation_id="billing.work_orders.list",
        dependencies=[view_gate],
        summary="List billing work orders",
    )
    def list_work_orders(
        ctx: _Ctx,
        session: _Db,
        organization_id: str | None = None,
        property_id: str | None = None,
        status: _Status | None = None,
    ) -> WorkOrderListResponse:
        try:
            views = WorkOrderService(ctx).list(
                SqlAlchemyWorkOrderRepository(session),
                organization_id=organization_id,
                property_id=property_id,
                status=status,
            )
        except WorkOrderInvalid as exc:
            raise _http_for_work_order_error(exc) from exc
        return WorkOrderListResponse(
            data=[WorkOrderResponse.from_view(view) for view in views]
        )

    @router.post(
        "",
        response_model=WorkOrderResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="billing.work_orders.create",
        dependencies=[edit_gate],
        summary="Create a billing work order",
    )
    def create_work_order(
        body: WorkOrderCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> WorkOrderResponse:
        try:
            view = WorkOrderService(ctx).create(
                SqlAlchemyWorkOrderRepository(session),
                body.to_domain(),
            )
        except (WorkOrderInvalid, WorkOrderNotFound) as exc:
            raise _http_for_work_order_error(exc) from exc
        return WorkOrderResponse.from_view(view)

    @router.get(
        "/{work_order_id}",
        response_model=WorkOrderResponse,
        operation_id="billing.work_orders.get",
        dependencies=[view_gate],
        summary="Get a billing work order",
    )
    def get_work_order(
        work_order_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> WorkOrderResponse:
        try:
            view = WorkOrderService(ctx).get(
                SqlAlchemyWorkOrderRepository(session),
                work_order_id,
            )
        except WorkOrderNotFound as exc:
            raise _http_for_work_order_error(exc) from exc
        return WorkOrderResponse.from_view(view)

    @router.patch(
        "/{work_order_id}",
        response_model=WorkOrderResponse,
        operation_id="billing.work_orders.update",
        dependencies=[edit_gate],
        summary="Update a billing work order",
    )
    def patch_work_order(
        work_order_id: str,
        body: WorkOrderPatchRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> WorkOrderResponse:
        try:
            view = WorkOrderService(ctx).update(
                SqlAlchemyWorkOrderRepository(session),
                work_order_id,
                body.to_domain(),
            )
        except (WorkOrderInvalid, WorkOrderNotFound) as exc:
            raise _http_for_work_order_error(exc) from exc
        return WorkOrderResponse.from_view(view)

    @router.post(
        "/{work_order_id}/in-progress",
        response_model=WorkOrderResponse,
        operation_id="billing.work_orders.mark_in_progress",
        dependencies=[edit_gate],
        summary="Move a billing work order into progress",
    )
    def mark_work_order_in_progress(
        work_order_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> WorkOrderResponse:
        try:
            view = WorkOrderService(ctx).mark_in_progress(
                SqlAlchemyWorkOrderRepository(session),
                work_order_id,
            )
        except (WorkOrderInvalid, WorkOrderNotFound) as exc:
            raise _http_for_work_order_error(exc) from exc
        return WorkOrderResponse.from_view(view)

    @router.post(
        "/{work_order_id}/complete",
        response_model=WorkOrderResponse,
        operation_id="billing.work_orders.complete",
        dependencies=[edit_gate],
        summary="Complete a billing work order",
    )
    def complete_work_order(
        work_order_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> WorkOrderResponse:
        try:
            view = WorkOrderService(ctx).complete(
                SqlAlchemyWorkOrderRepository(session),
                work_order_id,
            )
        except (WorkOrderInvalid, WorkOrderNotFound) as exc:
            raise _http_for_work_order_error(exc) from exc
        return WorkOrderResponse.from_view(view)

    @router.post(
        "/{work_order_id}/invoice",
        response_model=WorkOrderResponse,
        operation_id="billing.work_orders.invoice",
        dependencies=[edit_gate],
        summary="Invoice a completed billing work order",
    )
    def invoice_work_order(
        work_order_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> WorkOrderResponse:
        try:
            view = WorkOrderService(ctx).invoice(
                SqlAlchemyWorkOrderRepository(session),
                work_order_id,
            )
        except (WorkOrderInvalid, WorkOrderNotFound) as exc:
            raise _http_for_work_order_error(exc) from exc
        return WorkOrderResponse.from_view(view)

    return router
