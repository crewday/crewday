"""Integration tests for inventory API routes beyond item CRUD."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.inventory.models import Item, Movement
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
def seeded(db_session: Session) -> tuple[WorkspaceContext, str]:
    tag = new_ulid()[-8:].lower()
    owner = bootstrap_user(
        db_session,
        email=f"owner-{tag}@example.com",
        display_name="Owner",
    )
    ws = bootstrap_workspace(
        db_session,
        slug=f"inv-{tag}",
        name="Inventory",
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
        db_session.flush()

    ctx = build_workspace_context(
        workspace_id=ws.id,
        workspace_slug=ws.slug,
        actor_id=owner.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    return ctx, prop.id


@pytest.fixture
def client(
    db_session: Session, seeded: tuple[WorkspaceContext, str]
) -> Iterator[TestClient]:
    ctx, _ = seeded
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


def _assert_problem(
    response: Response, *, status_code: int, error: str
) -> dict[str, object]:
    assert response.status_code == status_code, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["error"] == error
    return body


def _create_item(
    client: TestClient,
    property_id: str,
    *,
    name: str,
    sku: str,
    reorder_point: Decimal | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"name": name, "sku": sku, "unit": "each"}
    if reorder_point is not None:
        payload["reorder_point"] = str(reorder_point)
    response = client.post(
        f"/api/v1/inventory/properties/{property_id}/items",
        json=payload,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def test_movements_and_adjustments_append_ledger_and_update_on_hand(
    client: TestClient,
    seeded: tuple[WorkspaceContext, str],
    db_session: Session,
) -> None:
    _, property_id = seeded
    item = _create_item(client, property_id, name="Soap", sku="SOAP")

    restock = client.post(
        f"/api/v1/inventory/{item['id']}/movements",
        json={"delta": 3.5, "reason": "restock", "note": "delivery"},
    )
    assert restock.status_code == 201, restock.text
    assert restock.json()["delta"] == 3.5
    assert restock.json()["on_hand_after"] == 3.5
    assert restock.json()["reason"] == "restock"

    adjust = client.post(
        f"/api/v1/inventory/{item['id']}/adjust",
        json={
            "observed_on_hand": 2.25,
            "reason": "audit_correction",
            "note": "counted shelf",
        },
    )
    assert adjust.status_code == 201, adjust.text
    assert adjust.json()["delta"] == -1.25
    assert adjust.json()["on_hand_after"] == 2.25

    row = db_session.get(Item, item["id"])
    assert row is not None
    assert row.on_hand == Decimal("2.2500")
    movements = db_session.scalars(
        select(Movement).where(Movement.item_id == item["id"]).order_by(Movement.at)
    ).all()
    assert [movement.reason for movement in movements] == [
        "restock",
        "audit_correction",
    ]

    page = client.get(
        f"/api/v1/inventory/{item['id']}/movements",
        params={"limit": "1"},
    )
    assert page.status_code == 200, page.text
    page_body = page.json()
    assert page_body["has_more"] is True
    assert page_body["next_cursor"] is not None
    assert [
        (row["reason"], row["delta"], row["on_hand_after"]) for row in page_body["data"]
    ] == [("audit_correction", -1.25, 2.25)]

    next_page = client.get(
        f"/api/v1/inventory/{item['id']}/movements",
        params={"limit": "1", "before": page_body["next_cursor"]},
    )
    assert next_page.status_code == 200, next_page.text
    assert next_page.json()["has_more"] is False
    assert [
        (row["reason"], row["delta"], row["on_hand_after"])
        for row in next_page.json()["data"]
    ] == [("restock", 3.5, 3.5)]


def test_below_reorder_and_low_stock_report_exclude_produce_only_items(
    client: TestClient,
    seeded: tuple[WorkspaceContext, str],
) -> None:
    _, property_id = seeded
    low = _create_item(
        client,
        property_id,
        name="Towels",
        sku="TOWELS",
        reorder_point=Decimal("2"),
    )
    ok = _create_item(
        client,
        property_id,
        name="Coffee",
        sku="COFFEE",
        reorder_point=Decimal("2"),
    )
    produced = _create_item(
        client,
        property_id,
        name="Dirty linen",
        sku="DIRTY-LINEN",
        reorder_point=Decimal("2"),
    )

    response = client.post(
        f"/api/v1/inventory/{ok['id']}/movements",
        json={"delta": 3, "reason": "restock"},
    )
    assert response.status_code == 201, response.text
    response = client.post(
        f"/api/v1/inventory/{produced['id']}/movements",
        json={"delta": 1, "reason": "produce"},
    )
    assert response.status_code == 201, response.text

    filtered = client.get(
        f"/api/v1/inventory/properties/{property_id}/items",
        params={"below_reorder": "true"},
    )
    assert filtered.status_code == 200, filtered.text
    assert [item["id"] for item in filtered.json()["data"]] == [low["id"]]

    report = client.get("/api/v1/inventory/reports/low_stock")
    assert report.status_code == 200, report.text
    assert [item["id"] for item in report.json()["data"]] == [low["id"]]


def test_movement_rejects_reason_with_wrong_delta_sign(
    client: TestClient,
    seeded: tuple[WorkspaceContext, str],
) -> None:
    _, property_id = seeded
    item = _create_item(client, property_id, name="Soap", sku="SOAP")

    response = client.post(
        f"/api/v1/inventory/{item['id']}/movements",
        json={"delta": -1, "reason": "restock"},
    )

    body = _assert_problem(response, status_code=422, error="reason_requires_positive")
    assert body["field"] == "delta"


def test_adjust_rejects_reason_that_disagrees_with_computed_delta(
    client: TestClient,
    seeded: tuple[WorkspaceContext, str],
) -> None:
    _, property_id = seeded
    item = _create_item(client, property_id, name="Soap", sku="SOAP")

    response = client.post(
        f"/api/v1/inventory/{item['id']}/adjust",
        json={"observed_on_hand": -1, "reason": "found"},
    )

    body = _assert_problem(response, status_code=422, error="reason_requires_positive")
    assert body["field"] == "delta"
