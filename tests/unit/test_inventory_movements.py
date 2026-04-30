"""Unit tests for inventory movement service."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.inventory.models import Item, Movement
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import Occurrence
from app.events.bus import EventBus
from app.events.types import InventoryItemChanged
from app.services.inventory import movement_service
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

_PINNED = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine_inventory_movements() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine_inventory_movements: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine_inventory_movements, expire_on_commit=False)
    with factory() as s:
        yield s


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


def _seed_scope(session: Session, clock: FrozenClock) -> tuple[WorkspaceContext, str]:
    owner = bootstrap_user(
        session,
        email=f"owner-{new_ulid().lower()}@example.com",
        display_name="Owner",
        clock=clock,
    )
    workspace = bootstrap_workspace(
        session,
        slug=f"inv-{new_ulid()[-8:].lower()}",
        name="Inventory",
        owner_user_id=owner.id,
        clock=clock,
    )
    with tenant_agnostic():
        prop = Property(
            id=new_ulid(clock=clock),
            address="1 Stock Way",
            timezone="UTC",
            tags_json=[],
            created_at=clock.now(),
        )
        session.add(prop)
        session.flush()
        session.add(
            PropertyWorkspace(
                property_id=prop.id,
                workspace_id=workspace.id,
                label="Main",
                membership_role="owner_workspace",
                status="active",
                created_at=clock.now(),
            )
        )
        session.flush()

    ctx = build_workspace_context(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=owner.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    return ctx, prop.id


def _seed_property(
    session: Session, ctx: WorkspaceContext, clock: FrozenClock, *, label: str
) -> str:
    with tenant_agnostic():
        prop = Property(
            id=new_ulid(clock=clock),
            address=f"{label} Stock Way",
            timezone="UTC",
            tags_json=[],
            created_at=clock.now(),
        )
        session.add(prop)
        session.flush()
        session.add(
            PropertyWorkspace(
                property_id=prop.id,
                workspace_id=ctx.workspace_id,
                label=label,
                membership_role="owner_workspace",
                status="active",
                created_at=clock.now(),
            )
        )
        session.flush()
    return prop.id


def _seed_item(
    session: Session,
    ctx: WorkspaceContext,
    property_id: str,
    clock: FrozenClock,
    *,
    name: str = "Soap",
    on_hand: Decimal = Decimal("0"),
) -> Item:
    row = Item(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        property_id=property_id,
        sku=f"SKU-{new_ulid()[-6:]}",
        name=name,
        unit="each",
        on_hand=on_hand,
        reorder_point=None,
        reorder_target=None,
        vendor=None,
        vendor_url=None,
        unit_cost_cents=None,
        barcode=None,
        barcode_ean13=None,
        tags_json=[],
        notes_md=None,
        created_at=clock.now(),
        updated_at=clock.now(),
        deleted_at=None,
    )
    session.add(row)
    session.flush()
    return row


def _seed_task(
    session: Session,
    ctx: WorkspaceContext,
    property_id: str,
    clock: FrozenClock,
) -> str:
    row = Occurrence(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        schedule_id=None,
        template_id=None,
        property_id=property_id,
        assignee_user_id=None,
        starts_at=clock.now(),
        ends_at=clock.now() + timedelta(minutes=30),
        scheduled_for_local="2026-04-28T12:00:00",
        originally_scheduled_for="2026-04-28T12:00:00",
        state="pending",
        overdue_since=None,
        completed_at=None,
        completed_by_user_id=None,
        reviewer_user_id=None,
        reviewed_at=None,
        cancellation_reason=None,
        title="Inventory task",
        description_md="",
        priority="normal",
        photo_evidence="disabled",
        duration_minutes=None,
        area_id=None,
        unit_id=None,
        expected_role_id=None,
        linked_instruction_ids=[],
        inventory_consumption_json={},
        is_personal=False,
        created_by_user_id=ctx.actor_id,
        created_at=clock.now(),
    )
    session.add(row)
    session.flush()
    return row.id


def _movement_rows(session: Session) -> list[Movement]:
    return list(session.scalars(select(Movement).order_by(Movement.at, Movement.id)))


def test_restock_writes_one_movement_updates_cache_audits_and_publishes(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    item = _seed_item(session, ctx, property_id, clock, on_hand=Decimal("1.5000"))
    task_id = _seed_task(session, ctx, property_id, clock)
    bus = EventBus()
    captured: list[InventoryItemChanged] = []
    bus.subscribe(InventoryItemChanged)(captured.append)

    view = movement_service.restock(
        session,
        ctx,
        item_id=item.id,
        qty=Decimal("2.2500"),
        source_task_id=task_id,
        clock=clock,
        event_bus=bus,
    )
    assert captured == []

    session.commit()

    movements = _movement_rows(session)
    assert len(movements) == 1
    assert movements[0].id == view.id
    assert movements[0].delta == Decimal("2.2500")
    assert movements[0].reason == "restock"
    assert movements[0].source_task_id == task_id
    assert item.on_hand == Decimal("3.7500")
    audits = session.scalars(
        select(AuditLog).where(AuditLog.entity_kind == "inventory_movement")
    ).all()
    assert len(audits) == 1
    assert audits[0].action == "inventory_movement.created"
    assert [event.movement_id for event in captured] == [view.id]


def test_rollback_discards_movement_audit_and_pending_event(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    item = _seed_item(session, ctx, property_id, clock, on_hand=Decimal("1.5000"))
    session.commit()
    bus = EventBus()
    captured: list[InventoryItemChanged] = []
    bus.subscribe(InventoryItemChanged)(captured.append)

    movement_service.restock(
        session,
        ctx,
        item_id=item.id,
        qty=Decimal("2.0000"),
        clock=clock,
        event_bus=bus,
    )
    assert captured == []

    session.rollback()

    session.refresh(item)
    assert item.on_hand == Decimal("1.5000")
    assert _movement_rows(session) == []
    assert (
        session.scalars(
            select(AuditLog).where(AuditLog.entity_kind == "inventory_movement")
        ).all()
        == []
    )
    assert captured == []


def test_consume_permits_negative_on_hand_and_carries_source_task(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    item = _seed_item(session, ctx, property_id, clock, on_hand=Decimal("1.0000"))
    task_id = _seed_task(session, ctx, property_id, clock)

    view = movement_service.consume(
        session,
        ctx,
        item_id=item.id,
        qty=Decimal("1.2500"),
        source_task_id=task_id,
        clock=clock,
    )

    assert view.delta == Decimal("-1.2500")
    assert view.source_task_id == task_id
    assert view.on_hand_after == Decimal("-0.2500")
    assert item.on_hand == Decimal("-0.2500")
    assert _movement_rows(session)[0].reason == "consume"


def test_produce_writes_positive_task_source_movement(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    item = _seed_item(session, ctx, property_id, clock, on_hand=Decimal("0"))
    task_id = _seed_task(session, ctx, property_id, clock)

    view = movement_service.produce(
        session,
        ctx,
        item_id=item.id,
        qty=Decimal("3.0000"),
        source_task_id=task_id,
        clock=clock,
    )

    assert view.delta == Decimal("3.0000")
    assert view.reason == "produce"
    assert view.source_task_id == task_id
    assert item.on_hand == Decimal("3.0000")


def test_adjust_to_observed_computes_delta_and_uses_supplied_reason(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    item = _seed_item(session, ctx, property_id, clock, on_hand=Decimal("5.0000"))

    view = movement_service.adjust_to_observed(
        session,
        ctx,
        item_id=item.id,
        observed_qty=Decimal("2.5000"),
        reason="loss",
        note="counted shelf",
        clock=clock,
    )

    assert view.delta == Decimal("-2.5000")
    assert view.reason == "loss"
    assert view.note == "counted shelf"
    assert item.on_hand == Decimal("2.5000")


def test_adjust_to_observed_rejects_zero_delta(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    item = _seed_item(session, ctx, property_id, clock, on_hand=Decimal("5.0000"))

    with pytest.raises(movement_service.InventoryMovementValidationError) as raised:
        movement_service.adjust_to_observed(
            session,
            ctx,
            item_id=item.id,
            observed_qty=Decimal("5.0000"),
            clock=clock,
        )

    assert raised.value.error == "nothing_to_adjust"
    assert _movement_rows(session) == []


def test_adjust_to_observed_rejects_reason_with_wrong_delta_sign(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    item = _seed_item(session, ctx, property_id, clock, on_hand=Decimal("5.0000"))

    with pytest.raises(movement_service.InventoryMovementValidationError) as raised:
        movement_service.adjust_to_observed(
            session,
            ctx,
            item_id=item.id,
            observed_qty=Decimal("4.0000"),
            reason="found",
            clock=clock,
        )

    assert raised.value.field == "delta"
    assert raised.value.error == "reason_requires_positive"
    assert _movement_rows(session) == []


def test_source_task_must_belong_to_item_property(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    other_property_id = _seed_property(session, ctx, clock, label="Other")
    item = _seed_item(session, ctx, property_id, clock)
    task_id = _seed_task(session, ctx, other_property_id, clock)

    with pytest.raises(movement_service.InventoryMovementValidationError) as raised:
        movement_service.record(
            session,
            ctx,
            item_id=item.id,
            delta=Decimal("1.0000"),
            reason="restock",
            source_task_id=task_id,
            clock=clock,
        )

    assert raised.value.field == "source_task_id"
    assert raised.value.error == "invalid"
    assert _movement_rows(session) == []


def test_list_movements_returns_newest_first_with_historical_balances(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    item = _seed_item(session, ctx, property_id, clock, on_hand=Decimal("0"))
    first = movement_service.record(
        session,
        ctx,
        item_id=item.id,
        delta=Decimal("3.0000"),
        reason="restock",
        clock=clock,
    )
    clock.advance(timedelta(seconds=1))
    second = movement_service.record(
        session,
        ctx,
        item_id=item.id,
        delta=Decimal("-1.0000"),
        reason="consume",
        clock=clock,
    )

    page = movement_service.list_movements(
        session,
        ctx,
        item_id=item.id,
        limit=2,
    )

    assert [movement.id for movement in page] == [second.id, first.id]
    assert [movement.on_hand_after for movement in page] == [
        Decimal("2.0000"),
        Decimal("3.0000"),
    ]


def test_transfer_writes_two_correlated_movements_and_two_events(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    destination_property_id = _seed_property(
        session, ctx, clock, label="Destination property"
    )
    source = _seed_item(
        session, ctx, property_id, clock, name="Source", on_hand=Decimal("10.0000")
    )
    destination = _seed_item(
        session,
        ctx,
        destination_property_id,
        clock,
        name="Destination",
        on_hand=Decimal("1.0000"),
    )
    bus = EventBus()
    captured: list[InventoryItemChanged] = []
    bus.subscribe(InventoryItemChanged)(captured.append)

    out, incoming = movement_service.transfer(
        session,
        ctx,
        source_item_id=source.id,
        destination_item_id=destination.id,
        qty=Decimal("2.0000"),
        note="move to villa",
        clock=clock,
        event_bus=bus,
    )
    session.commit()

    rows = {row.reason: row for row in _movement_rows(session)}
    assert set(rows) == {"transfer_out", "transfer_in"}
    assert rows["transfer_out"].note == rows["transfer_in"].note
    assert rows["transfer_out"].note is not None
    assert rows["transfer_out"].note.startswith("transfer_correlation_id=")
    assert "move to villa" in rows["transfer_out"].note
    assert source.on_hand == Decimal("8.0000")
    assert destination.on_hand == Decimal("3.0000")
    assert [event.movement_id for event in captured] == [out.id, incoming.id]


def test_transfer_requires_distinct_properties(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    source = _seed_item(
        session, ctx, property_id, clock, name="Source", on_hand=Decimal("10.0000")
    )
    destination = _seed_item(
        session, ctx, property_id, clock, name="Destination", on_hand=Decimal("1.0000")
    )

    with pytest.raises(movement_service.InventoryMovementValidationError) as raised:
        movement_service.transfer(
            session,
            ctx,
            source_item_id=source.id,
            destination_item_id=destination.id,
            qty=Decimal("2.0000"),
            clock=clock,
        )

    assert raised.value.error == "property_distinct"
    assert _movement_rows(session) == []


def test_nested_transfer_commit_does_not_publish_before_outer_commit(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    destination_property_id = _seed_property(
        session, ctx, clock, label="Destination property"
    )
    source = _seed_item(
        session, ctx, property_id, clock, name="Source", on_hand=Decimal("10.0000")
    )
    destination = _seed_item(
        session,
        ctx,
        destination_property_id,
        clock,
        name="Destination",
        on_hand=Decimal("1.0000"),
    )
    bus = EventBus()
    captured: list[InventoryItemChanged] = []
    bus.subscribe(InventoryItemChanged)(captured.append)

    restock = movement_service.restock(
        session,
        ctx,
        item_id=source.id,
        qty=Decimal("1.0000"),
        clock=clock,
        event_bus=bus,
    )
    out, incoming = movement_service.transfer(
        session,
        ctx,
        source_item_id=source.id,
        destination_item_id=destination.id,
        qty=Decimal("2.0000"),
        clock=clock,
        event_bus=bus,
    )

    assert captured == []

    session.commit()

    assert [event.movement_id for event in captured] == [
        restock.id,
        out.id,
        incoming.id,
    ]


def test_nested_transfer_rollback_preserves_prior_pending_event(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    source = _seed_item(
        session, ctx, property_id, clock, name="Source", on_hand=Decimal("10.0000")
    )
    bus = EventBus()
    captured: list[InventoryItemChanged] = []
    bus.subscribe(InventoryItemChanged)(captured.append)

    restock = movement_service.restock(
        session,
        ctx,
        item_id=source.id,
        qty=Decimal("1.0000"),
        clock=clock,
        event_bus=bus,
    )
    with pytest.raises(movement_service.InventoryItemNotFound):
        movement_service.transfer(
            session,
            ctx,
            source_item_id=source.id,
            destination_item_id=new_ulid(clock=clock),
            qty=Decimal("2.0000"),
            clock=clock,
            event_bus=bus,
        )

    assert captured == []

    session.commit()

    assert [event.movement_id for event in captured] == [restock.id]
