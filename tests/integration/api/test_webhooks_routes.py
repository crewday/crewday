"""Integration coverage for workspace outbound webhook API routes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.adapters.db.integrations.models import WebhookDelivery, WebhookSubscription
from app.api.deps import current_workspace_context, db_session
from app.api.v1 import webhooks as webhook_api
from app.api.v1.webhooks import get_envelope
from app.api.v1.webhooks import router as webhooks_router
from app.events.types import WorkspaceChanged
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.ulid import new_ulid
from tests._fakes.envelope import FakeEnvelope
from tests.factories.identity import bootstrap_user, bootstrap_workspace

_NOW = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


def _ctx(
    workspace_id: str,
    actor_id: str,
    *,
    slug: str,
    role: ActorGrantRole = "manager",
    owner: bool = False,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=role,
        actor_was_owner_member=owner,
        audit_correlation_id="corr_webhooks_api",
    )


def _client(session: Session, ctx: WorkspaceContext) -> TestClient:
    app = FastAPI()
    app.include_router(webhooks_router, prefix="/webhooks")

    def override_ctx() -> WorkspaceContext:
        return ctx

    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[current_workspace_context] = override_ctx
    app.dependency_overrides[db_session] = override_db
    app.dependency_overrides[get_envelope] = FakeEnvelope
    return TestClient(app)


def _seed_workspace(session: Session) -> tuple[WorkspaceContext, WorkspaceContext]:
    owner = bootstrap_user(
        session,
        email="webhooks-api-owner@example.com",
        display_name="Webhook API Owner",
    )
    worker = bootstrap_user(
        session,
        email="webhooks-api-worker@example.com",
        display_name="Webhook API Worker",
    )
    workspace = bootstrap_workspace(
        session,
        slug="webhooks-api",
        name="Webhooks API",
        owner_user_id=owner.id,
    )
    return (
        _ctx(workspace.id, owner.id, slug=workspace.slug, owner=True),
        _ctx(workspace.id, worker.id, slug=workspace.slug, role="worker"),
    )


def test_owner_crud_and_list_latest_delivery(db_session: Session) -> None:
    owner_ctx, _worker_ctx = _seed_workspace(db_session)
    client = _client(db_session, owner_ctx)

    created = client.post(
        "/webhooks",
        json={
            "name": "Hermes",
            "url": "https://hooks.example.test/crewday",
            "events": ["task.completed", "approval.pending"],
            "secret": "0123456789abcdef",
        },
    )

    assert created.status_code == 201
    created_body = created.json()
    webhook_id = created_body["id"]
    assert created_body["secret"] == "0123456789abcdef"
    assert created_body["secret_last_4"] == "cdef"
    assert created_body["paused_reason"] is None
    assert created_body["paused_at"] is None
    assert created_body["last_delivery_at"] is None
    assert created_body["last_delivery_status"] is None

    db_session.add(
        WebhookDelivery(
            id=new_ulid(),
            workspace_id=owner_ctx.workspace_id,
            subscription_id=webhook_id,
            event="task.completed",
            payload_json={"event": "task.completed", "data": {}},
            status="succeeded",
            attempt=1,
            next_attempt_at=None,
            last_status_code=202,
            last_error=None,
            last_attempted_at=_NOW,
            succeeded_at=_NOW,
            dead_lettered_at=None,
            replayed_from_id=None,
            created_at=_NOW,
        )
    )
    db_session.add(
        WebhookDelivery(
            id=new_ulid(),
            workspace_id=owner_ctx.workspace_id,
            subscription_id=webhook_id,
            event="approval.pending",
            payload_json={"event": "approval.pending", "data": {}},
            status="pending",
            attempt=0,
            next_attempt_at=_NOW,
            last_status_code=None,
            last_error=None,
            last_attempted_at=None,
            succeeded_at=None,
            dead_lettered_at=None,
            replayed_from_id=None,
            created_at=datetime(2026, 4, 29, 12, 1, 0, tzinfo=UTC),
        )
    )
    db_session.flush()

    listed = client.get("/webhooks")

    assert listed.status_code == 200
    assert listed.json() == [
        {
            "id": webhook_id,
            "name": "Hermes",
            "url": "https://hooks.example.test/crewday",
            "events": ["task.completed", "approval.pending"],
            "active": True,
            "paused_reason": None,
            "paused_at": None,
            "last_delivery_at": "2026-04-29T12:00:00Z",
            "last_delivery_status": 202,
            "secret_last_4": "cdef",
            "secret": None,
            "created_at": created_body["created_at"],
            "updated_at": created_body["updated_at"],
        }
    ]

    deliveries = client.get(f"/webhooks/{webhook_id}/deliveries")

    assert deliveries.status_code == 200
    delivery_body = deliveries.json()
    assert [row["event"] for row in delivery_body] == [
        "approval.pending",
        "task.completed",
    ]
    assert delivery_body[0]["status"] == "pending"
    assert delivery_body[1]["last_status_code"] == 202

    patched = client.patch(
        f"/webhooks/{webhook_id}",
        json={"active": False, "events": ["issue.reported"]},
    )

    assert patched.status_code == 200
    assert patched.json()["active"] is False
    assert patched.json()["events"] == ["issue.reported"]
    assert patched.json()["last_delivery_status"] == 202

    null_patch = client.patch(f"/webhooks/{webhook_id}", json={"name": None})

    assert null_patch.status_code == 422

    deleted = client.delete(f"/webhooks/{webhook_id}")

    assert deleted.status_code == 204
    assert client.get("/webhooks").json() == []


def test_test_delivery_and_rotate_secret(db_session: Session) -> None:
    owner_ctx, _worker_ctx = _seed_workspace(db_session)
    client = _client(db_session, owner_ctx)
    created = client.post(
        "/webhooks",
        json={
            "name": "Hermes",
            "url": "https://hooks.example.test/crewday",
            "events": ["task.completed", "approval.pending"],
            "secret": "0123456789abcdef",
        },
    )
    webhook_id = created.json()["id"]

    delivery = client.post(
        f"/webhooks/{webhook_id}/test",
        json={"event": "approval.pending"},
    )

    assert delivery.status_code == 201
    delivery_body = delivery.json()
    assert delivery_body["event"] == "approval.pending"
    assert delivery_body["status"] == "pending"
    assert delivery_body["attempt"] == 0
    assert delivery_body["last_status_code"] is None
    assert delivery_body["last_error"] is None

    deliveries = client.get(f"/webhooks/{webhook_id}/deliveries")

    assert deliveries.status_code == 200
    assert deliveries.json()[0]["id"] == delivery_body["id"]

    bad_event = client.post(
        f"/webhooks/{webhook_id}/test",
        json={"event": "expense.approved"},
    )

    assert bad_event.status_code == 422
    assert bad_event.json()["detail"]["error"] == "invalid_webhook"

    rotated = client.post(f"/webhooks/{webhook_id}/rotate-secret")

    assert rotated.status_code == 200
    rotated_body = rotated.json()
    assert rotated_body["secret"] is not None
    assert rotated_body["secret"] != "0123456789abcdef"
    assert rotated_body["secret_last_4"] == rotated_body["secret"][-4:]

    listed = client.get("/webhooks")

    assert listed.status_code == 200
    assert listed.json()[0]["secret"] is None
    assert listed.json()[0]["secret_last_4"] == rotated_body["secret_last_4"]


def test_read_and_enable_clear_auto_pause_state(db_session: Session) -> None:
    owner_ctx, _worker_ctx = _seed_workspace(db_session)
    client = _client(db_session, owner_ctx)
    created = client.post(
        "/webhooks",
        json={
            "name": "Hermes",
            "url": "https://hooks.example.test/crewday",
            "events": ["task.completed"],
            "secret": "0123456789abcdef",
        },
    )
    webhook_id = created.json()["id"]
    row = db_session.get(WebhookSubscription, webhook_id)
    assert row is not None
    row.active = False
    row.paused_reason = "auto_unhealthy"
    row.paused_at = _NOW
    db_session.flush()

    read = client.get(f"/webhooks/{webhook_id}")

    assert read.status_code == 200
    read_body = read.json()
    assert read_body["active"] is False
    assert read_body["paused_reason"] == "auto_unhealthy"
    assert read_body["paused_at"] == "2026-04-29T12:00:00Z"

    enabled = client.post(f"/webhooks/{webhook_id}/enable")

    assert enabled.status_code == 200
    enabled_body = enabled.json()
    assert enabled_body["active"] is True
    assert enabled_body["paused_reason"] is None
    assert enabled_body["paused_at"] is None


def test_patch_active_true_also_clears_auto_pause_state(db_session: Session) -> None:
    owner_ctx, _worker_ctx = _seed_workspace(db_session)
    client = _client(db_session, owner_ctx)
    created = client.post(
        "/webhooks",
        json={
            "name": "Hermes",
            "url": "https://hooks.example.test/crewday",
            "events": ["task.completed"],
            "secret": "0123456789abcdef",
        },
    )
    webhook_id = created.json()["id"]
    row = db_session.get(WebhookSubscription, webhook_id)
    assert row is not None
    row.active = False
    row.paused_reason = "auto_unhealthy"
    row.paused_at = _NOW
    db_session.flush()

    patched = client.patch(f"/webhooks/{webhook_id}", json={"active": True})

    assert patched.status_code == 200
    body = patched.json()
    assert body["active"] is True
    assert body["paused_reason"] is None
    assert body["paused_at"] is None


def test_worker_without_settings_permission_is_rejected(db_session: Session) -> None:
    _owner_ctx, worker_ctx = _seed_workspace(db_session)
    client = _client(db_session, worker_ctx)

    response = client.get("/webhooks")

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "error": "permission_denied",
        "action_key": "scope.edit_settings",
    }


def test_foreign_workspace_subscription_is_not_found(db_session: Session) -> None:
    owner_ctx, _worker_ctx = _seed_workspace(db_session)
    other_owner = bootstrap_user(
        db_session,
        email="webhooks-api-other@example.com",
        display_name="Other Webhook Owner",
    )
    other_workspace = bootstrap_workspace(
        db_session,
        slug="webhooks-api-other",
        name="Other Webhooks API",
        owner_user_id=other_owner.id,
    )
    other_ctx = _ctx(
        other_workspace.id,
        other_owner.id,
        slug=other_workspace.slug,
        owner=True,
    )
    other_client = _client(db_session, other_ctx)
    created = other_client.post(
        "/webhooks",
        json={
            "name": "Foreign",
            "url": "https://hooks.example.test/foreign",
            "events": ["task.completed"],
            "secret": "0123456789abcdef",
        },
    )
    assert created.status_code == 201

    owner_client = _client(db_session, owner_ctx)
    response = owner_client.patch(
        f"/webhooks/{created.json()['id']}",
        json={"active": False},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {"error": "webhook_not_found"}

    deliveries = owner_client.get(f"/webhooks/{created.json()['id']}/deliveries")

    assert deliveries.status_code == 404
    assert deliveries.json()["detail"] == {"error": "webhook_not_found"}


def test_mutations_publish_workspace_changed(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_ctx, _worker_ctx = _seed_workspace(db_session)
    client = _client(db_session, owner_ctx)
    published: list[WorkspaceChanged] = []
    monkeypatch.setattr(webhook_api.default_event_bus, "publish", published.append)

    created = client.post(
        "/webhooks",
        json={
            "name": "Hermes",
            "url": "https://hooks.example.test/crewday",
            "events": ["task.completed"],
            "secret": "0123456789abcdef",
        },
    )
    webhook_id = created.json()["id"]
    rotated = client.post(f"/webhooks/{webhook_id}/rotate-secret")
    tested = client.post(f"/webhooks/{webhook_id}/test")
    patched = client.patch(f"/webhooks/{webhook_id}", json={"active": False})
    deleted = client.delete(f"/webhooks/{webhook_id}")

    assert created.status_code == 201
    assert rotated.status_code == 200
    assert tested.status_code == 201
    assert patched.status_code == 200
    assert deleted.status_code == 204
    assert [event.changed_keys for event in published] == [
        ("webhooks",),
        ("webhooks",),
        ("webhooks",),
        ("webhooks",),
        ("webhooks",),
    ]
    assert {event.workspace_id for event in published} == {owner_ctx.workspace_id}
