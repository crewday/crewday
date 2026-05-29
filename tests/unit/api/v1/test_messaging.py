"""Focused unit checks for the messaging router surface."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import JSONResponse

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.llm.models import ApprovalRequest
from app.adapters.db.messaging.models import Notification
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import WorkEngagement
from app.api.deps import current_workspace_context, db_session
from app.api.errors import _handle_domain_error, add_exception_handlers
from app.api.v1.messaging import build_messaging_router
from app.domain.errors import DomainError
from app.domain.messaging.push_tokens import validate_endpoint
from app.tenancy import PrincipalKind, WorkspaceContext, tenant_agnostic
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

_PINNED = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)


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


def _load_all_models() -> None:
    import importlib
    import pkgutil

    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


def _seed_broadcast_workspace(
    factory: sessionmaker[Session],
) -> tuple[WorkspaceContext, tuple[str, str]]:
    with factory() as session:
        owner = bootstrap_user(
            session,
            email="router-broadcast-owner@example.com",
            display_name="Router Broadcast Owner",
            clock=FrozenClock(_PINNED),
        )
        workspace = bootstrap_workspace(
            session,
            slug="router-broadcasts",
            name="Router broadcasts",
            owner_user_id=owner.id,
            clock=FrozenClock(_PINNED),
        )
        worker_ids: list[str] = []
        with tenant_agnostic():
            for idx in range(2):
                worker = bootstrap_user(
                    session,
                    email=f"router-broadcast-worker-{idx}@example.com",
                    display_name=f"Router Broadcast Worker {idx}",
                    clock=FrozenClock(_PINNED),
                )
                worker_ids.append(worker.id)
                session.add(
                    RoleGrant(
                        id=new_ulid(),
                        workspace_id=workspace.id,
                        user_id=worker.id,
                        grant_role="worker",
                        scope_kind="workspace",
                        scope_property_id=None,
                        created_at=_PINNED,
                        created_by_user_id=owner.id,
                    )
                )
                session.add(
                    WorkEngagement(
                        id=new_ulid(),
                        workspace_id=workspace.id,
                        user_id=worker.id,
                        engagement_kind="payroll",
                        started_on=date(2026, 1, 1),
                        created_at=_PINNED,
                        updated_at=_PINNED,
                    )
                )
        session.commit()
        ctx = build_workspace_context(
            workspace_id=workspace.id,
            workspace_slug=workspace.slug,
            actor_id=owner.id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
        )
    return ctx, (worker_ids[0], worker_ids[1])


def _build_db_client(
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
) -> TestClient:
    app = FastAPI()
    add_exception_handlers(app)
    app.include_router(build_messaging_router(), prefix="/messaging")

    def _override_ctx() -> WorkspaceContext:
        return ctx

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as session:
            assert isinstance(session, Session)
            yield session

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db
    return TestClient(app, raise_server_exceptions=False)


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
        "messaging.get_vapid_public_key",
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
        "messaging.broadcast.recipients",
        "messaging.broadcast.send",
    }.issubset(operations)


def test_broadcast_single_recipient_creates_notification_row() -> None:
    _load_all_models()
    engine: Engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        ctx, worker_ids = _seed_broadcast_workspace(factory)
        client = _build_db_client(factory, ctx)

        preview = client.get("/messaging/broadcast/recipients")
        assert preview.status_code == 200
        preview_body = preview.json()
        assert preview_body["total"] >= 2
        assert preview_body["data"] == preview_body["people"]
        token = next(
            person["token"]
            for person in preview_body["people"]
            if person["user_id"] == worker_ids[0]
        )

        resp = client.post(
            "/messaging/broadcast",
            json={
                "audience_tokens": [token],
                "confirmed_recipient_count": 1,
                "subject": "Water shutoff",
                "body_md": "Water will be off between 14:00 and 15:00.",
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "sent"
        assert body["recipient_count"] == 1
        assert body["approval_request_id"] is None
        assert len(body["notification_ids"]) == 1

        with factory() as session, tenant_agnostic():
            row = session.get(Notification, body["notification_ids"][0])
            assert row is not None
            assert row.recipient_user_id == worker_ids[0]
            assert row.kind == "agent_message"
            assert row.payload_json["broadcast_subject"] == "Water shutoff"
    finally:
        engine.dispose()


def test_broadcast_multi_recipient_human_session_sends_without_approval() -> None:
    _load_all_models()
    engine: Engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        ctx, worker_ids = _seed_broadcast_workspace(factory)
        client = _build_db_client(factory, ctx)
        preview = client.get("/messaging/broadcast/recipients")
        assert preview.status_code == 200
        employees_token = next(
            group["token"]
            for group in preview.json()["groups"]
            if group["kind"] == "workspace_role" and group["label"] == "Employees"
        )

        resp = client.post(
            "/messaging/broadcast",
            json={
                "audience_tokens": [employees_token],
                "confirmed_recipient_count": 2,
                "subject": "Storm watch",
                "body_md": "Bring patio furniture inside before 16:00.",
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "sent"
        assert body["recipient_count"] == 2
        assert len(body["notification_ids"]) == 2
        assert body["approval_request_id"] is None

        with factory() as session, tenant_agnostic():
            notification_rows = session.scalars(select(Notification)).all()
            approvals = session.scalars(select(ApprovalRequest)).all()
            assert [row.kind for row in notification_rows] == [
                "agent_message",
                "agent_message",
            ]
            assert {row.recipient_user_id for row in notification_rows} == set(
                worker_ids
            )
            assert approvals == []
            assert "approval_needed" not in {row.kind for row in notification_rows}
    finally:
        engine.dispose()


@pytest.mark.parametrize("principal_kind", ["token", "demo", "system"])
def test_broadcast_multi_recipient_non_session_principals_queue_approval(
    principal_kind: PrincipalKind,
) -> None:
    _load_all_models()
    engine: Engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        session_ctx, worker_ids = _seed_broadcast_workspace(factory)
        ctx = replace(session_ctx, principal_kind=principal_kind)
        client = _build_db_client(factory, ctx)
        preview = client.get("/messaging/broadcast/recipients")
        assert preview.status_code == 200
        employees_token = next(
            group["token"]
            for group in preview.json()["groups"]
            if group["kind"] == "workspace_role" and group["label"] == "Employees"
        )

        resp = client.post(
            "/messaging/broadcast",
            json={
                "audience_tokens": [employees_token],
                "confirmed_recipient_count": 2,
                "subject": "Storm watch",
                "body_md": "Bring patio furniture inside before 16:00.",
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending_approval"
        assert body["recipient_count"] == 2
        assert body["notification_ids"] == []
        assert body["approval_request_id"] is not None

        with factory() as session, tenant_agnostic():
            broadcast_rows = session.scalars(
                select(Notification).where(Notification.kind == "agent_message")
            ).all()
            approval_rows = session.scalars(
                select(Notification).where(Notification.kind == "approval_needed")
            ).all()
            approval = session.get(ApprovalRequest, body["approval_request_id"])
            assert broadcast_rows == []
            assert approval_rows
            assert approval is not None
            assert approval.action_json["tool_name"] == "messaging.broadcast"
            assert approval.action_json["tool_input"]["recipient_user_ids"] == list(
                worker_ids
            )
    finally:
        engine.dispose()


def test_broadcast_rejects_stale_audience_token() -> None:
    _load_all_models()
    engine: Engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        ctx, _worker_ids = _seed_broadcast_workspace(factory)
        client = _build_db_client(factory, ctx)

        resp = client.post(
            "/messaging/broadcast",
            json={
                "audience_tokens": ["group:work_role:role_missing"],
                "confirmed_recipient_count": 1,
                "subject": "Wrong audience",
                "body_md": "This should not leave the workspace.",
            },
        )

        assert resp.status_code == 422
        assert resp.json()["error"] == "audience_token_not_found"
    finally:
        engine.dispose()


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
                    "error_id": {"minLength": 1, "type": "string"},
                    "user_message": {"minLength": 1, "type": "string"},
                    "detail": {"type": "string"},
                    "instance": {"type": "string"},
                    "errors": {"items": {"type": "object"}, "type": "array"},
                },
                "required": [
                    "error_id",
                    "instance",
                    "status",
                    "title",
                    "type",
                    "user_message",
                ],
                "type": "object",
            }
        }
    }


def test_native_push_registration_documents_and_returns_problem_json_501() -> None:
    app = FastAPI()
    add_exception_handlers(app)
    app.dependency_overrides[current_workspace_context] = _ctx
    app.include_router(build_messaging_router())
    openapi = app.openapi()
    responses = openapi["paths"]["/notifications/push/tokens"]["post"]["responses"]

    assert responses["501"]["description"] == (
        "Native push token registration is unavailable"
    )
    assert "application/problem+json" in responses["501"]["content"]
    assert "application/json" not in responses["501"]["content"]

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/notifications/push/tokens",
        json={"platform": "ios", "token": "native-token"},
    )

    assert resp.status_code == 501
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["error"] == "push_unavailable"


def test_vapid_public_key_documents_problem_json_503() -> None:
    app = FastAPI()
    app.include_router(build_messaging_router())
    responses = app.openapi()["paths"]["/notifications/push/vapid-key"]["get"][
        "responses"
    ]

    assert responses["503"]["description"] == (
        "Workspace VAPID public key is not configured"
    )
    assert "application/problem+json" in responses["503"]["content"]
    assert "application/json" not in responses["503"]["content"]


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


def test_chat_openapi_bounds_opaque_cursor_query_params() -> None:
    app = FastAPI()
    app.include_router(build_messaging_router())
    openapi = app.openapi()

    channel_params = {
        param["name"]: param
        for param in openapi["paths"]["/chat/channels"]["get"]["parameters"]
    }
    assert channel_params["cursor"]["schema"]["anyOf"] == [
        {"maxLength": 256, "type": "string"},
        {"type": "null"},
    ]
    assert "Opaque forward cursor" in channel_params["cursor"]["description"]
    assert "Omitted or empty" in channel_params["cursor"]["description"]

    message_params = {
        param["name"]: param
        for param in openapi["paths"]["/chat/channels/{channel_id}/messages"]["get"][
            "parameters"
        ]
    }
    assert message_params["before"]["schema"]["anyOf"] == [
        {"maxLength": 256, "type": "string"},
        {"type": "null"},
    ]
    assert "Opaque boundary cursor" in message_params["before"]["description"]


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
    assert isinstance(body["error_id"], str)
    assert body["error_id"]
    assert body["user_message"] == "cursor may be provided at most once"
    assert body["detail"] == "cursor may be provided at most once"


def test_push_subscribe_openapi_bounds_endpoint_to_browser_push_origins() -> None:
    app = FastAPI()
    app.include_router(build_messaging_router())
    openapi = app.openapi()

    schema_ref = openapi["paths"]["/notifications/push/subscribe"]["post"][
        "requestBody"
    ]["content"]["application/json"]["schema"]["$ref"]
    schema_name = schema_ref.removeprefix("#/components/schemas/")
    endpoint_schema = openapi["components"]["schemas"][schema_name]["properties"][
        "endpoint"
    ]

    assert endpoint_schema["pattern"].startswith("^https://")
    assert "fcm\\.googleapis\\.com" in endpoint_schema["pattern"]
    assert "updates\\.push\\.services\\.mozilla\\.com" in endpoint_schema["pattern"]
    assert "web\\.push\\.apple\\.com" in endpoint_schema["pattern"]

    endpoint_pattern = re.compile(endpoint_schema["pattern"])
    valid_examples = [
        "https://fcm.googleapis.com/fcm/send/abc",
        "https://updates.push.services.mozilla.com/abc",
        "https://web.push.apple.com:443/opaque-token?auth=x",
    ]
    invalid_examples = [
        "http://fcm.googleapis.com/fcm/send/abc",
        "https://attacker.example/push/sink",
        "https://user:pass@fcm.googleapis.com/fcm/send/abc",
        "https://fcm.googleapis.com:8443/fcm/send/abc",
        "https://fcm.googleapis.com/fcm/send/abc#frag",
    ]
    for endpoint in valid_examples:
        assert endpoint_pattern.fullmatch(endpoint) is not None
        validate_endpoint(endpoint)
    for endpoint in invalid_examples:
        assert endpoint_pattern.fullmatch(endpoint) is None


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
