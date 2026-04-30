"""Integration coverage for asset document API routes."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.api.assets.assets as asset_api
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.api.deps import (
    current_workspace_context,
    db_session,
    get_mime_sniffer,
    get_storage,
)
from app.api.v1.assets import documents_router
from app.api.v1.assets import router as assets_router
from app.domain.assets.assets import create_asset
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests._fakes.storage import InMemoryStorage
from tests.factories.identity import bootstrap_user, bootstrap_workspace

_NOW = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


class _StaticMimeSniffer:
    def __init__(self, content_type: str | None) -> None:
        self.content_type = content_type

    def sniff(self, payload: bytes, *, hint: str | None = None) -> str | None:
        return self.content_type


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
        audit_correlation_id="corr_asset_documents_api",
    )


def _client(
    session: Session,
    ctx: WorkspaceContext,
    *,
    storage: InMemoryStorage,
    sniffed_type: str | None = "application/pdf",
) -> TestClient:
    app = FastAPI()
    app.include_router(assets_router, prefix="/assets")
    app.include_router(documents_router)

    def override_ctx() -> WorkspaceContext:
        return ctx

    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[current_workspace_context] = override_ctx
    app.dependency_overrides[db_session] = override_db
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_mime_sniffer] = lambda: _StaticMimeSniffer(
        sniffed_type
    )
    return TestClient(app)


def _seed_workspace(
    session: Session,
) -> tuple[WorkspaceContext, str, str, str]:
    owner = bootstrap_user(
        session,
        email="asset-documents-owner@example.com",
        display_name="Asset Documents Owner",
    )
    workspace = bootstrap_workspace(
        session,
        slug="asset-documents-api",
        name="Asset Documents API",
        owner_user_id=owner.id,
    )
    property_id = "prop_asset_documents"
    session.add(
        Property(
            id=property_id,
            name="Asset Documents Villa",
            kind="residence",
            address="1 Asset Documents Road",
            address_json={"line1": "1 Asset Documents Road", "country": "US"},
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
            label="Asset Documents Villa",
            membership_role="owner_workspace",
            share_guest_identity=False,
            status="active",
            created_at=_NOW,
        )
    )
    session.flush()
    return (
        _ctx(workspace.id, owner.id, slug=workspace.slug, owner=True),
        property_id,
        owner.id,
        workspace.slug,
    )


def test_document_upload_stores_sniffed_blob_and_lists_document(
    db_session: Session,
) -> None:
    ctx, property_id, _owner_id, _slug = _seed_workspace(db_session)
    asset = create_asset(
        db_session,
        ctx,
        property_id=property_id,
        label="Warranty fridge",
        token_factory=lambda: "D0CMNT000001",
        clock=FrozenClock(_NOW),
    )
    storage = InMemoryStorage()
    client = _client(db_session, ctx, storage=storage, sniffed_type="application/pdf")
    payload = b"%PDF-1.7\ncrewday"

    uploaded = client.post(
        f"/assets/{asset.id}/documents",
        data={"category": "warranty", "title": "Warranty"},
        files={"file": ("warranty.pdf", payload, "image/png")},
    )
    listed = client.get(f"/assets/{asset.id}/documents")

    assert uploaded.status_code == 201
    body = uploaded.json()
    assert body["category"] == "warranty"
    assert body["filename"] == "warranty.pdf"
    blob_hash = hashlib.sha256(payload).hexdigest()
    assert body["blob_hash"] == blob_hash
    assert storage.exists(blob_hash)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]] == [body["id"]]


def test_workspace_documents_list_and_extraction_routes(
    db_session: Session,
) -> None:
    ctx, property_id, _owner_id, _slug = _seed_workspace(db_session)
    asset = create_asset(
        db_session,
        ctx,
        property_id=property_id,
        label="Document library pump",
        token_factory=lambda: "D0CMNT000005",
        clock=FrozenClock(_NOW),
    )
    storage = InMemoryStorage()
    client = _client(db_session, ctx, storage=storage, sniffed_type="application/pdf")
    created = client.post(
        f"/assets/{asset.id}/documents",
        data={"category": "manual", "title": "Pump manual"},
        files={"file": ("pump.pdf", b"%PDF pump", "application/pdf")},
    )
    assert created.status_code == 201
    document_id = created.json()["id"]

    listed = client.get("/documents")
    filtered = client.get(f"/documents?property_id={property_id}&kind=manual")
    extraction = client.get(f"/documents/{document_id}/extraction")
    page = client.get(f"/documents/{document_id}/extraction/pages/1")
    retry = client.post(f"/documents/{document_id}/extraction/retry")

    assert listed.status_code == 200
    row = listed.json()["data"][0]
    assert row["id"] == document_id
    assert row["asset_id"] == asset.id
    assert row["property_id"] == property_id
    assert row["filename"] == "pump.pdf"
    assert row["uploaded_at"] == created.json()["created_at"]
    assert row["extraction_status"] == "pending"
    assert filtered.status_code == 200
    assert [r["id"] for r in filtered.json()["data"]] == [document_id]
    assert extraction.status_code == 200
    assert extraction.json() == {
        "document_id": document_id,
        "status": "pending",
        "extractor": None,
        "body_preview": "",
        "page_count": 0,
        "token_count": 0,
        "has_secret_marker": False,
        "last_error": None,
        "extracted_at": None,
    }
    assert page.status_code == 200
    assert page.json() == {
        "page": 1,
        "char_start": 0,
        "char_end": 0,
        "body": "",
        "more_pages": False,
    }
    assert retry.status_code == 202


def test_document_upload_rejects_oversized_body(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, property_id, _owner_id, _slug = _seed_workspace(db_session)
    asset = create_asset(
        db_session,
        ctx,
        property_id=property_id,
        label="Manual boiler",
        token_factory=lambda: "D0CMNT000002",
        clock=FrozenClock(_NOW),
    )
    monkeypatch.setattr(asset_api, "_MAX_ASSET_DOCUMENT_BYTES", 4)
    client = _client(
        db_session,
        ctx,
        storage=InMemoryStorage(),
        sniffed_type="application/pdf",
    )

    response = client.post(
        f"/assets/{asset.id}/documents",
        data={"category": "manual"},
        files={"file": ("manual.pdf", b"12345", "application/pdf")},
    )

    assert response.status_code == 413
    assert response.json()["detail"]["error"] == "asset_document_too_large"


def test_document_upload_rejects_invalid_category_before_storing_blob(
    db_session: Session,
) -> None:
    ctx, property_id, _owner_id, _slug = _seed_workspace(db_session)
    asset = create_asset(
        db_session,
        ctx,
        property_id=property_id,
        label="Manual safe",
        token_factory=lambda: "D0CMNT000004",
        clock=FrozenClock(_NOW),
    )
    storage = InMemoryStorage()
    client = _client(db_session, ctx, storage=storage, sniffed_type="application/pdf")
    payload = b"%PDF invalid category"

    response = client.post(
        f"/assets/{asset.id}/documents",
        data={"category": "spreadsheet"},
        files={"file": ("manual.pdf", payload, "application/pdf")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {"error": "invalid", "field": "category"}
    assert not storage.exists(hashlib.sha256(payload).hexdigest())


def test_worker_cannot_delete_document(db_session: Session) -> None:
    ctx, property_id, owner_id, slug = _seed_workspace(db_session)
    worker = bootstrap_user(
        db_session,
        email="asset-documents-worker@example.com",
        display_name="Asset Document Worker",
    )
    db_session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=ctx.workspace_id,
            user_id=worker.id,
            grant_role="worker",
            scope_kind="workspace",
            scope_property_id=None,
            created_at=_NOW,
            created_by_user_id=owner_id,
        )
    )
    asset = create_asset(
        db_session,
        ctx,
        property_id=property_id,
        label="Invoice heater",
        token_factory=lambda: "D0CMNT000003",
        clock=FrozenClock(_NOW),
    )
    storage = InMemoryStorage()
    manager_client = _client(db_session, ctx, storage=storage)
    created = manager_client.post(
        f"/assets/{asset.id}/documents",
        data={"category": "invoice"},
        files={"file": ("invoice.pdf", b"%PDF invoice", "application/pdf")},
    )
    assert created.status_code == 201
    worker_ctx = _ctx(
        ctx.workspace_id,
        worker.id,
        slug=slug,
        role="worker",
    )
    worker_client = _client(db_session, worker_ctx, storage=storage)
    document_id = created.json()["id"]

    denied = worker_client.delete(f"/assets/documents/{document_id}")

    assert denied.status_code == 403
    assert denied.json()["detail"]["action_key"] == "assets.manage_documents"

    list_denied = worker_client.get("/documents")
    get_denied = worker_client.get(f"/documents/{document_id}")
    extraction_denied = worker_client.get(f"/documents/{document_id}/extraction")
    page_denied = worker_client.get(f"/documents/{document_id}/extraction/pages/1")
    retry_denied = worker_client.post(f"/documents/{document_id}/extraction/retry")

    assert list_denied.status_code == 403
    assert list_denied.json()["detail"]["action_key"] == "assets.manage_documents"
    assert get_denied.status_code == 403
    assert get_denied.json()["detail"]["action_key"] == "assets.manage_documents"
    assert extraction_denied.status_code == 403
    assert extraction_denied.json()["detail"]["action_key"] == "assets.manage_documents"
    assert page_denied.status_code == 403
    assert page_denied.json()["detail"]["action_key"] == "assets.manage_documents"
    assert retry_denied.status_code == 403
    assert retry_denied.json()["detail"]["action_key"] == "assets.manage_documents"
