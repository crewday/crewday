"""HTTP tests for owner workspace export."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.assets.models import AssetDocument
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.api.deps import get_storage
from app.api.v1.admin import router as admin_router
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests._fakes.storage import InMemoryStorage
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _client(
    ctx: WorkspaceContext,
    factory: sessionmaker[Session],
    storage: InMemoryStorage,
) -> TestClient:
    client = build_client(
        [(f"/w/{ctx.workspace_slug}/api/v1/admin", admin_router)],
        factory,
        ctx,
    )
    client.app.dependency_overrides[get_storage] = lambda: storage
    return client


def _path(ctx: WorkspaceContext) -> str:
    return f"/w/{ctx.workspace_slug}/api/v1/admin/workspace/export"


def _seed_property_document(
    session: Session,
    *,
    workspace_id: str,
    blob_hash: str,
    title: str,
) -> str:
    property_id = new_ulid()
    session.add(
        Property(
            id=property_id,
            name=f"{title} Property",
            kind="residence",
            address="1 Test St",
            address_json={"country": "US"},
            country="US",
            timezone="UTC",
            tags_json=[],
            welcome_defaults_json={},
            settings_override_json={},
            property_notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
    )
    session.add(
        PropertyWorkspace(
            property_id=property_id,
            workspace_id=workspace_id,
            label=title,
            membership_role="owner_workspace",
            share_guest_identity=True,
            auto_shift_from_occurrence=False,
            status="active",
            created_at=_PINNED,
        )
    )
    session.flush()
    session.add(
        AssetDocument(
            id=new_ulid(),
            workspace_id=workspace_id,
            file_id=None,
            blob_hash=blob_hash,
            filename=f"{title}.txt",
            asset_id=None,
            property_id=property_id,
            kind="manual",
            title=title,
            notes_md=None,
            created_at=_PINNED,
            updated_at=_PINNED,
        )
    )
    return property_id


def _link_shared_property(
    session: Session,
    *,
    owner_workspace_id: str,
    linked_workspace_id: str,
) -> str:
    property_id = new_ulid()
    session.add(
        Property(
            id=property_id,
            name="Shared Property",
            kind="residence",
            address="2 Shared St",
            address_json={"country": "US"},
            country="US",
            timezone="UTC",
            tags_json=[],
            welcome_defaults_json={},
            settings_override_json={},
            property_notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
    )
    session.flush()
    session.add_all(
        [
            PropertyWorkspace(
                property_id=property_id,
                workspace_id=owner_workspace_id,
                label="shared-owner",
                membership_role="owner_workspace",
                share_guest_identity=True,
                auto_shift_from_occurrence=False,
                status="active",
                created_at=_PINNED,
            ),
            PropertyWorkspace(
                property_id=property_id,
                workspace_id=linked_workspace_id,
                label="shared-linked",
                membership_role="managed_workspace",
                share_guest_identity=False,
                auto_shift_from_occurrence=False,
                status="active",
                created_at=_PINNED,
            ),
        ]
    )
    return property_id


def _read_zip(response_content: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(response_content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_owner_downloads_workspace_export_with_manifest_rows_and_files(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, ws_id = owner_ctx
    storage = InMemoryStorage()
    payload = b"owner workspace document"
    blob_hash = hashlib.sha256(payload).hexdigest()
    storage.put(blob_hash, io.BytesIO(payload), content_type="text/plain")

    with factory() as session:
        _seed_property_document(
            session,
            workspace_id=ws_id,
            blob_hash=blob_hash,
            title="owner-doc",
        )
        session.commit()

    response = _client(ctx, factory, storage).post(_path(ctx))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="crewday-workspace-ws-identity-'
    )
    files = _read_zip(response.content)
    manifest = json.loads(files["manifest.json"])
    documents = json.loads(files["data/asset_document.json"])
    assert manifest["schema_version"] == 1
    assert manifest["workspace"] == {
        "id": ws_id,
        "slug": ctx.workspace_slug,
        "name": "Identity WS",
    }
    assert manifest["tables"]
    assert manifest["files"] == [
        {
            "content_hash": blob_hash,
            "path": f"files/{blob_hash[:2]}/{blob_hash}",
            "size_bytes": len(payload),
        }
    ]
    assert files[f"files/{blob_hash[:2]}/{blob_hash}"] == payload
    assert [row["blob_hash"] for row in documents] == [blob_hash]

    with factory() as session:
        audit = session.scalar(
            select(AuditLog).where(AuditLog.action == "workspace.export_requested")
        )
        assert audit is not None
        assert audit.workspace_id == ws_id
        assert audit.actor_id == ctx.actor_id
        assert audit.diff["file_count"] == 1
        assert "owner workspace document" not in json.dumps(audit.diff)


def test_export_excludes_other_workspace_rows_and_files(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, ws_id = owner_ctx
    storage = InMemoryStorage()
    own_payload = b"own"
    other_payload = b"other"
    own_hash = hashlib.sha256(own_payload).hexdigest()
    other_hash = hashlib.sha256(other_payload).hexdigest()
    storage.put(own_hash, io.BytesIO(own_payload), content_type="text/plain")
    storage.put(other_hash, io.BytesIO(other_payload), content_type="text/plain")

    with factory() as session:
        other_owner = bootstrap_user(
            session, email="other-export@example.com", display_name="Other"
        )
        other_ws = bootstrap_workspace(
            session,
            slug="other-export",
            name="Other Export",
            owner_user_id=other_owner.id,
        )
        own_property_id = _seed_property_document(
            session,
            workspace_id=ws_id,
            blob_hash=own_hash,
            title="own-doc",
        )
        _seed_property_document(
            session,
            workspace_id=other_ws.id,
            blob_hash=other_hash,
            title="other-doc",
        )
        _link_shared_property(
            session,
            owner_workspace_id=other_ws.id,
            linked_workspace_id=ws_id,
        )
        session.commit()

    files = _read_zip(_client(ctx, factory, storage).post(_path(ctx)).content)
    manifest = json.loads(files["manifest.json"])
    documents = json.loads(files["data/asset_document.json"])
    properties = json.loads(files["data/property.json"])

    assert [row["blob_hash"] for row in documents] == [own_hash]
    assert [row["id"] for row in properties] == [own_property_id]
    assert manifest["files"][0]["content_hash"] == own_hash
    assert other_hash not in response_text(files)


def test_export_includes_workspace_task_thread_rows(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, ws_id = owner_ctx

    with factory() as session:
        task_id = new_ulid()
        session.execute(
            Base.metadata.tables["occurrence"]
            .insert()
            .values(
                id=task_id,
                workspace_id=ws_id,
                template_id=None,
                schedule_id=None,
                property_id=None,
                unit_id=None,
                area_id=None,
                title="Exported task",
                state="pending",
                starts_at=_PINNED,
                ends_at=_PINNED + timedelta(hours=1),
                assignee_user_id=None,
                scheduled_for_local=None,
                originally_scheduled_for=None,
                completed_at=None,
                completed_by_user_id=None,
                reviewer_user_id=None,
                reviewed_at=None,
                completion_note_md=None,
                skipped_reason=None,
                cancellation_reason=None,
                description_md=None,
                priority="normal",
                photo_evidence="disabled",
                settings_override_json={},
                duration_minutes=None,
                expected_role_id=None,
                asset_id=None,
                asset_action_id=None,
                linked_instruction_ids=[],
                inventory_consumption_json={},
                is_personal=False,
                created_by_user_id=ctx.actor_id,
                reservation_id=None,
                lifecycle_rule_id=None,
                occurrence_key=None,
                created_at=_PINNED,
            )
        )
        session.execute(
            Base.metadata.tables["comment"]
            .insert()
            .values(
                id=new_ulid(),
                workspace_id=ws_id,
                occurrence_id=task_id,
                author_user_id=ctx.actor_id,
                body_md="Exported comment",
                created_at=_PINNED,
                attachments_json=[],
                kind="user",
                mentioned_user_ids=[],
                edited_at=None,
                deleted_at=None,
                llm_call_id=None,
            )
        )
        session.execute(
            Base.metadata.tables["evidence"]
            .insert()
            .values(
                id=new_ulid(),
                workspace_id=ws_id,
                occurrence_id=task_id,
                kind="note",
                blob_hash=None,
                note_md="Exported evidence",
                gps_lat=None,
                gps_lon=None,
                checklist_snapshot_json=[],
                created_at=_PINNED,
                created_by_user_id=ctx.actor_id,
                deleted_at=None,
            )
        )
        session.execute(
            Base.metadata.tables["nl_task_preview"]
            .insert()
            .values(
                id=new_ulid(),
                workspace_id=ws_id,
                requested_by_user_id=ctx.actor_id,
                original_text="Create a task from natural language",
                resolved_json={"title": "Exported task"},
                assumptions_json=[],
                ambiguities_json=[],
                created_at=_PINNED,
                expires_at=_PINNED,
                committed_at=None,
            )
        )
        session.commit()

    files = _read_zip(_client(ctx, factory, InMemoryStorage()).post(_path(ctx)).content)

    assert json.loads(files["data/comment.json"])[0]["body_md"] == "Exported comment"
    assert json.loads(files["data/evidence.json"])[0]["note_md"] == "Exported evidence"
    assert json.loads(files["data/nl_task_preview.json"])[0]["original_text"] == (
        "Create a task from natural language"
    )


def test_export_redacts_workspace_secret_material(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, ws_id = owner_ctx

    with factory() as session:
        property_id = _seed_property_document(
            session,
            workspace_id=ws_id,
            blob_hash="0" * 64,
            title="secret-redaction",
        )
        session.execute(
            Base.metadata.tables["asset"]
            .insert()
            .values(
                id=new_ulid(),
                workspace_id=ws_id,
                property_id=property_id,
                area_id=None,
                asset_type_id=None,
                name="Door keypad",
                make=None,
                model=None,
                serial_number=None,
                condition="good",
                status="active",
                installed_on=None,
                purchased_on=None,
                purchase_price_cents=None,
                purchase_currency=None,
                purchase_vendor=None,
                warranty_expires_on=None,
                expected_lifespan_years=None,
                estimated_replacement_on=None,
                cover_photo_file_id=None,
                qr_token="SECRETQR1234",
                guest_visible=False,
                guest_instructions_md=None,
                notes_md=None,
                settings_override_json={},
                created_at=_PINNED,
                updated_at=_PINNED,
                deleted_at=None,
            )
        )
        session.execute(
            Base.metadata.tables["webhook_subscription"]
            .insert()
            .values(
                id=new_ulid(),
                workspace_id=ws_id,
                name="ops hook",
                url="https://hooks.example.test/tenant",
                secret_blob="webhook-secret-ciphertext",
                secret_last_4="last",
                events_json=["task.completed"],
                active=True,
                paused_reason=None,
                paused_at=None,
                created_at=_PINNED,
                updated_at=_PINNED,
            )
        )
        session.execute(
            Base.metadata.tables["ical_feed"]
            .insert()
            .values(
                id=new_ulid(),
                workspace_id=ws_id,
                property_id=property_id,
                unit_id=None,
                url="https://calendar.example.test/private-token.ics",
                provider="generic",
                poll_cadence="*/15 * * * *",
                last_polled_at=None,
                last_etag=None,
                last_error=None,
                enabled=True,
                created_at=_PINNED,
            )
        )
        session.execute(
            Base.metadata.tables["payout_destination"]
            .insert()
            .values(
                id=new_ulid(),
                workspace_id=ws_id,
                user_id=ctx.actor_id,
                kind="bank_account",
                currency="USD",
                display_stub="Checking *1234",
                secret_ref_id="payout-secret-ref",
                country="US",
                label="Payroll",
                archived_at=None,
                created_at=_PINNED,
                updated_at=_PINNED,
            )
        )
        session.execute(
            Base.metadata.tables["push_token"]
            .insert()
            .values(
                id=new_ulid(),
                workspace_id=ws_id,
                user_id=ctx.actor_id,
                endpoint="https://push.example.test/device-secret",
                p256dh="push-public-key",
                auth="push-auth-secret",
                user_agent="pytest",
                created_at=_PINNED,
                last_used_at=None,
            )
        )
        session.commit()

    files = _read_zip(_client(ctx, factory, InMemoryStorage()).post(_path(ctx)).content)
    export_text = response_text(files)

    assert "SECRETQR1234" not in export_text
    assert "webhook-secret-ciphertext" not in export_text
    assert "private-token.ics" not in export_text
    assert "payout-secret-ref" not in export_text
    assert "device-secret" not in export_text
    assert "push-public-key" not in export_text
    assert "push-auth-secret" not in export_text
    assert json.loads(files["data/asset.json"])[0]["qr_token"] == "<redacted>"
    assert json.loads(files["data/webhook_subscription.json"])[0]["secret_blob"] == (
        "<redacted>"
    )
    assert json.loads(files["data/ical_feed.json"])[0]["url"] == "<redacted>"
    assert json.loads(files["data/payout_destination.json"])[0]["secret_ref_id"] == (
        "<redacted>"
    )
    assert json.loads(files["data/push_token.json"])[0]["endpoint"] == "<redacted>"


def test_non_owner_cannot_export(
    worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
) -> None:
    ctx, factory, _ws_id, _worker_id = worker_ctx
    response = _client(ctx, factory, InMemoryStorage()).post(_path(ctx))

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "owners_only"


def response_text(files: dict[str, bytes]) -> str:
    return "\n".join(
        payload.decode("utf-8", errors="ignore") for payload in files.values()
    )
