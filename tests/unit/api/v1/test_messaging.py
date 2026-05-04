"""Focused unit checks for the messaging router surface."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from app.api.deps import current_workspace_context, db_session
from app.api.errors import _handle_domain_error, add_exception_handlers
from app.api.v1.messaging import build_messaging_router
from app.domain.errors import DomainError
from app.tenancy import WorkspaceContext


def _ctx() -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id="ws_test",
        workspace_slug="test",
        actor_id="user_test",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="corr_test",
    )


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
        "messaging.chat_channels.list",
        "messaging.chat_channels.create",
        "messaging.chat_channels.update",
        "messaging.chat_messages.list",
        "messaging.chat_messages.send",
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


def test_chat_openapi_documents_request_body_invariants() -> None:
    app = FastAPI()
    app.include_router(build_messaging_router())
    openapi = app.openapi()
    schemas = openapi["components"]["schemas"]

    channel_create = openapi["paths"]["/chat/channels"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert channel_create["discriminator"] == {
        "propertyName": "kind",
        "mapping": {
            "staff": "#/components/schemas/AppChatChannelCreateRequest",
            "manager": "#/components/schemas/AppChatChannelCreateRequest",
            "chat_gateway": "#/components/schemas/GatewayChatChannelCreateRequest",
        },
    }
    assert channel_create["oneOf"] == [
        {"$ref": "#/components/schemas/AppChatChannelCreateRequest"},
        {"$ref": "#/components/schemas/GatewayChatChannelCreateRequest"},
    ]
    assert schemas["AppChatChannelCreateRequest"]["properties"]["external_ref"] == {
        "type": "null",
        "title": "External Ref",
    }
    assert (
        schemas["GatewayChatChannelCreateRequest"]["properties"]["external_ref"][
            "pattern"
        ]
        == r"\S"
    )

    channel_patch = schemas["ChatChannelPatchRequest"]
    assert channel_patch["properties"]["archived"]["enum"] == [True, None]
    assert {"required": ["title"]} in channel_patch["anyOf"]
    assert {
        "required": ["archived"],
        "properties": {"archived": {"const": True}},
    } in channel_patch["anyOf"]

    message_send = schemas["ChatMessageSendRequest"]
    assert {
        "required": ["body_md"],
        "properties": {
            "body_md": {
                "type": "string",
                "minLength": 1,
                "maxLength": 20_000,
                "pattern": r"\S",
            }
        },
    } in message_send["anyOf"]
    assert {
        "required": ["attachments"],
        "properties": {
            "attachments": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
            }
        },
    } in message_send["anyOf"]


def test_notifications_list_rejects_duplicate_cursor_query_params() -> None:
    app = FastAPI()
    add_exception_handlers(app)
    app.dependency_overrides[current_workspace_context] = _ctx
    app.dependency_overrides[db_session] = object
    app.include_router(build_messaging_router())
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/notifications?cursor=first&cursor=second")

    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["type"].endswith("/validation")
    assert body["detail"] == "cursor may be provided at most once"


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
