"""Integration smoke tests for composed places route paths (cd-75wp)."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.session import UnitOfWorkImpl
from app.api.deps import current_workspace_context
from app.api.deps import db_session as db_session_dep
from app.api.v1.places import build_properties_router
from app.tenancy import WorkspaceContext

pytest_plugins = ["tests.unit.api.v1.places.conftest"]


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    app = FastAPI()
    app.include_router(build_properties_router(), prefix="/api/v1")

    def _ctx() -> WorkspaceContext:
        return ctx

    def _session() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as session:
            assert isinstance(session, Session)
            yield session

    app.dependency_overrides[current_workspace_context] = _ctx
    app.dependency_overrides[db_session_dep] = _session
    return TestClient(app, raise_server_exceptions=False)


def _property_body() -> dict[str, object]:
    return {
        "name": "Villa Routes",
        "kind": "residence",
        "timezone": "Europe/Paris",
        "address_json": {
            "line1": "1 Route Test",
            "line2": None,
            "city": "Antibes",
            "state_province": None,
            "postal_code": None,
            "country": "FR",
        },
    }


def test_places_routes_mount_at_spec_paths_and_openapi_shapes(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ = owner_ctx
    client = _client(ctx, factory)

    created = client.post("/api/v1/properties", json=_property_body())
    assert created.status_code == 201, created.text
    property_id = created.json()["id"]

    unit = client.post(
        f"/api/v1/properties/{property_id}/units",
        json={"name": "Suite"},
    )
    assert unit.status_code == 201, unit.text
    unit_id = unit.json()["id"]
    assert client.get(f"/api/v1/units/{unit_id}").status_code == 200

    area = client.post(
        f"/api/v1/properties/{property_id}/areas",
        json={"name": "Kitchen"},
    )
    assert area.status_code == 201, area.text
    area_id = area.json()["id"]
    assert client.get(f"/api/v1/areas/{area_id}").status_code == 200

    closure = client.post(
        "/api/v1/property_closures",
        json={
            "property_id": property_id,
            "starts_at": "2026-05-01T00:00:00+00:00",
            "ends_at": "2026-05-02T00:00:00+00:00",
            "reason": "seasonal",
        },
    )
    assert closure.status_code == 201, closure.text
    closure_id = closure.json()["id"]
    assert client.delete(f"/api/v1/property_closures/{closure_id}").status_code == 204
    schema = client.get("/openapi.json").json()

    assert "post" in schema["paths"]["/api/v1/properties"]
    assert "get" in schema["paths"]["/api/v1/properties/{property_id}/units"]
    assert "post" in schema["paths"]["/api/v1/property_closures"]
    assert "post" in schema["paths"]["/api/v1/properties/{property_id}/share"]
