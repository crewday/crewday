"""Inventory report queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.adapters.db.inventory.models import Item, Movement, Stocktake
from app.adapters.db.places.models import PropertyWorkspace
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock

__all__ = [
    "InventoryRateReportRow",
    "InventoryReportPropertyNotFound",
    "InventoryShrinkageReportRow",
    "InventoryStocktakeActivityRow",
    "production_rate",
    "shrinkage",
    "stocktake_activity",
]


class InventoryReportPropertyNotFound(LookupError):
    """No active property matched the workspace/property filter."""


@dataclass(frozen=True, slots=True)
class InventoryRateReportRow:
    property_id: str
    item_id: str
    item_name: str
    sku: str | None
    unit: str
    total_qty: Decimal
    daily_avg: Decimal
    window_days: int


@dataclass(frozen=True, slots=True)
class InventoryShrinkageReportRow:
    property_id: str
    item_id: str
    item_name: str
    sku: str | None
    unit: str
    theft_qty: Decimal
    loss_qty: Decimal
    audit_correction_qty: Decimal
    shrinkage_qty: Decimal
    window_days: int


@dataclass(frozen=True, slots=True)
class InventoryStocktakeActivityRow:
    stocktake_id: str
    property_id: str
    started_at: str
    completed_at: str | None
    actor_kind: str
    actor_id: str | None
    movement_count: int
    absolute_delta: Decimal
    net_delta: Decimal


def production_rate(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str | None = None,
    window_days: int = 30,
    clock: Clock | None = None,
) -> tuple[InventoryRateReportRow, ...]:
    """Return average daily production by item and property."""
    _ensure_property(session, ctx, property_id)
    since = _since(window_days=window_days, clock=clock)
    total_qty = func.coalesce(func.sum(Movement.delta), Decimal("0"))
    stmt = (
        select(
            Item.property_id,
            Item.id,
            Item.name,
            Item.sku,
            Item.unit,
            total_qty,
        )
        .join(Movement, Movement.item_id == Item.id)
        .where(
            Item.workspace_id == ctx.workspace_id,
            Movement.workspace_id == ctx.workspace_id,
            Item.deleted_at.is_(None),
            Movement.reason == "produce",
            Movement.at >= since,
        )
        .group_by(Item.property_id, Item.id, Item.name, Item.sku, Item.unit)
        .order_by(Item.property_id, Item.name, Item.id)
    )
    if property_id is not None:
        stmt = stmt.where(Item.property_id == property_id)
    rows = session.execute(stmt).all()
    return tuple(
        InventoryRateReportRow(
            property_id=row[0],
            item_id=row[1],
            item_name=row[2],
            sku=row[3],
            unit=row[4],
            total_qty=_decimal(row[5]),
            daily_avg=_decimal(row[5]) / Decimal(window_days),
            window_days=window_days,
        )
        for row in rows
    )


def shrinkage(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str | None = None,
    window_days: int = 30,
    clock: Clock | None = None,
) -> tuple[InventoryShrinkageReportRow, ...]:
    """Return theft, loss, and unexplained negative audit deltas."""
    _ensure_property(session, ctx, property_id)
    since = _since(window_days=window_days, clock=clock)
    theft_qty: ColumnElement[Decimal] = _positive_sum("theft")
    loss_qty: ColumnElement[Decimal] = _positive_sum("loss")
    audit_qty: ColumnElement[Decimal] = func.coalesce(
        func.sum(
            case(
                (Movement.reason == "audit_correction", _negative_abs(Movement.delta)),
                else_=Decimal("0"),
            )
        ),
        Decimal("0"),
    )
    stmt = (
        select(
            Item.property_id,
            Item.id,
            Item.name,
            Item.sku,
            Item.unit,
            theft_qty,
            loss_qty,
            audit_qty,
        )
        .join(Movement, Movement.item_id == Item.id)
        .where(
            Item.workspace_id == ctx.workspace_id,
            Movement.workspace_id == ctx.workspace_id,
            Item.deleted_at.is_(None),
            (Movement.reason.in_(("theft", "loss")))
            | (
                (Movement.reason == "audit_correction")
                & (Movement.delta < Decimal("0"))
            ),
            Movement.at >= since,
        )
        .group_by(Item.property_id, Item.id, Item.name, Item.sku, Item.unit)
        .order_by(Item.property_id, Item.name, Item.id)
    )
    if property_id is not None:
        stmt = stmt.where(Item.property_id == property_id)
    rows = session.execute(stmt).all()
    return tuple(
        InventoryShrinkageReportRow(
            property_id=row[0],
            item_id=row[1],
            item_name=row[2],
            sku=row[3],
            unit=row[4],
            theft_qty=_decimal(row[5]),
            loss_qty=_decimal(row[6]),
            audit_correction_qty=_decimal(row[7]),
            shrinkage_qty=_decimal(row[5]) + _decimal(row[6]) + _decimal(row[7]),
            window_days=window_days,
        )
        for row in rows
    )


def stocktake_activity(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str | None = None,
    limit: int,
) -> tuple[InventoryStocktakeActivityRow, ...]:
    """Return recent stocktake sessions with movement totals."""
    _ensure_property(session, ctx, property_id)
    movement_count = func.count(Movement.id)
    absolute_delta = func.coalesce(func.sum(func.abs(Movement.delta)), Decimal("0"))
    net_delta = func.coalesce(func.sum(Movement.delta), Decimal("0"))
    stmt = (
        select(
            Stocktake.id,
            Stocktake.property_id,
            Stocktake.started_at,
            Stocktake.completed_at,
            Stocktake.actor_kind,
            Stocktake.actor_id,
            movement_count,
            absolute_delta,
            net_delta,
        )
        .outerjoin(
            Movement,
            (Movement.workspace_id == Stocktake.workspace_id)
            & (Movement.source_stocktake_id == Stocktake.id),
        )
        .where(Stocktake.workspace_id == ctx.workspace_id)
        .group_by(
            Stocktake.id,
            Stocktake.property_id,
            Stocktake.started_at,
            Stocktake.completed_at,
            Stocktake.actor_kind,
            Stocktake.actor_id,
        )
        .order_by(Stocktake.started_at.desc(), Stocktake.id.desc())
        .limit(limit)
    )
    if property_id is not None:
        stmt = stmt.where(Stocktake.property_id == property_id)
    rows = session.execute(stmt).all()
    return tuple(
        InventoryStocktakeActivityRow(
            stocktake_id=row[0],
            property_id=row[1],
            started_at=row[2].isoformat(),
            completed_at=row[3].isoformat() if row[3] is not None else None,
            actor_kind=row[4],
            actor_id=row[5],
            movement_count=int(row[6]),
            absolute_delta=_decimal(row[7]),
            net_delta=_decimal(row[8]),
        )
        for row in rows
    )


def _ensure_property(
    session: Session, ctx: WorkspaceContext, property_id: str | None
) -> None:
    if property_id is None:
        return
    row = session.scalar(
        select(PropertyWorkspace.property_id)
        .where(
            PropertyWorkspace.workspace_id == ctx.workspace_id,
            PropertyWorkspace.property_id == property_id,
            PropertyWorkspace.status == "active",
        )
        .limit(1)
    )
    if row is None:
        raise InventoryReportPropertyNotFound("property not found in workspace")


def _since(*, window_days: int, clock: Clock | None) -> object:
    resolved_clock = clock if clock is not None else SystemClock()
    return resolved_clock.now() - timedelta(days=window_days)


def _positive_sum(reason: str) -> ColumnElement[Decimal]:
    return func.coalesce(
        func.sum(
            case(
                (Movement.reason == reason, _negative_abs(Movement.delta)),
                else_=Decimal("0"),
            )
        ),
        Decimal("0"),
    )


def _negative_abs(
    value: ColumnElement[Decimal] | InstrumentedAttribute[Decimal],
) -> ColumnElement[Decimal]:
    return case((value < Decimal("0"), -value), else_=Decimal("0"))


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
