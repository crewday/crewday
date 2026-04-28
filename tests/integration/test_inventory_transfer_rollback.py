"""Integration coverage for inventory transfer rollback."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.inventory.models import Item, Movement
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.workspace.models import Workspace
from app.events.bus import EventBus
from app.events.types import InventoryItemChanged
from app.services.inventory import movement_service
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _ctx_for(workspace: Workspace, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLI",
    )


def _seed_scope(session: Session) -> tuple[WorkspaceContext, str]:
    clock = FrozenClock(_PINNED)
    user = bootstrap_user(
        session,
        email=f"owner-{new_ulid().lower()}@example.com",
        display_name="Owner",
        clock=clock,
    )
    workspace = bootstrap_workspace(
        session,
        slug=f"transfer-{new_ulid()[-8:].lower()}",
        name="Inventory transfer",
        owner_user_id=user.id,
        clock=clock,
    )
    with tenant_agnostic():
        prop = Property(
            id=new_ulid(clock=clock),
            address="1 Transfer Way",
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
    return _ctx_for(workspace, user.id), prop.id


def _seed_item(
    session: Session,
    ctx: WorkspaceContext,
    property_id: str,
    *,
    name: str,
    on_hand: Decimal,
) -> Item:
    clock = FrozenClock(_PINNED)
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


def test_transfer_rolls_back_source_leg_when_destination_fails(
    db_session: Session,
) -> None:
    ctx, property_id = _seed_scope(db_session)
    source = _seed_item(
        db_session,
        ctx,
        property_id,
        name="Source",
        on_hand=Decimal("5.0000"),
    )
    missing_destination_id = new_ulid()
    bus = EventBus()
    captured: list[InventoryItemChanged] = []
    bus.subscribe(InventoryItemChanged)(captured.append)

    with pytest.raises(movement_service.InventoryItemNotFound):
        movement_service.transfer(
            db_session,
            ctx,
            source_item_id=source.id,
            destination_item_id=missing_destination_id,
            qty=Decimal("2.0000"),
            clock=FrozenClock(_PINNED),
            event_bus=bus,
        )
    db_session.commit()

    db_session.refresh(source)
    assert source.on_hand == Decimal("5.0000")
    assert db_session.scalars(select(Movement)).all() == []
    assert captured == []
