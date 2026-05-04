"""Focused unit checks for the messaging router surface."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from app.api.errors import _handle_domain_error
from app.api.v1.messaging import build_messaging_router
from app.domain.errors import DomainError


def test_messaging_router_declares_notifications_and_push_management_routes() -> None:
    operations = {
        route.operation_id
        for route in build_messaging_router().routes
        if hasattr(route, "operation_id")
    }

    assert {
        "messaging.notifications.list",
        "messaging.notifications.get",
        "messaging.notifications.update",
        "messaging.notifications.mark_read",
        "messaging.push_tokens.list",
        "messaging.push_tokens.register_native_unavailable",
        "messaging.push_tokens.delete",
        "messaging.register_push_subscription",
        "messaging.unregister_push_subscription",
    }.issubset(operations)


def test_messaging_router_documents_problem_json_validation_errors() -> None:
    app = FastAPI()
    app.include_router(build_messaging_router())

    responses = app.openapi()["paths"]["/notifications"]["get"]["responses"]

    assert responses["422"]["content"] == {
        "application/problem+json": {
            "schema": {
                "additionalProperties": True,
                "properties": {
                    "type": {"type": "string"},
                    "title": {"type": "string"},
                    "status": {"type": "integer"},
                    "detail": {"type": "string"},
                    "instance": {"type": "string"},
                    "errors": {"items": {"type": "object"}, "type": "array"},
                },
                "required": ["type", "title", "status", "instance"],
                "type": "object",
            }
        }
    }


def test_native_push_registration_requires_workspace_context() -> None:
    app = FastAPI()

    async def _on_domain_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DomainError)
        return _handle_domain_error(request, exc)

    app.add_exception_handler(DomainError, _on_domain_error)
    app.include_router(build_messaging_router())
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/notifications/push/tokens",
        json={"platform": "ios", "token": "native-token"},
    )

    assert resp.status_code == 401
    assert resp.json()["type"].endswith("/unauthorized")
