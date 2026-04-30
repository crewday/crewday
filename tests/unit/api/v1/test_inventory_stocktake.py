"""Unit tests for inventory stocktake route metadata."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import make_engine
from app.api.deps import current_workspace_context
from app.api.deps import db_session as _db_session_dep
from app.api.v1.inventory import (
    build_inventory_router,
    build_inventory_stocktakes_router,
)
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

_PINNED = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine: Engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            yield session
    finally:
        engine.dispose()


def test_stocktake_routes_publish_operation_ids_and_cli_metadata() -> None:
    routes = [
        route
        for route in build_inventory_stocktakes_router().routes
        if isinstance(route, APIRoute)
    ]

    assert {route.path for route in routes} == {
        "/properties/{property_id}/stocktakes",
        "/stocktakes/{stocktake_id}",
        "/stocktakes/{stocktake_id}/lines/{item_id}",
        "/stocktakes/{stocktake_id}/commit",
    }
    for route in routes:
        assert route.operation_id is not None
        assert route.operation_id.startswith("inventory.stocktakes.")
        assert route.openapi_extra is not None
        assert route.openapi_extra["x-cli"]["group"] == "inventory"
        assert route.openapi_extra["x-cli"]["verb"]


def test_stocktake_commit_requires_idempotency_key_and_agent_confirm() -> None:
    routes = [
        route
        for route in build_inventory_stocktakes_router().routes
        if isinstance(route, APIRoute)
    ]
    [commit] = [
        route for route in routes if route.operation_id == "inventory.stocktakes.commit"
    ]

    header_params = {param.alias: param for param in commit.dependant.header_params}
    assert "Idempotency-Key" in header_params
    assert commit.openapi_extra is not None
    assert commit.openapi_extra["x-agent-confirm"]


def test_inventory_openapi_documents_decimal_types_and_error_statuses() -> None:
    app = FastAPI()
    app.include_router(build_inventory_router(), prefix="/api/v1/inventory")
    app.include_router(build_inventory_stocktakes_router(), prefix="/api/v1")

    schema = app.openapi()

    line_response = schema["components"]["schemas"]["InventoryStocktakeLineResponse"]
    assert line_response["properties"]["item_id"]["type"] == "string"
    assert line_response["properties"]["observed_on_hand"]["anyOf"] == [
        {"type": "integer"},
        {"type": "number"},
    ]

    rate_response = schema["components"]["schemas"]["InventoryRateReportRowResponse"]
    assert rate_response["properties"]["property_id"]["type"] == "string"
    assert rate_response["properties"]["daily_avg"]["anyOf"] == [
        {"type": "integer"},
        {"type": "number"},
    ]

    commit_responses = schema["paths"]["/api/v1/stocktakes/{stocktake_id}/commit"][
        "post"
    ]["responses"]
    assert {"403", "404", "409"}.issubset(commit_responses)


@pytest.fixture
def seeded(db_session: Session) -> tuple[WorkspaceContext, WorkspaceContext, str]:
    tag = new_ulid()[-8:].lower()
    owner = bootstrap_user(
        db_session,
        email=f"stocktake-owner-{tag}@example.com",
        display_name="Owner",
    )
    worker = bootstrap_user(
        db_session,
        email=f"stocktake-worker-{tag}@example.com",
        display_name="Worker",
    )
    ws = bootstrap_workspace(
        db_session,
        slug=f"stocktake-{tag}",
        name="Stocktake",
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
    db_session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=ws.id,
            user_id=worker.id,
            grant_role="worker",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=owner.id,
        )
    )
    db_session.flush()

    owner_ctx = build_workspace_context(
        workspace_id=ws.id,
        workspace_slug=ws.slug,
        actor_id=owner.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    worker_ctx = build_workspace_context(
        workspace_id=ws.id,
        workspace_slug=ws.slug,
        actor_id=worker.id,
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
    )
    return owner_ctx, worker_ctx, prop.id


@contextmanager
def _client(db_session: Session, ctx: WorkspaceContext) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(build_inventory_router(), prefix="/api/v1/inventory")
    app.include_router(build_inventory_stocktakes_router(), prefix="/api/v1")

    def _session() -> Iterator[Session]:
        yield db_session

    def _ctx() -> WorkspaceContext:
        return ctx

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = _ctx
    with TestClient(app) as client:
        yield client


def test_stocktake_session_flow_commits_nonzero_deltas(
    db_session: Session,
    seeded: tuple[WorkspaceContext, WorkspaceContext, str],
) -> None:
    owner_ctx, _, property_id = seeded
    with _client(db_session, owner_ctx) as client:
        item = client.post(
            f"/api/v1/inventory/properties/{property_id}/items",
            json={"name": "Soap", "sku": "SOAP", "unit": "each"},
        )
        assert item.status_code == 201, item.text
        item_id = item.json()["id"]

        opened = client.post(
            f"/api/v1/properties/{property_id}/stocktakes",
            json={"note_md": "quarterly"},
        )
        assert opened.status_code == 201, opened.text
        stocktake_id = opened.json()["id"]

        line = client.patch(
            f"/api/v1/stocktakes/{stocktake_id}/lines/{item_id}",
            json={"observed_on_hand": 2.25, "reason": "found", "note": "closet"},
        )
        assert line.status_code == 200, line.text
        assert line.json()["observed_on_hand"] == 2.25

        missing_key = client.post(f"/api/v1/stocktakes/{stocktake_id}/commit")
        assert missing_key.status_code == 422, missing_key.text

        committed = client.post(
            f"/api/v1/stocktakes/{stocktake_id}/commit",
            headers={"Idempotency-Key": f"stocktake:{stocktake_id}:commit"},
        )
        assert committed.status_code == 200, committed.text
        body = committed.json()
        assert body["stocktake"]["completed_at"] is not None
        assert body["stocktake"]["lines"] == []
        assert [(row["reason"], row["delta"]) for row in body["movements"]] == [
            ("found", 2.25)
        ]
        assert body["movements"][0]["source_stocktake_id"] == stocktake_id


def test_stocktake_routes_return_403_without_stocktake_scope(
    db_session: Session,
    seeded: tuple[WorkspaceContext, WorkspaceContext, str],
) -> None:
    _, worker_ctx, property_id = seeded
    with _client(db_session, worker_ctx) as client:
        response = client.post(
            f"/api/v1/properties/{property_id}/stocktakes",
            json={},
        )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == {
        "error": "permission_denied",
        "action_key": "inventory.stocktake",
    }
