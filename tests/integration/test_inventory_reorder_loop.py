"""Integration test for movement-triggered inventory reorders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.inventory.models import Item, Movement
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import WorkRole
from app.events.bus import EventBus
from app.events.types import InventoryLowStock
from app.services.inventory import movement_service
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.tasks.inventory_reorder import register_inventory_reorder_subscriber
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

_PINNED = datetime(2026, 4, 30, 10, 0, 0, tzinfo=UTC)


class _SessionUow:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory
        self._session: Session | None = None

    def __enter__(self) -> Session:
        self._session = self._factory()
        return self._session

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        assert self._session is not None
        try:
            if exc_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()


def test_movement_event_creates_restock_task_then_completion_allows_next_loop() -> None:
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    clock = FrozenClock(_PINNED)
    bus = EventBus()
    captured: list[InventoryLowStock] = []
    bus.subscribe(InventoryLowStock)(captured.append)
    register_inventory_reorder_subscriber(
        event_bus=bus,
        session_factory=lambda: _SessionUow(factory),
        clock=clock,
    )

    try:
        with factory() as session:
            ctx, property_id, item_id, source_task_id = _seed_loop(session, clock=clock)
            session.commit()

        with factory() as session:
            movement_service.consume(
                session,
                ctx,
                item_id=item_id,
                qty=Decimal("2.0000"),
                source_task_id=source_task_id,
                clock=clock,
                event_bus=bus,
            )
            session.commit()

        with factory() as session:
            tasks = _tasks(session, ctx)
            assert len(tasks) == 2
            restock_task = next(task for task in tasks if task.id != source_task_id)
            assert restock_task.title == "Restock Soap"
            assert restock_task.property_id == property_id
            assert captured[0].restock_task_id == restock_task.id
            assert _auto_restock_audit_count(session, ctx) == 1

            restock_task.state = "done"
            restock_task.completed_at = clock.now() + timedelta(minutes=10)
            session.add(
                Movement(
                    id=new_ulid(clock=clock),
                    workspace_id=ctx.workspace_id,
                    item_id=item_id,
                    delta=Decimal("4.0000"),
                    reason="restock",
                    source_task_id=restock_task.id,
                    source_stocktake_id=None,
                    actor_kind="user",
                    actor_id=ctx.actor_id,
                    at=clock.now() + timedelta(minutes=10),
                    note=None,
                )
            )
            item = session.get(Item, item_id)
            assert item is not None
            item.on_hand = Decimal("5.0000")
            session.commit()

        clock.advance(timedelta(minutes=20))
        with factory() as session:
            movement_service.consume(
                session,
                ctx,
                item_id=item_id,
                qty=Decimal("4.0000"),
                source_task_id=source_task_id,
                clock=clock,
                event_bus=bus,
            )
            session.commit()

        with factory() as session:
            tasks = _tasks(session, ctx)
            assert len([task for task in tasks if task.id != source_task_id]) == 2
            assert len(captured) == 2
            assert _auto_restock_audit_count(session, ctx) == 2
    finally:
        engine.dispose()


def _seed_loop(
    session: Session, *, clock: FrozenClock
) -> tuple[WorkspaceContext, str, str, str]:
    owner = bootstrap_user(
        session,
        email=f"loop-{new_ulid().lower()}@example.com",
        display_name="Owner",
        clock=clock,
    )
    workspace = bootstrap_workspace(
        session,
        slug=f"loop-{new_ulid()[-8:].lower()}",
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
        item = Item(
            id=new_ulid(clock=clock),
            workspace_id=workspace.id,
            property_id=prop.id,
            sku="SOAP",
            name="Soap",
            unit="each",
            on_hand=Decimal("3.0000"),
            reorder_point=Decimal("2.0000"),
            reorder_target=Decimal("8.0000"),
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
        session.add(item)
        task = Occurrence(
            id=new_ulid(clock=clock),
            workspace_id=workspace.id,
            schedule_id=None,
            template_id=None,
            property_id=prop.id,
            assignee_user_id=None,
            starts_at=clock.now(),
            ends_at=clock.now() + timedelta(minutes=30),
            scheduled_for_local="2026-04-30T10:00:00",
            originally_scheduled_for="2026-04-30T10:00:00",
            state="pending",
            overdue_since=None,
            completed_at=None,
            completed_by_user_id=None,
            reviewer_user_id=None,
            reviewed_at=None,
            cancellation_reason=None,
            title="Use soap",
            description_md="",
            priority="normal",
            photo_evidence="disabled",
            duration_minutes=30,
            area_id=None,
            unit_id=None,
            expected_role_id=None,
            linked_instruction_ids=[],
            inventory_consumption_json={},
            is_personal=False,
            created_by_user_id=owner.id,
            created_at=clock.now(),
        )
        session.add(task)
        session.flush()

    ctx = build_workspace_context(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=owner.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    return ctx, prop.id, item.id, task.id


def _tasks(session: Session, ctx: WorkspaceContext) -> list[Occurrence]:
    return list(
        session.scalars(
            select(Occurrence)
            .where(Occurrence.workspace_id == ctx.workspace_id)
            .order_by(Occurrence.created_at, Occurrence.id)
        )
    )


def _auto_restock_audit_count(session: Session, ctx: WorkspaceContext) -> int:
    return len(
        session.scalars(
            select(AuditLog).where(
                AuditLog.workspace_id == ctx.workspace_id,
                AuditLog.action == "inventory.auto_restock_created",
            )
        ).all()
    )
