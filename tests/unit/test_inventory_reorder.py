"""Unit tests for the inventory reorder-point service."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.inventory.models import Item
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import Occurrence, TaskTemplate
from app.adapters.db.workspace.models import WorkRole
from app.events.bus import EventBus
from app.events.types import InventoryLowStock
from app.services.inventory.reorder_service import check_reorder_points
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

_PINNED = datetime(2026, 4, 30, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine_inventory_reorder() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine_inventory_reorder: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine_inventory_reorder, expire_on_commit=False)
    with factory() as s:
        yield s


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


def _seed_scope(session: Session, clock: FrozenClock) -> tuple[WorkspaceContext, str]:
    owner = bootstrap_user(
        session,
        email=f"reorder-{new_ulid().lower()}@example.com",
        display_name="Owner",
        clock=clock,
    )
    workspace = bootstrap_workspace(
        session,
        slug=f"reorder-{new_ulid()[-8:].lower()}",
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
        session.add(
            WorkRole(
                id=new_ulid(clock=clock),
                workspace_id=workspace.id,
                key="property_manager",
                name="Property manager",
                description_md="",
                default_settings_json={},
                icon_name="",
                created_at=clock.now(),
                deleted_at=None,
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


def _seed_item(
    session: Session,
    ctx: WorkspaceContext,
    property_id: str,
    clock: FrozenClock,
    *,
    name: str,
    on_hand: Decimal,
    reorder_point: Decimal | None,
) -> Item:
    row = Item(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        property_id=property_id,
        sku=f"SKU-{new_ulid()[-6:]}",
        name=name,
        unit="each",
        on_hand=on_hand,
        reorder_point=reorder_point,
        reorder_target=Decimal("10.0000"),
        vendor="Supplier",
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


def _tasks(session: Session) -> list[Occurrence]:
    return list(session.scalars(select(Occurrence).order_by(Occurrence.created_at)))


def test_low_stock_creates_one_task_event_and_audit(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    item = _seed_item(
        session,
        ctx,
        property_id,
        clock,
        name="Soap",
        on_hand=Decimal("2.0000"),
        reorder_point=Decimal("2.0000"),
    )
    bus = EventBus()
    captured: list[InventoryLowStock] = []
    bus.subscribe(InventoryLowStock)(captured.append)

    report = check_reorder_points(session, ctx, clock=clock, event_bus=bus)

    tasks = _tasks(session)
    assert report.checked_items == 1
    assert report.tasks_created == 1
    assert report.events_emitted == 1
    assert len(tasks) == 1
    assert tasks[0].title == "Restock Soap"
    assert tasks[0].property_id == property_id
    assert tasks[0].expected_role_id is not None
    assert captured == [
        InventoryLowStock(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=clock.now(),
            property_id=property_id,
            item_id=item.id,
            on_hand=Decimal("2.0000"),
            reorder_point=Decimal("2.0000"),
            restock_task_id=tasks[0].id,
        )
    ]
    audit = session.scalar(
        select(AuditLog).where(AuditLog.action == "inventory.auto_restock_created")
    )
    assert audit is not None
    assert audit.diff["item_id"] == item.id
    assert audit.diff["restock_task_id"] == tasks[0].id


def test_default_restock_item_template_is_used(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    template = TaskTemplate(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        title="Restock {item}",
        name="Restock {item}",
        role_id=None,
        description_md="Buy {item}",
        default_duration_min=45,
        duration_minutes=45,
        required_evidence="none",
        photo_required=False,
        default_assignee_role=None,
        property_scope="one",
        listed_property_ids=[property_id],
        area_scope="any",
        listed_area_ids=[],
        checklist_template_json=[],
        photo_evidence="optional",
        linked_instruction_ids=[],
        priority="high",
        required_approval=False,
        inventory_effects_json=[],
        llm_hints_md=None,
        deleted_at=None,
        created_at=clock.now(),
    )
    session.add(template)
    _seed_item(
        session,
        ctx,
        property_id,
        clock,
        name="Soap",
        on_hand=Decimal("1.0000"),
        reorder_point=Decimal("2.0000"),
    )

    check_reorder_points(session, ctx, clock=clock, event_bus=EventBus())

    task = _tasks(session)[0]
    assert task.template_id == template.id
    assert task.title == "Restock Soap"
    assert task.description_md == "Buy {item}"
    assert task.priority == "high"
    assert task.photo_evidence == "optional"
    assert task.duration_minutes == 45


def test_reorder_check_is_idempotent_while_restock_task_is_open(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    _seed_item(
        session,
        ctx,
        property_id,
        clock,
        name="Coffee",
        on_hand=Decimal("1.0000"),
        reorder_point=Decimal("3.0000"),
    )
    bus = EventBus()
    captured: list[InventoryLowStock] = []
    bus.subscribe(InventoryLowStock)(captured.append)

    first = check_reorder_points(session, ctx, clock=clock, event_bus=bus)
    second = check_reorder_points(session, ctx, clock=clock, event_bus=bus)

    assert first.tasks_created == 1
    assert second.tasks_created == 0
    assert second.skipped_existing_open_task == 1
    assert len(_tasks(session)) == 1
    assert len(captured) == 1


def test_items_above_threshold_are_ignored(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    _seed_item(
        session,
        ctx,
        property_id,
        clock,
        name="Paper",
        on_hand=Decimal("4.0000"),
        reorder_point=Decimal("3.0000"),
    )

    report = check_reorder_points(session, ctx, clock=clock, event_bus=EventBus())

    assert report.checked_items == 1
    assert report.skipped_above_threshold == 1
    assert report.tasks_created == 0
    assert _tasks(session) == []


def test_closed_restock_task_allows_new_low_stock_event(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    _seed_item(
        session,
        ctx,
        property_id,
        clock,
        name="Towels",
        on_hand=Decimal("0.0000"),
        reorder_point=Decimal("2.0000"),
    )
    bus = EventBus()
    captured: list[InventoryLowStock] = []
    bus.subscribe(InventoryLowStock)(captured.append)

    check_reorder_points(session, ctx, clock=clock, event_bus=bus)
    task = _tasks(session)[0]
    task.state = "done"
    task.completed_at = clock.now() + timedelta(minutes=5)
    session.flush()
    check_reorder_points(session, ctx, clock=clock, event_bus=bus)

    assert len(_tasks(session)) == 2
    assert len(captured) == 2
