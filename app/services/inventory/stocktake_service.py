"""Property-wide inventory stocktake service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.adapters.db.inventory.models import Item, Stocktake, StocktakeLine
from app.adapters.db.places.models import PropertyWorkspace
from app.audit import write_audit
from app.authz import InvalidScope, PermissionDenied, UnknownActionKey, require
from app.events.bus import EventBus
from app.services.inventory import movement_service
from app.services.inventory.movement_service import InventoryMovementView
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "ABANDONED_NOTE",
    "StocktakeAlreadyCommitted",
    "StocktakeLineView",
    "StocktakeNotFound",
    "StocktakePermissionDenied",
    "StocktakeValidationError",
    "StocktakeView",
    "abandon_stale",
    "commit",
    "open",
    "save_line",
]


ABANDONED_NOTE = "abandoned — no movements written"
_DEFAULT_REASON: movement_service.MovementReason = "audit_correction"
_QTY_QUANTUM = Decimal("0.0001")
_REASON_BY_VALUE: dict[str, movement_service.MovementReason] = {
    "restock": "restock",
    "consume": "consume",
    "produce": "produce",
    "waste": "waste",
    "theft": "theft",
    "loss": "loss",
    "found": "found",
    "returned_to_vendor": "returned_to_vendor",
    "transfer_in": "transfer_in",
    "transfer_out": "transfer_out",
    "audit_correction": "audit_correction",
    "adjust": "adjust",
}


class StocktakeNotFound(LookupError):
    """No stocktake session matched the workspace/id filter."""


class StocktakeAlreadyCommitted(RuntimeError):
    """The stocktake session is no longer mutable."""


class StocktakePermissionDenied(PermissionError):
    """Caller lacks ``inventory.stocktake`` for the target property."""


class StocktakeValidationError(ValueError):
    """Submitted stocktake data failed service-level validation."""

    __slots__ = ("error", "field")

    def __init__(self, field: str, error: str) -> None:
        super().__init__(f"{field}: {error}")
        self.field = field
        self.error = error


@dataclass(frozen=True, slots=True)
class StocktakeView:
    id: str
    workspace_id: str
    property_id: str
    started_at: datetime
    completed_at: datetime | None
    actor_kind: str
    actor_id: str | None
    note_md: str | None


@dataclass(frozen=True, slots=True)
class StocktakeLineView:
    stocktake_id: str
    item_id: str
    workspace_id: str
    observed_on_hand: Decimal
    reason: str
    note: str | None
    updated_at: datetime


def open(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    note_md: str | None = None,
    clock: Clock | None = None,
) -> StocktakeView:
    """Open a stocktake session for one active property."""
    _ensure_property(session, ctx, property_id)
    _require_stocktake(session, ctx, property_id=property_id)
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = Stocktake(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        property_id=property_id,
        started_at=now,
        completed_at=None,
        actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id if ctx.actor_kind != "system" else None,
        note_md=_clean_optional(note_md),
    )
    session.add(row)
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="inventory_stocktake",
        entity_id=row.id,
        action="inventory_stocktake.opened",
        diff={"after": _stocktake_audit(row)},
        clock=resolved_clock,
    )
    return _project(row)


def save_line(
    session: Session,
    ctx: WorkspaceContext,
    *,
    stocktake_id: str,
    item_id: str,
    observed: Decimal,
    reason: movement_service.MovementReason = _DEFAULT_REASON,
    note: str | None = None,
    clock: Clock | None = None,
) -> StocktakeLineView:
    """Create or replace the draft observed count for one item."""
    stocktake = _load_open_stocktake(session, ctx, stocktake_id)
    _require_stocktake(session, ctx, property_id=stocktake.property_id)
    item = _load_active_item_for_stocktake(session, ctx, stocktake, item_id)
    observed_on_hand = _clean_quantity(observed, field_name="observed")
    _validate_reason(reason)
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = session.get(StocktakeLine, (stocktake.id, item.id))
    clean_note = _clean_optional(note)
    if row is None:
        row = StocktakeLine(
            stocktake_id=stocktake.id,
            item_id=item.id,
            workspace_id=ctx.workspace_id,
            observed_on_hand=observed_on_hand,
            reason=reason,
            note=clean_note,
            updated_at=now,
        )
        session.add(row)
    else:
        row.observed_on_hand = observed_on_hand
        row.reason = reason
        row.note = clean_note
        row.updated_at = now
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="inventory_stocktake",
        entity_id=stocktake.id,
        action="inventory_stocktake.line_saved",
        diff={
            "after": {
                "item_id": item.id,
                "observed_on_hand": str(observed_on_hand),
                "reason": reason,
            }
        },
        clock=resolved_clock,
    )
    return _project_line(row)


def commit(
    session: Session,
    ctx: WorkspaceContext,
    *,
    stocktake_id: str,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> tuple[InventoryMovementView, ...]:
    """Commit a stocktake by writing one movement per non-zero draft delta."""
    resolved_clock = clock if clock is not None else SystemClock()
    with session.begin_nested():
        stocktake = _load_stocktake_for_update(session, ctx, stocktake_id)
        if stocktake.completed_at is not None:
            raise StocktakeAlreadyCommitted("stocktake already completed")
        _require_stocktake(session, ctx, property_id=stocktake.property_id)
        lines = tuple(_load_lines(session, ctx, stocktake_id=stocktake.id))
        movements: list[InventoryMovementView] = []
        for line in lines:
            item = _load_active_item_for_stocktake(
                session, ctx, stocktake, line.item_id
            )
            delta = line.observed_on_hand - _clean_quantity(
                item.on_hand, field_name="on_hand"
            )
            if delta == Decimal("0"):
                continue
            movements.append(
                movement_service.reconcile(
                    session,
                    ctx,
                    item_id=item.id,
                    observed_qty=line.observed_on_hand,
                    reason=_reason_literal(line.reason),
                    source_stocktake_id=stocktake.id,
                    note=line.note,
                    clock=clock,
                    event_bus=event_bus,
                )
            )
        stocktake.completed_at = resolved_clock.now()
        session.execute(
            delete(StocktakeLine).where(
                StocktakeLine.workspace_id == ctx.workspace_id,
                StocktakeLine.stocktake_id == stocktake.id,
            )
        )
        write_audit(
            session,
            ctx,
            entity_kind="inventory_stocktake",
            entity_id=stocktake.id,
            action="inventory_stocktake.committed",
            diff={
                "after": {
                    "completed_at": stocktake.completed_at.isoformat(),
                    "movement_ids": [movement.id for movement in movements],
                }
            },
            clock=resolved_clock,
        )
    return tuple(movements)


def abandon_stale(
    session: Session,
    ctx: WorkspaceContext,
    *,
    max_age: timedelta = timedelta(hours=24),
    clock: Clock | None = None,
) -> int:
    """Mark open stocktakes older than ``max_age`` abandoned without movements."""
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    cutoff = now - max_age
    rows = tuple(
        session.scalars(
            select(Stocktake)
            .where(
                Stocktake.workspace_id == ctx.workspace_id,
                Stocktake.completed_at.is_(None),
                Stocktake.started_at < cutoff,
            )
            .with_for_update()
        ).all()
    )
    for row in rows:
        row.completed_at = now
        row.note_md = ABANDONED_NOTE
        session.execute(
            delete(StocktakeLine).where(
                StocktakeLine.workspace_id == ctx.workspace_id,
                StocktakeLine.stocktake_id == row.id,
            )
        )
        write_audit(
            session,
            ctx,
            entity_kind="inventory_stocktake",
            entity_id=row.id,
            action="inventory_stocktake.abandoned",
            diff={"after": _stocktake_audit(row)},
            clock=resolved_clock,
        )
    session.flush()
    return len(rows)


def _ensure_property(session: Session, ctx: WorkspaceContext, property_id: str) -> None:
    row = session.scalar(
        select(PropertyWorkspace.property_id).where(
            PropertyWorkspace.workspace_id == ctx.workspace_id,
            PropertyWorkspace.property_id == property_id,
            PropertyWorkspace.status == "active",
        )
    )
    if row is None:
        raise StocktakeNotFound("property not found in workspace")


def _require_stocktake(
    session: Session, ctx: WorkspaceContext, *, property_id: str
) -> None:
    try:
        require(
            session,
            ctx,
            action_key="inventory.stocktake",
            scope_kind="property",
            scope_id=property_id,
        )
    except (UnknownActionKey, InvalidScope) as exc:
        raise RuntimeError(
            f"authz catalog misconfigured for 'inventory.stocktake': {exc!s}"
        ) from exc
    except PermissionDenied as exc:
        raise StocktakePermissionDenied("inventory.stocktake") from exc


def _load_stocktake_for_update(
    session: Session, ctx: WorkspaceContext, stocktake_id: str
) -> Stocktake:
    row = session.scalar(
        select(Stocktake)
        .where(Stocktake.workspace_id == ctx.workspace_id, Stocktake.id == stocktake_id)
        .with_for_update()
    )
    if row is None:
        raise StocktakeNotFound("stocktake not found")
    return row


def _load_open_stocktake(
    session: Session, ctx: WorkspaceContext, stocktake_id: str
) -> Stocktake:
    row = _load_stocktake_for_update(session, ctx, stocktake_id)
    if row.completed_at is not None:
        raise StocktakeAlreadyCommitted("stocktake already completed")
    return row


def _load_active_item_for_stocktake(
    session: Session,
    ctx: WorkspaceContext,
    stocktake: Stocktake,
    item_id: str,
) -> Item:
    row = session.scalar(
        select(Item)
        .where(
            Item.workspace_id == ctx.workspace_id,
            Item.property_id == stocktake.property_id,
            Item.id == item_id,
            Item.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        raise StocktakeNotFound("active inventory item not found")
    return row


def _load_lines(
    session: Session, ctx: WorkspaceContext, *, stocktake_id: str
) -> tuple[StocktakeLine, ...]:
    return tuple(
        session.scalars(
            select(StocktakeLine)
            .where(
                StocktakeLine.workspace_id == ctx.workspace_id,
                StocktakeLine.stocktake_id == stocktake_id,
            )
            .order_by(StocktakeLine.item_id)
        ).all()
    )


def _clean_quantity(value: Decimal, *, field_name: str) -> Decimal:
    if not value.is_finite():
        raise StocktakeValidationError(field_name, "quantity_invalid")
    try:
        quantized = value.quantize(_QTY_QUANTUM)
    except InvalidOperation as exc:
        raise StocktakeValidationError(field_name, "quantity_precision") from exc
    if value != quantized:
        raise StocktakeValidationError(field_name, "quantity_precision")
    return quantized


def _validate_reason(reason: str) -> None:
    if reason not in _REASON_BY_VALUE:
        raise StocktakeValidationError("reason", "invalid")


def _reason_literal(reason: str) -> movement_service.MovementReason:
    try:
        return _REASON_BY_VALUE[reason]
    except KeyError as exc:
        raise StocktakeValidationError("reason", "invalid") from exc


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _project(row: Stocktake) -> StocktakeView:
    return StocktakeView(
        id=row.id,
        workspace_id=row.workspace_id,
        property_id=row.property_id,
        started_at=row.started_at,
        completed_at=row.completed_at,
        actor_kind=row.actor_kind,
        actor_id=row.actor_id,
        note_md=row.note_md,
    )


def _project_line(row: StocktakeLine) -> StocktakeLineView:
    return StocktakeLineView(
        stocktake_id=row.stocktake_id,
        item_id=row.item_id,
        workspace_id=row.workspace_id,
        observed_on_hand=row.observed_on_hand,
        reason=row.reason,
        note=row.note,
        updated_at=row.updated_at,
    )


def _stocktake_audit(row: Stocktake) -> dict[str, object]:
    return {
        "id": row.id,
        "property_id": row.property_id,
        "started_at": row.started_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "actor_kind": row.actor_kind,
        "actor_id": row.actor_id,
        "note_md": row.note_md,
    }
