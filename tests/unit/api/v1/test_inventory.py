"""Unit tests for the inventory v1 router metadata."""

from __future__ import annotations

from fastapi.routing import APIRoute

from app.api.v1.inventory import build_inventory_router


def test_inventory_routes_publish_operation_ids_and_cli_metadata() -> None:
    routes = [
        route
        for route in build_inventory_router().routes
        if isinstance(route, APIRoute)
    ]

    assert routes
    operation_ids = [route.operation_id for route in routes]
    assert all(operation_ids)
    assert len(set(operation_ids)) == len(operation_ids)

    for route in routes:
        assert route.operation_id is not None
        assert route.operation_id.startswith("inventory.")
        extra = route.openapi_extra
        assert isinstance(extra, dict)
        assert extra["x-cli"]["group"] == "inventory"
        assert extra["x-cli"]["verb"]


def test_inventory_post_routes_declare_idempotency_key_header() -> None:
    routes = [
        route
        for route in build_inventory_router().routes
        if isinstance(route, APIRoute) and "POST" in route.methods
    ]

    assert routes
    for route in routes:
        header_params = {param.alias for param in route.dependant.header_params}
        assert "Idempotency-Key" in header_params
