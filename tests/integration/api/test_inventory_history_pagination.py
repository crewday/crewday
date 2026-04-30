"""Integration tests for inventory movement history pagination."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.adapters.db.inventory.models import Item, Movement
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.api.deps import current_workspace_context
from app.api.deps import db_session as _db_session_dep
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
def seeded(db_session: Session) -> tuple[WorkspaceContext, str, str]:
    tag = new_ulid()[-8:].lower()
    owner = bootstrap_user(
        db_session,
        email=f"history-owner-{tag}@example.com",
        display_name="Owner",
    )
    ws = bootstrap_workspace(
        db_session,
        slug=f"history-{tag}",
        name="History",
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
    item = Item(
        id=new_ulid(),
        workspace_id=ws.id,
        property_id=prop.id,
        sku="SOAP",
        name="Soap",
        unit="each",
        on_hand=Decimal("10"),
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
    db_session.add(item)
    db_session.flush()
    return ctx, prop.id, item.id


@pytest.fixture
def client(
    db_session: Session, seeded: tuple[WorkspaceContext, str, str]
) -> Iterator[TestClient]:
    ctx, _, _ = seeded
    app = FastAPI()
    app.include_router(build_inventory_router(), prefix="/api/v1/inventory")

    def _session() -> Iterator[Session]:
        yield db_session

    def _ctx() -> WorkspaceContext:
        return ctx

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = _ctx
    with TestClient(app) as c:
        yield c


def test_movement_history_uses_stable_composite_keyset_cursor(
    client: TestClient,
    db_session: Session,
    seeded: tuple[WorkspaceContext, str, str],
) -> None:
    ctx, _, item_id = seeded
    tied_at = _PINNED - timedelta(minutes=1)
    rows = [
        Movement(
            id=f"history-{suffix}",
            workspace_id=ctx.workspace_id,
            item_id=item_id,
            delta=delta,
            reason=reason,
            source_task_id=None,
            source_stocktake_id=None,
            actor_kind="user",
            actor_id=ctx.actor_id,
            at=at,
            note=None,
        )
        for suffix, at, delta, reason in (
            ("03", _PINNED, Decimal("5"), "restock"),
            ("02", tied_at, Decimal("-1"), "consume"),
            ("01", tied_at, Decimal("-2"), "loss"),
            ("00", tied_at - timedelta(minutes=1), Decimal("8"), "restock"),
        )
    ]
    db_session.add_all(rows)
    db_session.flush()

    first = client.get(
        f"/api/v1/inventory/{item_id}/movements",
        params={"limit": "2"},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert [row["id"] for row in first_body["data"]] == ["history-03", "history-02"]
    assert first_body["has_more"] is True
    assert first_body["next_cursor"] is not None

    db_session.add(
        Movement(
            id="history-04",
            workspace_id=ctx.workspace_id,
            item_id=item_id,
            delta=Decimal("1"),
            reason="restock",
            source_task_id=None,
            source_stocktake_id=None,
            actor_kind="user",
            actor_id=ctx.actor_id,
            at=_PINNED + timedelta(seconds=1),
            note=None,
        )
    )
    db_session.flush()

    second = client.get(
        f"/api/v1/inventory/{item_id}/movements",
        params={"limit": "2", "before": first_body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert [row["id"] for row in second_body["data"]] == [
        "history-01",
        "history-00",
    ]
    assert second_body["has_more"] is False


def test_movement_history_missing_item_wins_over_invalid_cursor(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/v1/inventory/missing-item/movements",
        params={"before": "not-a-cursor"},
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == {"error": "inventory_item_not_found"}
