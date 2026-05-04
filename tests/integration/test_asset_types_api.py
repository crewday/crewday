"""Integration coverage for the asset-type HTTP surface."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.assets.bootstrap import BASE_ASSET_TYPE_CATALOG
from app.adapters.db.assets.models import Asset, AssetType
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.places.models import Property
from app.api.deps import current_workspace_context, db_session
from app.api.errors import CONTENT_TYPE_PROBLEM_JSON, add_exception_handlers
from app.api.v1.assets import router as assets_router
from app.tenancy import WorkspaceContext
from tests.factories.identity import bootstrap_user, bootstrap_workspace


def _assert_problem_error(response: Response, *, error: str) -> dict[str, object]:
    assert response.headers["content-type"].startswith(CONTENT_TYPE_PROBLEM_JSON)
    body = response.json()
    assert isinstance(body, dict)
    assert body["error"] == error
    return body


def _ctx(workspace_id: str, actor_id: str, *, slug: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="corr_asset_types_api",
    )


def _client(session: Session, ctx: WorkspaceContext) -> TestClient:
    app = FastAPI()
    app.include_router(assets_router, prefix="/assets")
    add_exception_handlers(app)

    def override_ctx() -> WorkspaceContext:
        return ctx

    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[current_workspace_context] = override_ctx
    app.dependency_overrides[db_session] = override_db
    return TestClient(app)


def _seed_workspace(session: Session) -> tuple[WorkspaceContext, TestClient]:
    owner = bootstrap_user(
        session,
        email="asset-types@example.com",
        display_name="Asset Manager",
    )
    workspace = bootstrap_workspace(
        session,
        slug="asset-types",
        name="Asset Types",
        owner_user_id=owner.id,
    )
    ctx = _ctx(workspace.id, owner.id, slug=workspace.slug)
    return ctx, _client(session, ctx)


def _action() -> dict[str, object]:
    return {
        "kind": "service",
        "label": "Annual service",
        "interval_days": 365,
        "warn_before_days": 30,
    }


def test_bootstrap_catalog_and_crud_endpoints_audit(db_session: Session) -> None:
    ctx, client = _seed_workspace(db_session)

    listed = client.get("/assets/asset_types")
    assert listed.status_code == 200
    assert len(listed.json()["data"]) == len(BASE_ASSET_TYPE_CATALOG)

    created = client.post(
        "/assets/asset_types",
        json={
            "slug": "wine_fridge",
            "name": "Wine fridge",
            "category": "appliance",
            "icon": "Refrigerator",
            "default_actions": [_action()],
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["key"] == "wine_fridge"
    assert body["default_actions"] == [_action()]

    fetched = client.get(f"/assets/asset_types/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == body["id"]

    patched = client.patch(
        f"/assets/asset_types/{body['id']}",
        json={
            "name": "Cellar fridge",
            "default_actions_json": [
                {
                    "kind": "inspect",
                    "label": "Check thermostat",
                    "interval_days": 90,
                    "warn_before_days": 7,
                }
            ],
        },
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Cellar fridge"
    assert patched.json()["default_actions_json"][0]["kind"] == "inspect"

    deleted = client.delete(f"/assets/asset_types/{body['id']}")
    assert deleted.status_code == 204
    assert db_session.get(AssetType, body["id"]) is None

    actions = db_session.scalars(
        select(AuditLog.action)
        .where(AuditLog.workspace_id == ctx.workspace_id)
        .where(AuditLog.entity_id == body["id"])
        .order_by(AuditLog.created_at, AuditLog.id)
    ).all()
    assert actions == [
        "asset_type.create",
        "asset_type.update",
        "asset_type.delete",
    ]


def test_list_asset_types_paginates_without_repeating_rows(
    db_session: Session,
) -> None:
    _ctx, client = _seed_workspace(db_session)

    first = client.get("/assets/asset_types", params={"limit": 5})
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["has_more"] is True
    assert first_body["next_cursor"] is not None

    second = client.get(
        "/assets/asset_types",
        params={"limit": 5, "cursor": first_body["next_cursor"]},
    )
    assert second.status_code == 200
    second_body = second.json()

    first_ids = {row["id"] for row in first_body["data"]}
    second_ids = {row["id"] for row in second_body["data"]}
    assert len(first_ids) == 5
    assert len(second_ids) == 5
    assert first_ids.isdisjoint(second_ids)


def test_delete_archives_type_when_asset_references_it(db_session: Session) -> None:
    ctx, client = _seed_workspace(db_session)
    created = client.post(
        "/assets/asset_types",
        json={
            "key": "kayak",
            "name": "Kayak",
            "category": "outdoor",
            "default_actions": [_action()],
        },
    )
    assert created.status_code == 201
    type_id = created.json()["id"]
    db_session.add(
        Property(
            id="asset_type_api_property",
            name="Asset API property",
            kind="residence",
            address="1 Asset Way",
            address_json={"line1": "1 Asset Way", "country": "US"},
            country="US",
            timezone="UTC",
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=datetime(2026, 4, 29, tzinfo=UTC),
            updated_at=datetime(2026, 4, 29, tzinfo=UTC),
            deleted_at=None,
        )
    )
    db_session.flush()
    db_session.add(
        Asset(
            id="asset_type_api_asset",
            workspace_id=ctx.workspace_id,
            property_id="asset_type_api_property",
            asset_type_id=type_id,
            name="Kayak 1",
            condition="good",
            status="active",
            qr_token="KAYAK0000001"[:12],
            created_at=datetime(2026, 4, 29, tzinfo=UTC),
            updated_at=datetime(2026, 4, 29, tzinfo=UTC),
        )
    )
    db_session.flush()

    deleted = client.delete(f"/assets/asset_types/{type_id}")
    assert deleted.status_code == 204

    hidden = client.get(f"/assets/asset_types/{type_id}")
    assert hidden.status_code == 404
    _assert_problem_error(hidden, error="asset_type_not_found")

    archived = client.get(
        "/assets/asset_types",
        params={"include_archived": True, "workspace_only": True},
    )
    assert archived.status_code == 200
    archived_row = next(row for row in archived.json()["data"] if row["id"] == type_id)
    assert archived_row["archived_at"] is not None


def test_manage_type_endpoints_require_capability(db_session: Session) -> None:
    owner = bootstrap_user(
        db_session,
        email="asset-types-denied@example.com",
        display_name="Denied",
    )
    workspace = bootstrap_workspace(
        db_session,
        slug="asset-types-denied",
        name="Asset Types Denied",
        owner_user_id=owner.id,
    )
    denied_ctx = _ctx(workspace.id, "not_a_manager", slug=workspace.slug)
    client = _client(db_session, denied_ctx)

    denied = client.post(
        "/assets/asset_types",
        json={
            "key": "shed",
            "name": "Shed",
            "category": "outdoor",
            "default_actions": [_action()],
        },
    )
    assert denied.status_code == 403
    assert (
        _assert_problem_error(denied, error="permission_denied")["action_key"]
        == "assets.manage_types"
    )
