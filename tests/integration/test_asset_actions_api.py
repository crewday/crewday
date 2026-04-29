"""Integration coverage for asset action API routes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.api.deps import current_workspace_context, db_session, get_storage
from app.api.v1.assets import router as assets_router
from app.domain.assets.assets import create_asset
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests._fakes.storage import InMemoryStorage
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
        audit_correlation_id="corr_asset_actions_api",
    )


def _client(
    session: Session,
    ctx: WorkspaceContext,
    storage: InMemoryStorage | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(assets_router, prefix="/assets")
    resolved_storage = storage if storage is not None else InMemoryStorage()

    def override_ctx() -> WorkspaceContext:
        return ctx

    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[current_workspace_context] = override_ctx
    app.dependency_overrides[db_session] = override_db
    app.dependency_overrides[get_storage] = lambda: resolved_storage
    return TestClient(app)


def _seed_property(
    session: Session, *, workspace_id: str, property_id: str, label: str
) -> None:
    session.add(
        Property(
            id=property_id,
            name=label,
            kind="residence",
            address=f"{label} Road",
            address_json={"line1": f"{label} Road", "country": "US"},
            country="US",
            timezone="UTC",
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_NOW,
            updated_at=_NOW,
            deleted_at=None,
        )
    )
    session.add(
        PropertyWorkspace(
            property_id=property_id,
            workspace_id=workspace_id,
            label=label,
            membership_role="owner_workspace",
            share_guest_identity=False,
            status="active",
            created_at=_NOW,
        )
    )
    session.flush()


def test_worker_records_action_only_on_assigned_property(
    db_session: Session,
) -> None:
    owner = bootstrap_user(
        db_session,
        email="asset-actions-api-owner@example.com",
        display_name="Asset Actions API Owner",
    )
    workspace = bootstrap_workspace(
        db_session,
        slug="asset-actions-api",
        name="Asset Actions API",
        owner_user_id=owner.id,
    )
    manager_ctx = _ctx(workspace.id, owner.id, slug=workspace.slug, owner=True)
    worker = bootstrap_user(
        db_session,
        email="asset-actions-worker@example.com",
        display_name="Asset Worker",
    )
    visible_property = "prop_asset_actions_visible"
    hidden_property = "prop_asset_actions_hidden"
    _seed_property(
        db_session,
        workspace_id=workspace.id,
        property_id=visible_property,
        label="Visible Asset Villa",
    )
    _seed_property(
        db_session,
        workspace_id=workspace.id,
        property_id=hidden_property,
        label="Hidden Asset Villa",
    )
    db_session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace.id,
            user_id=worker.id,
            grant_role="worker",
            scope_kind="workspace",
            scope_property_id=visible_property,
            created_at=_NOW,
            created_by_user_id=owner.id,
        )
    )
    db_session.flush()
    visible_asset = create_asset(
        db_session,
        manager_ctx,
        property_id=visible_property,
        label="Visible pump",
        token_factory=lambda: "W0RKER000001",
        clock=FrozenClock(_NOW),
    )
    hidden_asset = create_asset(
        db_session,
        manager_ctx,
        property_id=hidden_property,
        label="Hidden pump",
        token_factory=lambda: "W0RKER000002",
        clock=FrozenClock(_NOW),
    )
    worker_ctx = _ctx(
        workspace.id,
        worker.id,
        slug=workspace.slug,
        role="worker",
        owner=False,
    )
    client = _client(db_session, worker_ctx)

    allowed = client.post(
        f"/assets/{visible_asset.id}/actions",
        json={"kind": "read", "meter_reading": "42.2500"},
    )
    denied = client.post(
        f"/assets/{hidden_asset.id}/actions",
        json={"kind": "read", "meter_reading": "42.2500"},
    )

    assert allowed.status_code == 201
    assert allowed.json()["meter_reading"] == "42.2500"
    assert denied.status_code == 403
    assert denied.json()["detail"]["action_key"] == "assets.record_action"
