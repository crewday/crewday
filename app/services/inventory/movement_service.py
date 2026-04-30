"""Append-only inventory movement service."""

from __future__ import annotations

import weakref
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from app.adapters.db.inventory.models import Item, Movement, Stocktake
from app.adapters.db.tasks.models import Occurrence
from app.audit import write_audit
from app.authz import InvalidScope, PermissionDenied, UnknownActionKey, require
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import InventoryItemChanged
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "InventoryItemNotFound",
    "InventoryMovementPermissionDenied",
    "InventoryMovementValidationError",
    "InventoryMovementView",
    "MovementReason",
    "adjust_to_observed",
    "consume",
    "ensure_active_item",
    "list_movements",
    "produce",
    "reconcile",
    "record",
    "require_adjust_for_item",
    "restock",
    "transfer",
]


MovementReason = Literal[
    "restock",
    "consume",
    "produce",
    "waste",
    "theft",
    "loss",
    "found",
    "returned_to_vendor",
    "transfer_in",
    "transfer_out",
    "audit_correction",
    "adjust",
]

_QTY_QUANTUM = Decimal("0.0001")
_REASONS: frozenset[str] = frozenset(
    {
        "restock",
        "consume",
        "produce",
        "waste",
        "theft",
        "loss",
        "found",
        "returned_to_vendor",
        "transfer_in",
        "transfer_out",
        "audit_correction",
        "adjust",
    }
)
_POSITIVE_REASONS: frozenset[str] = frozenset(
    {"restock", "produce", "found", "transfer_in"}
)
_NEGATIVE_REASONS: frozenset[str] = frozenset(
    {"consume", "waste", "theft", "loss", "returned_to_vendor", "transfer_out"}
)


class InventoryItemNotFound(LookupError):
    """No active item matched the workspace/id filter."""


class InventoryMovementValidationError(ValueError):
    """Submitted movement data failed service-level validation."""

    __slots__ = ("error", "field")

    def __init__(self, field: str, error: str) -> None:
        super().__init__(f"{field}: {error}")
        self.field = field
        self.error = error


class InventoryMovementPermissionDenied(PermissionError):
    """Caller lacks ``inventory.adjust`` for the target property."""


@dataclass(frozen=True, slots=True)
class InventoryMovementView:
    id: str
    workspace_id: str
    item_id: str
    delta: Decimal
    reason: str
    source_task_id: str | None
    source_stocktake_id: str | None
    actor_kind: str
    actor_id: str | None
    at: datetime
    note: str | None
    on_hand_after: Decimal


@dataclass(frozen=True, slots=True)
class _PendingInventoryEvent:
    bus: EventBus
    event: InventoryItemChanged


_PENDING_EVENTS: weakref.WeakKeyDictionary[Session, list[_PendingInventoryEvent]] = (
    weakref.WeakKeyDictionary()
)
_HOOKED_SESSIONS: weakref.WeakSet[Session] = weakref.WeakSet()


