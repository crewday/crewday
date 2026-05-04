"""Integration tests for inventory report API routes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.adapters.db.inventory.models import Item, Movement, Stocktake
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.api.deps import current_workspace_context
from app.api.deps import db_session as _db_session_dep
from app.api.errors import add_exception_handlers
from app.api.v1.inventory import build_inventory_router
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def seeded(db_session: Session) -> tuple[WorkspaceContext, str, str, str]:
    tag = new_ulid()[-8:].lower()
    owner = bootstrap_user(
        db_session,
        email=f"reports-owner-{tag}@example.com",
        display_name="Owner",
    )
    ws = bootstrap_workspace(
        db_session,
        slug=f"reports-{tag}",
        name="Reports",
        owner_user_id=owner.id,
    )
    with tenant_agnostic():
        prop = Property(
            id=new_ulid(),
            address="1 Stock Way",
            timezone="UTC",
            tags_json=[],
            created_at=_PINNED,
        )
        db_session.add(prop)
        db_session.flush()
        db_session.add(
            PropertyWorkspace(
                property_id=prop.id,
                workspace_id=ws.id,
                label="Main",
                membership_role="owner_workspace",
                status="active",
                created_at=_PINNED,
            )
        )
    ctx = build_workspace_context(
        workspace_id=ws.id,
        workspace_slug=ws.slug,
        actor_id=owner.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    produced = _item(db_session, ctx, prop.id, sku="DIRTY", name="Dirty linen")
    towels = _item(db_session, ctx, prop.id, sku="TOWELS", name="Towels")
    db_session.flush()
    return ctx, prop.id, produced.id, towels.id


@pytest.fixture
def client(
    db_session: Session, seeded: tuple[WorkspaceContext, str, str, str]
) -> Iterator[TestClient]:
    ctx, _, _, _ = seeded
    app = FastAPI()
    app.include_router(build_inventory_router(), prefix="/api/v1/inventory")
    add_exception_handlers(app)

    def _session() -> Iterator[Session]:
        yield db_session

    def _ctx() -> WorkspaceContext:
        return ctx

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = _ctx
    with TestClient(app) as c:
        yield c


def test_production_and_shrinkage_reports_return_decimal_numbers(
    client: TestClient,
    db_session: Session,
    seeded: tuple[WorkspaceContext, str, str, str],
) -> None:
    ctx, _, produced_id, towels_id = seeded
    found = _item(db_session, ctx, seeded[1], sku="FOUND", name="Found goods")
    _movement(
        db_session, ctx, item_id=produced_id, delta=Decimal("9"), reason="produce"
    )
    _movement(db_session, ctx, item_id=towels_id, delta=Decimal("-2"), reason="theft")
    _movement(db_session, ctx, item_id=towels_id, delta=Decimal("-1.5"), reason="loss")
    _movement(
        db_session,
        ctx,
        item_id=towels_id,
        delta=Decimal("-0.25"),
        reason="audit_correction",
    )
    _movement(
        db_session,
        ctx,
        item_id=found.id,
        delta=Decimal("3"),
        reason="audit_correction",
    )
    db_session.flush()

    production = client.get("/api/v1/inventory/reports/production_rate")
    assert production.status_code == 200, production.text
    assert production.json()["data"] == [
        {
            "property_id": seeded[1],
            "item_id": produced_id,
            "item_name": "Dirty linen",
            "sku": "DIRTY",
            "unit": "each",
            "total_qty": 9,
            "daily_avg": 0.3,
            "window_days": 30,
        }
    ]

    shrinkage = client.get("/api/v1/inventory/reports/shrinkage")
    assert shrinkage.status_code == 200, shrinkage.text
    assert shrinkage.json()["data"] == [
        {
            "property_id": seeded[1],
            "item_id": towels_id,
            "item_name": "Towels",
            "sku": "TOWELS",
            "unit": "each",
            "theft_qty": 2,
            "loss_qty": 1.5,
            "audit_correction_qty": 0.25,
            "shrinkage_qty": 3.75,
            "window_days": 30,
        }
    ]


def test_stocktake_activity_report_summarises_sessions(
    client: TestClient,
    db_session: Session,
    seeded: tuple[WorkspaceContext, str, str, str],
) -> None:
    ctx, property_id, _, towels_id = seeded
    stocktake = Stocktake(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        property_id=property_id,
        started_at=_PINNED,
        completed_at=_PINNED + timedelta(minutes=5),
        actor_kind="user",
        actor_id=ctx.actor_id,
        note_md=None,
    )
    db_session.add(stocktake)
    db_session.flush()
    _movement(
        db_session,
        ctx,
        item_id=towels_id,
        delta=Decimal("-1.25"),
        reason="loss",
        source_stocktake_id=stocktake.id,
    )
    db_session.flush()

    response = client.get("/api/v1/inventory/reports/stocktakes")
    assert response.status_code == 200, response.text
    assert response.json()["data"] == [
        {
            "stocktake_id": stocktake.id,
            "property_id": property_id,
            # Aware UTC roundtrip — pydantic v2 serialises ``DateTime(timezone=
            # True)`` columns as RFC 3339 with a ``+00:00`` suffix after the
            # cd-xma93 ``UtcDateTime`` TypeDecorator landed (was naive on
            # SQLite before).
            "started_at": _PINNED.isoformat(),
            "completed_at": (_PINNED + timedelta(minutes=5)).isoformat(),
            "actor_kind": "user",
            "actor_id": ctx.actor_id,
            "movement_count": 1,
            "absolute_delta": 1.25,
            "net_delta": -1.25,
        }
    ]


def test_reports_return_404_for_unknown_property_filter(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/v1/inventory/reports/production_rate",
        params={"property_id": "missing-property"},
    )

    assert response.status_code == 404, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error"] == "property_not_found"


def _item(
    session: Session,
    ctx: WorkspaceContext,
    property_id: str,
    *,
    sku: str,
    name: str,
) -> Item:
    row = Item(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        property_id=property_id,
        sku=sku,
        name=name,
        unit="each",
        on_hand=Decimal("0"),
        reorder_point=None,
        reorder_target=None,
        vendor=None,
        vendor_url=None,
        unit_cost_cents=None,
        barcode=None,
        barcode_ean13=None,
        tags_json=[],
        notes_md=None,
        created_at=_PINNED,
        updated_at=_PINNED,
        deleted_at=None,
    )
    session.add(row)
    return row


def _movement(
    session: Session,
    ctx: WorkspaceContext,
    *,
    item_id: str,
    delta: Decimal,
    reason: str,
    source_stocktake_id: str | None = None,
) -> None:
    session.add(
        Movement(
            id=new_ulid(),
            workspace_id=ctx.workspace_id,
            item_id=item_id,
            delta=delta,
            reason=reason,
            source_task_id=None,
            source_stocktake_id=source_stocktake_id,
            actor_kind="user",
            actor_id=ctx.actor_id,
            at=_PINNED,
            note=None,
        )
    )
