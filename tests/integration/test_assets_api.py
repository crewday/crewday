"""Integration coverage for the tracked asset HTTP surface."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.assets.models import AssetDocument, AssetType
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.places.models import Area, Property, PropertyWorkspace
from app.api.deps import current_workspace_context, db_session
from app.api.v1.assets import router as assets_router
from app.api.v1.assets import scan_router as asset_scan_router
from app.tenancy import WorkspaceContext
from tests.factories.identity import bootstrap_user, bootstrap_workspace

_NOW = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


def _ctx(workspace_id: str, actor_id: str, *, slug: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="corr_assets_api",
    )


def _client(session: Session, ctx: WorkspaceContext) -> TestClient:
    app = FastAPI()
    app.include_router(assets_router, prefix="/assets")
    app.include_router(asset_scan_router, prefix="/asset")

    def override_ctx() -> WorkspaceContext:
        return ctx

    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[current_workspace_context] = override_ctx
    app.dependency_overrides[db_session] = override_db
    return TestClient(app)


def _workspace_prefixed_client(session: Session, ctx: WorkspaceContext) -> TestClient:
    app = FastAPI()
    app.include_router(assets_router, prefix="/w/{slug}/api/v1/assets")
    app.include_router(asset_scan_router, prefix="/w/{slug}/api/v1/asset")

    def override_ctx() -> WorkspaceContext:
        return ctx

    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[current_workspace_context] = override_ctx
    app.dependency_overrides[db_session] = override_db
    return TestClient(app)


def _seed_workspace(session: Session) -> tuple[WorkspaceContext, TestClient, str, str]:
    owner = bootstrap_user(
        session,
        email="assets-api@example.com",
        display_name="Asset API Manager",
    )
    workspace = bootstrap_workspace(
        session,
        slug="assets-api",
        name="Assets API",
        owner_user_id=owner.id,
    )
    ctx = _ctx(workspace.id, owner.id, slug=workspace.slug)
    property_id = "prop_assets_api"
    area_id = "area_assets_api_kitchen"
    session.add(
        Property(
            id=property_id,
            name="Asset API Villa",
            kind="residence",
            address="1 Asset API Way",
            address_json={"line1": "1 Asset API Way", "country": "US"},
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
            workspace_id=workspace.id,
            label="Asset API Villa",
            membership_role="owner_workspace",
            share_guest_identity=False,
            status="active",
            created_at=_NOW,
        )
    )
    session.flush()
    session.add(
        Area(
            id=area_id,
            property_id=property_id,
            unit_id=None,
            name="Kitchen",
            label="Kitchen",
            kind="indoor_room",
            icon=None,
            ordering=10,
            parent_area_id=None,
            notes_md="",
            created_at=_NOW,
            updated_at=_NOW,
            deleted_at=None,
        )
    )
    session.flush()
    return ctx, _client(session, ctx), property_id, area_id


def test_assets_crud_move_regenerate_scan_and_png(db_session: Session) -> None:
    ctx, client, property_id, area_id = _seed_workspace(db_session)

    created = client.post(
        "/assets",
        json={
            "label": "Kitchen fridge",
            "property_id": property_id,
            "area_id": area_id,
            "condition": "good",
            "metadata": {"warranty_provider": "Acme"},
        },
    )
    assert created.status_code == 201
    asset = created.json()
    assert asset["name"] == "Kitchen fridge"
    assert len(asset["qr_token"]) == 12
    db_session.add(
        AssetDocument(
            id="asset_doc_fridge_manual",
            workspace_id=ctx.workspace_id,
            file_id=None,
            blob_hash="a" * 64,
            filename="fridge-manual.pdf",
            asset_id=asset["id"],
            property_id=None,
            kind="manual",
            title="Fridge manual",
            notes_md=None,
            expires_on=None,
            amount_cents=None,
            amount_currency=None,
            created_at=_NOW,
            updated_at=_NOW,
            deleted_at=None,
        )
    )
    db_session.flush()

    listed = client.get("/assets", params={"property_id": property_id})
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]] == [asset["id"]]

    type_list = client.get("/assets/asset_types")
    assert type_list.status_code == 200

    fetched = client.get(f"/assets/{asset['id']}")
    assert fetched.status_code == 200
    detail = fetched.json()
    assert detail["asset"]["id"] == asset["id"]
    assert detail["asset"]["area"] == "Kitchen"
    assert detail["property"]["name"] == "Asset API Villa"
    assert detail["property"]["client_org_id"] is None
    assert detail["property"]["owner_user_id"] is None
    assert detail["actions"] == []
    assert detail["documents"][0]["property_id"] == property_id
    assert detail["documents"][0]["title"] == "Fridge manual"

    recorded = client.post(
        f"/assets/{asset['id']}/actions",
        json={"kind": "inspect"},
    )
    assert recorded.status_code == 201
    completed = client.post(
        f"/assets/{asset['id']}/actions/{recorded.json()['id']}/complete"
    )
    assert completed.status_code == 201
    assert completed.json()["kind"] == "inspect"

    patched = client.patch(
        f"/assets/{asset['id']}",
        json={"name": "Cellar fridge", "status": "in_repair"},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "in_repair"

    cleared = client.patch(
        f"/assets/{asset['id']}",
        json={"make": None, "metadata": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["make"] is None
    assert cleared.json()["metadata"] is None

    moved = client.post(
        f"/assets/{asset['id']}/move",
        json={"property_id": property_id, "area_id": None},
    )
    assert moved.status_code == 200
    assert moved.json()["area_id"] is None

    old_token = asset["qr_token"]
    regenerated = client.post(f"/assets/{asset['id']}/regenerate_qr")
    assert regenerated.status_code == 200
    new_token = regenerated.json()["qr_token"]
    assert new_token != old_token

    old_scan = client.get(f"/assets/scan/{old_token}")
    assert old_scan.status_code == 404
    new_scan = client.get(f"/asset/scan/{new_token}")
    assert new_scan.status_code == 200
    assert new_scan.json()["id"] == asset["id"]

    png = client.get(f"/assets/{asset['id']}/qr.png")
    assert png.status_code == 200
    assert png.headers["content-type"] == "image/png"
    assert png.content.startswith(b"\x89PNG\r\n\x1a\n")

    deleted = client.delete(f"/assets/{asset['id']}")
    assert deleted.status_code == 204
    hidden = client.get(f"/assets/{asset['id']}")
    assert hidden.status_code == 404
    restored = client.put(f"/assets/{asset['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None

    actions = db_session.scalars(
        select(AuditLog.action)
        .where(AuditLog.workspace_id == ctx.workspace_id)
        .where(AuditLog.entity_id == asset["id"])
        .order_by(AuditLog.created_at, AuditLog.id)
    ).all()
    assert actions == [
        "asset.create",
        "asset.update",
        "asset.update",
        "asset.move",
        "asset.qr_regenerate",
        "asset.delete",
        "asset.restore",
    ]


def test_asset_detail_default_actions_can_be_completed(db_session: Session) -> None:
    ctx, client, property_id, area_id = _seed_workspace(db_session)
    db_session.add(
        AssetType(
            id="asset_type_default_actions",
            workspace_id=ctx.workspace_id,
            key="field-pump",
            name="Field pump",
            category="plumbing",
            icon_name="droplets",
            description_md=None,
            default_lifespan_years=None,
            default_actions_json=[
                {
                    "kind": "inspect",
                    "label": "Visual inspection",
                    "interval_days": 30,
                    "warn_before_days": 7,
                },
                {
                    "kind": "inspect",
                    "label": "Seal inspection",
                    "interval_days": 90,
                    "warn_before_days": 14,
                },
            ],
            created_at=_NOW,
            updated_at=_NOW,
            deleted_at=None,
        )
    )
    db_session.flush()

    created = client.post(
        "/assets",
        json={
            "label": "Pool pump",
            "property_id": property_id,
            "area_id": area_id,
            "asset_type_id": "asset_type_default_actions",
            "condition": "good",
        },
    )
    assert created.status_code == 201
    asset = created.json()

    detail = client.get(f"/assets/{asset['id']}")
    assert detail.status_code == 200
    actions = detail.json()["actions"]
    action_ids = [action["id"] for action in actions]
    assert action_ids == ["default__0__inspect", "default__1__inspect"]

    completed = client.post(f"/assets/{asset['id']}/actions/{action_ids[0]}/complete")
    assert completed.status_code == 201
    assert completed.json()["kind"] == "inspect"
    detail_after_complete = client.get(f"/assets/{asset['id']}")
    assert detail_after_complete.status_code == 200
    completed_actions = detail_after_complete.json()["actions"]
    assert [action["id"] for action in completed_actions] == action_ids
    assert completed_actions[0]["last_performed_at"] is not None
    assert completed_actions[1]["last_performed_at"] is None


def test_qr_png_uses_workspace_prefixed_scan_route(db_session: Session) -> None:
    ctx, _client, property_id, _area_id = _seed_workspace(db_session)
    client = _workspace_prefixed_client(db_session, ctx)

    created = client.post(
        f"/w/{ctx.workspace_slug}/api/v1/assets",
        json={"label": "Front gate", "property_id": property_id},
    )
    assert created.status_code == 201
    asset = created.json()

    png = client.get(f"/w/{ctx.workspace_slug}/api/v1/assets/{asset['id']}/qr.png")
    assert png.status_code == 200
    assert png.content.startswith(b"\x89PNG\r\n\x1a\n")
