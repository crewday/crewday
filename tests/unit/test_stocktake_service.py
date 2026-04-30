"""Unit tests for inventory stocktake service."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.inventory.models import Item, Stocktake, StocktakeLine
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import make_engine
from app.services.inventory import movement_service, stocktake_service
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
def engine_stocktake_service() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine_stocktake_service: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine_stocktake_service, expire_on_commit=False)
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
        slug=f"stocktake-{new_ulid()[-8:].lower()}",
        name="Stocktake",
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


def _seed_worker_ctx(
    session: Session, ctx: WorkspaceContext, clock: FrozenClock
) -> WorkspaceContext:
    worker = bootstrap_user(
        session,
        email=f"worker-{new_ulid().lower()}@example.com",
        display_name="Worker",
        clock=clock,
    )
    session.add(
        RoleGrant(
            id=new_ulid(clock=clock),
            workspace_id=ctx.workspace_id,
            user_id=worker.id,
            grant_role="worker",
            scope_property_id=None,
            created_at=clock.now(),
            created_by_user_id=ctx.actor_id,
        )
    )
    session.flush()
    return build_workspace_context(
        workspace_id=ctx.workspace_id,
        workspace_slug=ctx.workspace_slug,
        actor_id=worker.id,
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
    )


def _seed_item(
    session: Session,
    ctx: WorkspaceContext,
    property_id: str,
    clock: FrozenClock,
    *,
    on_hand: Decimal = Decimal("0"),
) -> Item:
    row = Item(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        property_id=property_id,
        sku=f"SKU-{new_ulid()[-6:]}",
        name="Soap",
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


def _seed_property(session: Session, ctx: WorkspaceContext, clock: FrozenClock) -> str:
    with tenant_agnostic():
        prop = Property(
            id=new_ulid(clock=clock),
            address=f"{new_ulid()[-4:]} Stock Way",
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
                label="Secondary",
                membership_role="owner_workspace",
                status="active",
                created_at=clock.now(),
            )
        )
        session.flush()
        return prop.id


def test_open_creates_session_row(session: Session, clock: FrozenClock) -> None:
    ctx, property_id = _seed_scope(session, clock)

    view = stocktake_service.open(
        session,
        ctx,
        property_id=property_id,
        note_md="Quarterly count",
        clock=clock,
    )

    row = session.get(Stocktake, view.id)
    assert row is not None
    assert row.workspace_id == ctx.workspace_id
    assert row.property_id == property_id
    assert row.completed_at is None
    assert row.note_md == "Quarterly count"
    assert row.actor_id == ctx.actor_id


def test_save_line_upserts_draft(session: Session, clock: FrozenClock) -> None:
    ctx, property_id = _seed_scope(session, clock)
    item = _seed_item(session, ctx, property_id, clock, on_hand=Decimal("5.0000"))
    stocktake = stocktake_service.open(session, ctx, property_id=property_id)

    first = stocktake_service.save_line(
        session,
        ctx,
        stocktake_id=stocktake.id,
        item_id=item.id,
        observed=Decimal("4.0000"),
        reason="loss",
        note="first count",
        clock=clock,
    )
    second = stocktake_service.save_line(
        session,
        ctx,
        stocktake_id=stocktake.id,
        item_id=item.id,
        observed=Decimal("6.0000"),
        reason="found",
        note="recounted",
        clock=clock,
    )

    assert first.stocktake_id == second.stocktake_id
    rows = session.scalars(select(StocktakeLine)).all()
    assert len(rows) == 1
    assert rows[0].observed_on_hand == Decimal("6.0000")
    assert rows[0].reason == "found"
    assert rows[0].note == "recounted"


def test_mutations_require_stocktake_permission(
    session: Session, clock: FrozenClock
) -> None:
    manager_ctx, property_id = _seed_scope(session, clock)
    worker_ctx = _seed_worker_ctx(session, manager_ctx, clock)
    item = _seed_item(session, manager_ctx, property_id, clock)
    stocktake = stocktake_service.open(session, manager_ctx, property_id=property_id)

    with pytest.raises(stocktake_service.StocktakePermissionDenied):
        stocktake_service.open(session, worker_ctx, property_id=property_id)

    with pytest.raises(stocktake_service.StocktakePermissionDenied):
        stocktake_service.save_line(
            session,
            worker_ctx,
            stocktake_id=stocktake.id,
            item_id=item.id,
            observed=Decimal("1.0000"),
        )

    with pytest.raises(stocktake_service.StocktakePermissionDenied):
        stocktake_service.commit(
            session,
            worker_ctx,
            stocktake_id=stocktake.id,
        )


def test_source_stocktake_must_match_item_property(
    session: Session, clock: FrozenClock
) -> None:
    ctx, first_property_id = _seed_scope(session, clock)
    second_property_id = _seed_property(session, ctx, clock)
    first_item = _seed_item(
        session, ctx, first_property_id, clock, on_hand=Decimal("5.0000")
    )
    second_stocktake = stocktake_service.open(
        session, ctx, property_id=second_property_id, clock=clock
    )

    with pytest.raises(movement_service.InventoryMovementValidationError) as exc:
        movement_service.adjust_to_observed(
            session,
            ctx,
            item_id=first_item.id,
            observed_qty=Decimal("6.0000"),
            source_stocktake_id=second_stocktake.id,
            clock=clock,
        )

    assert exc.value.field == "source_stocktake_id"
    assert exc.value.error == "invalid"