def restock(
    session: Session,
    ctx: WorkspaceContext,
    *,
    item_id: str,
    qty: Decimal,
    source_task_id: str | None = None,
    note: str | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> InventoryMovementView:
    """Record a positive restock movement."""
    return _write_movement(
        session,
        ctx,
        item_id=item_id,
        delta=_clean_magnitude(qty, field_name="qty"),
        reason="restock",
        source_task_id=source_task_id,
        source_stocktake_id=None,
        note=_clean_optional_note(note),
        clock=clock,
        event_bus=event_bus,
    )


def consume(
    session: Session,
    ctx: WorkspaceContext,
    *,
    item_id: str,
    qty: Decimal,
    source_task_id: str | None = None,
    note: str | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> InventoryMovementView:
    """Record a negative consumption movement; negative on-hand is allowed."""
    return _write_movement(
        session,
        ctx,
        item_id=item_id,
        delta=-_clean_magnitude(qty, field_name="qty"),
        reason="consume",
        source_task_id=source_task_id,
        source_stocktake_id=None,
        note=_clean_optional_note(note),
        clock=clock,
        event_bus=event_bus,
    )


def produce(
    session: Session,
    ctx: WorkspaceContext,
    *,
    item_id: str,
    qty: Decimal,
    source_task_id: str | None = None,
    note: str | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> InventoryMovementView:
    """Record a positive task-production movement."""
    return _write_movement(
        session,
        ctx,
        item_id=item_id,
        delta=_clean_magnitude(qty, field_name="qty"),
        reason="produce",
        source_task_id=source_task_id,
        source_stocktake_id=None,
        note=_clean_optional_note(note),
        clock=clock,
        event_bus=event_bus,
    )


def record(
    session: Session,
    ctx: WorkspaceContext,
    *,
    item_id: str,
    delta: Decimal,
    reason: MovementReason,
    source_task_id: str | None = None,
    note: str | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> InventoryMovementView:
    """Record one signed inventory movement from the REST/API surface."""
    clean_delta = _clean_quantity(delta, field_name="delta")
    _validate_reason_sign(reason, clean_delta)
    return _write_movement(
        session,
        ctx,
        item_id=item_id,
        delta=clean_delta,
        reason=reason,
        source_task_id=source_task_id,
        source_stocktake_id=None,
        note=_clean_optional_note(note),
        clock=clock,
        event_bus=event_bus,
    )


def require_adjust_for_item(
    session: Session,
    ctx: WorkspaceContext,
    *,
    item_id: str,
) -> None:
    """Require ``inventory.adjust`` for the item's property."""
    item = _load_active_item(session, ctx, item_id)
    if item.property_id is None:
        raise InventoryMovementValidationError("item_id", "property_required")
    try:
        require(
            session,
            ctx,
            action_key="inventory.adjust",
            scope_kind="property",
            scope_id=item.property_id,
        )
    except (UnknownActionKey, InvalidScope) as exc:
        raise RuntimeError(
            f"authz catalog misconfigured for 'inventory.adjust': {exc!s}"
        ) from exc
    except PermissionDenied as exc:
        raise InventoryMovementPermissionDenied("inventory.adjust") from exc


def ensure_active_item(
    session: Session,
    ctx: WorkspaceContext,
    *,
    item_id: str,
) -> None:
    """Raise when the item is not readable in the current workspace."""
    _ensure_active_item(session, ctx, item_id)


def list_movements(
    session: Session,
    ctx: WorkspaceContext,
    *,
    item_id: str,
    before: tuple[datetime, str | None] | None = None,
    limit: int,
) -> tuple[InventoryMovementView, ...]:
    """List an item's append-only movement ledger newest first."""
    _ensure_active_item(session, ctx, item_id)
    stmt = (
        select(Movement)
        .where(
            Movement.workspace_id == ctx.workspace_id,
            Movement.item_id == item_id,
        )
        .order_by(Movement.at.desc(), Movement.id.desc())
        .limit(limit)
    )
    if before is not None:
        before_at, before_id = before
        if before_id is None:
            stmt = stmt.where(Movement.at < before_at)
        else:
            stmt = stmt.where(
                (Movement.at < before_at)
                | ((Movement.at == before_at) & (Movement.id < before_id))
            )
    rows = tuple(session.scalars(stmt).all())
    on_hand = _on_hand_after_by_movement(session, ctx, item_id=item_id, rows=rows)
    return tuple(_project(row, on_hand_after=on_hand[row.id]) for row in rows)


def adjust_to_observed(
    session: Session,
    ctx: WorkspaceContext,
    *,
    item_id: str,
    observed_qty: Decimal,
    reason: MovementReason = "audit_correction",
    source_stocktake_id: str | None = None,
    note: str | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> InventoryMovementView:
    """Set book stock to an observed count by writing one delta movement."""
    clean_observed = _clean_quantity(observed_qty, field_name="observed_qty")
    _validate_reason(reason)
    row = _load_active_item(session, ctx, item_id)
    delta = clean_observed - _clean_quantity(row.on_hand, field_name="on_hand")
    if delta == Decimal("0"):
        raise InventoryMovementValidationError("observed_qty", "nothing_to_adjust")
    _validate_reason_sign(reason, delta)
    return _write_movement(
        session,
        ctx,
        item_id=item_id,
        delta=delta,
        reason=reason,
        source_task_id=None,
        source_stocktake_id=source_stocktake_id,
        note=_clean_optional_note(note),
        clock=clock,
        event_bus=event_bus,
        locked_item=row,
    )


def reconcile(
    session: Session,
    ctx: WorkspaceContext,
    *,
    item_id: str,
    observed_qty: Decimal,
    reason: MovementReason = "audit_correction",
    source_stocktake_id: str,
    note: str | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> InventoryMovementView:
    """Reconcile one item as part of a property-wide stocktake."""
    return adjust_to_observed(
        session,
        ctx,
        item_id=item_id,
        observed_qty=observed_qty,
        reason=reason,
        source_stocktake_id=source_stocktake_id,
        note=note,
        clock=clock,
        event_bus=event_bus,
    )


def transfer(
    session: Session,
    ctx: WorkspaceContext,
    *,
    source_item_id: str,
    destination_item_id: str,
    qty: Decimal,
    note: str | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> tuple[InventoryMovementView, InventoryMovementView]:
    """Move quantity between two property-scoped item rows atomically."""
    clean_qty = _clean_magnitude(qty, field_name="qty")
    correlation_id = new_ulid(clock=clock)
    transfer_note = _transfer_note(correlation_id, note)
    with session.begin_nested():
        source_item, destination_item = _load_transfer_items(
            session,
            ctx,
            source_item_id=source_item_id,
            destination_item_id=destination_item_id,
        )
        out = _write_movement(
            session,
            ctx,
            item_id=source_item_id,
            delta=-clean_qty,
            reason="transfer_out",
            source_task_id=None,
            source_stocktake_id=None,
            note=transfer_note,
            clock=clock,
            event_bus=event_bus,
            locked_item=source_item,
            queue_event=False,
        )
        incoming = _write_movement(
            session,
            ctx,
            item_id=destination_item_id,
            delta=clean_qty,
            reason="transfer_in",
            source_task_id=None,
            source_stocktake_id=None,
            note=transfer_note,
            clock=clock,
            event_bus=event_bus,
            locked_item=destination_item,
            queue_event=False,
        )
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    _queue_item_changed(session, _pending_event(ctx, out, resolved_bus))
    _queue_item_changed(session, _pending_event(ctx, incoming, resolved_bus))
    return out, incoming


def _write_movement(
    session: Session,
    ctx: WorkspaceContext,
    *,
    item_id: str,
    delta: Decimal,
    reason: MovementReason,
    source_task_id: str | None,
    source_stocktake_id: str | None,
    note: str | None,
    clock: Clock | None,
    event_bus: EventBus | None,
    locked_item: Item | None = None,
    queue_event: bool = True,
) -> InventoryMovementView:
    _validate_reason(reason)
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    clean_delta = _clean_quantity(delta, field_name="delta")
    item = (
        locked_item
        if locked_item is not None
        else _load_active_item(session, ctx, item_id)
    )
    _validate_source_task(session, ctx, source_task_id, item=item)
    _validate_source_stocktake(session, ctx, source_stocktake_id, item)
    now = resolved_clock.now()
    movement = Movement(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        item_id=item.id,
        delta=clean_delta,
        reason=reason,
        source_task_id=source_task_id,
        source_stocktake_id=source_stocktake_id,
        actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id if ctx.actor_kind != "system" else None,
        at=now,
        note=note,
    )
    session.add(movement)
    session.execute(
        update(Item)
        .where(Item.workspace_id == ctx.workspace_id, Item.id == item.id)
        .values(on_hand=Item.on_hand + clean_delta, updated_at=now)
    )
    session.flush()
    session.refresh(item, attribute_names=["on_hand", "updated_at"])

    write_audit(
        session,
        ctx,
        entity_kind="inventory_movement",
        entity_id=movement.id,
        action="inventory_movement.created",
        diff={
            "after": {
                "item_id": item.id,
                "delta": str(clean_delta),
                "reason": reason,
                "source_task_id": source_task_id,
                "source_stocktake_id": source_stocktake_id,
                "on_hand_after": str(item.on_hand),
            }
        },
        clock=resolved_clock,
    )
    view = _project(movement, on_hand_after=item.on_hand)
    if queue_event:
        _queue_item_changed(session, _pending_event(ctx, view, resolved_bus))
    return view


def _load_active_item(session: Session, ctx: WorkspaceContext, item_id: str) -> Item:
    row = session.scalar(
        select(Item)
        .where(
            Item.workspace_id == ctx.workspace_id,
            Item.id == item_id,
            Item.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        raise InventoryItemNotFound("active inventory item not found")
    return row


def _ensure_active_item(session: Session, ctx: WorkspaceContext, item_id: str) -> None:
    exists = session.scalar(
        select(Item.id)
        .where(
            Item.workspace_id == ctx.workspace_id,
            Item.id == item_id,
            Item.deleted_at.is_(None),
        )
        .limit(1)
    )
    if exists is None:
        raise InventoryItemNotFound("active inventory item not found")


def _on_hand_after_by_movement(
    session: Session,
    ctx: WorkspaceContext,
    *,
    item_id: str,
    rows: tuple[Movement, ...],
) -> dict[str, Decimal]:
    if not rows:
        return {}
    newest = rows[0]
    running = sum(
        session.scalars(
            select(Movement.delta).where(
                Movement.workspace_id == ctx.workspace_id,
                Movement.item_id == item_id,
                (Movement.at > newest.at)
                | ((Movement.at == newest.at) & (Movement.id > newest.id)),
            )
        ),
        Decimal("0"),
    )
    current_on_hand = session.scalar(
        select(Item.on_hand).where(
            Item.workspace_id == ctx.workspace_id,
            Item.id == item_id,
        )
    )
    if current_on_hand is None:
        raise InventoryItemNotFound("active inventory item not found")
    by_id: dict[str, Decimal] = {}
    cursor_on_hand = current_on_hand - running
    for row in rows:
        by_id[row.id] = cursor_on_hand
        cursor_on_hand -= row.delta
    return by_id


def _load_transfer_items(
    session: Session,
    ctx: WorkspaceContext,
    *,
    source_item_id: str,
    destination_item_id: str,
) -> tuple[Item, Item]:
    if source_item_id == destination_item_id:
        raise InventoryMovementValidationError("destination_item_id", "distinct_item")
    rows = session.scalars(
        select(Item)
        .where(
            Item.workspace_id == ctx.workspace_id,
            Item.id.in_((source_item_id, destination_item_id)),
            Item.deleted_at.is_(None),
        )
        .order_by(Item.id)
        .with_for_update()
    ).all()
    by_id = {row.id: row for row in rows}
    source = by_id.get(source_item_id)
    destination = by_id.get(destination_item_id)
    if source is None or destination is None:
        raise InventoryItemNotFound("active inventory item not found")
    if source.property_id is None or destination.property_id is None:
        raise InventoryMovementValidationError("item_id", "property_required")
    if source.property_id == destination.property_id:
        raise InventoryMovementValidationError(
            "destination_item_id", "property_distinct"
        )
    return source, destination


def _validate_source_task(
    session: Session,
    ctx: WorkspaceContext,
    source_task_id: str | None,
    *,
    item: Item,
) -> None:
    if source_task_id is None:
        return
    task = session.scalar(
        select(Occurrence)
        .where(
            Occurrence.workspace_id == ctx.workspace_id,
            Occurrence.id == source_task_id,
        )
        .limit(1)
    )
    if task is None or task.property_id != item.property_id:
        raise InventoryMovementValidationError("source_task_id", "invalid")


def _validate_source_stocktake(
    session: Session,
    ctx: WorkspaceContext,
    source_stocktake_id: str | None,
    item: Item,
) -> None:
    if source_stocktake_id is None:
        return
    stocktake = session.scalar(
        select(Stocktake)
        .where(
            Stocktake.workspace_id == ctx.workspace_id,
            Stocktake.id == source_stocktake_id,
        )
        .limit(1)
    )
    if stocktake is None or stocktake.property_id != item.property_id:
        raise InventoryMovementValidationError("source_stocktake_id", "invalid")


def _queue_item_changed(session: Session, pending: _PendingInventoryEvent) -> None:
    if session not in _HOOKED_SESSIONS:
        event.listen(session, "after_commit", _publish_pending_events)
        event.listen(session, "after_rollback", _clear_pending_events)
        _HOOKED_SESSIONS.add(session)
    _PENDING_EVENTS.setdefault(session, []).append(pending)


def _pending_event(
    ctx: WorkspaceContext, movement: InventoryMovementView, bus: EventBus
) -> _PendingInventoryEvent:
    return _PendingInventoryEvent(
        bus=bus,
        event=InventoryItemChanged(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=movement.at,
            item_id=movement.item_id,
            movement_id=movement.id,
            reason=movement.reason,
        ),
    )


def _publish_pending_events(session: Session) -> None:
    if session.in_nested_transaction():
        return
    pending = _PENDING_EVENTS.pop(session, [])
    for item in pending:
        item.bus.publish(item.event)


def _clear_pending_events(session: Session) -> None:
    if session.in_nested_transaction():
        return
    _PENDING_EVENTS.pop(session, None)


def _clean_magnitude(value: Decimal, *, field_name: str) -> Decimal:
    clean = _clean_quantity(value, field_name=field_name)
    if clean <= Decimal("0"):
        raise InventoryMovementValidationError(field_name, "quantity_positive")
    return clean


def _clean_quantity(value: Decimal, *, field_name: str) -> Decimal:
    if not value.is_finite():
        raise InventoryMovementValidationError(field_name, "quantity_invalid")
    try:
        quantized = value.quantize(_QTY_QUANTUM)
    except InvalidOperation as exc:
        raise InventoryMovementValidationError(
            field_name, "quantity_precision"
        ) from exc
    if value != quantized:
        raise InventoryMovementValidationError(field_name, "quantity_precision")
    return quantized


def _validate_reason(reason: str) -> None:
    if reason not in _REASONS:
        raise InventoryMovementValidationError("reason", "invalid")


def _validate_reason_sign(reason: MovementReason, delta: Decimal) -> None:
    _validate_reason(reason)
    if delta == Decimal("0"):
        raise InventoryMovementValidationError("delta", "quantity_nonzero")
    if reason in _POSITIVE_REASONS and delta <= Decimal("0"):
        raise InventoryMovementValidationError("delta", "reason_requires_positive")
    if reason in _NEGATIVE_REASONS and delta >= Decimal("0"):
        raise InventoryMovementValidationError("delta", "reason_requires_negative")


def _clean_optional_note(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _transfer_note(correlation_id: str, note: str | None) -> str:
    cleaned = _clean_optional_note(note)
    prefix = f"transfer_correlation_id={correlation_id}"
    if cleaned is None:
        return prefix
    return f"{prefix}; {cleaned}"


def _project(row: Movement, *, on_hand_after: Decimal) -> InventoryMovementView:
    return InventoryMovementView(
        id=row.id,
        workspace_id=row.workspace_id,
        item_id=row.item_id,
        delta=row.delta,
        reason=row.reason,
        source_task_id=row.source_task_id,
        source_stocktake_id=row.source_stocktake_id,
        actor_kind=row.actor_kind,
        actor_id=row.actor_id,
        at=row.at,
        note=row.note,
        on_hand_after=on_hand_after,
    )
