"""Owner workspace deletion scheduling and purge tests."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.assets.models import AssetDocument
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.workspace.models import Workspace
from app.api.v1.admin import router as admin_router
from app.domain.workspace.deletion_service import purge_due_workspaces
from app.tenancy import tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests._fakes.storage import InMemoryStorage
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client

_PINNED = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client(
        [(f"/w/{ctx.workspace_slug}/api/v1/admin", admin_router)],
        factory,
        ctx,
    )


def _path(slug: str) -> str:
    return f"/w/{slug}/api/v1/admin/workspace/delete"


def _seed_workspace(
    session: Session,
    *,
    slug: str,
    owner_email: str,
) -> tuple[Workspace, str]:
    owner = bootstrap_user(session, email=owner_email, display_name=owner_email)
    workspace = bootstrap_workspace(
        session,
        slug=slug,
        name=slug,
        owner_user_id=owner.id,
    )
    return workspace, owner.id


def _seed_document(
    session: Session,
    *,
    workspace_id: str,
    blob_hash: str,
    title: str,
) -> None:
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


def test_owner_delete_archives_and_schedules_fourteen_day_purge(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx

    response = _client(ctx, factory).post(_path(ctx.workspace_slug))

    assert response.status_code == 200, response.text
    body = response.json()
    requested_at = datetime.fromisoformat(body["delete_requested_at"])
    purge_after = datetime.fromisoformat(body["purge_after"])
    assert body["id"] == workspace_id
    assert purge_after - requested_at == timedelta(days=14)

    with factory() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.archived_at is not None
        assert workspace.delete_requested_at == requested_at
        assert workspace.purge_after == purge_after
        actions = set(session.scalars(select(AuditLog.action)).all())
        assert "workspace.archived" in actions
        assert "workspace.delete_requested" in actions


def test_owner_delete_is_idempotent_and_does_not_extend_deadline(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    client = _client(ctx, factory)

    first = client.post(_path(ctx.workspace_slug))
    second = client.post(_path(ctx.workspace_slug))

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["delete_requested_at"] == first.json()["delete_requested_at"]
    assert second.json()["purge_after"] == first.json()["purge_after"]

    with factory() as session:
        delete_audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "workspace.delete_requested")
        ).all()
        assert len(delete_audits) == 1


def test_owner_delete_repairs_partial_schedule_without_extending_deadline(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    original_requested_at = _PINNED - timedelta(days=3)
    with factory() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.archived_at = original_requested_at
        workspace.delete_requested_at = original_requested_at
        workspace.purge_after = None
        session.commit()

    response = _client(ctx, factory).post(_path(ctx.workspace_slug))

    assert response.status_code == 200, response.text
    body = response.json()
    expected_purge_after = original_requested_at + timedelta(days=14)
    assert datetime.fromisoformat(body["delete_requested_at"]) == original_requested_at
    assert datetime.fromisoformat(body["purge_after"]) == expected_purge_after
    with factory() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.delete_requested_at == original_requested_at
        assert workspace.purge_after == expected_purge_after


def test_non_owner_cannot_schedule_workspace_deletion(
    worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
) -> None:
    ctx, factory, workspace_id, _worker_id = worker_ctx

    response = _client(ctx, factory).post(_path(ctx.workspace_slug))

    assert response.status_code == 403
    with factory() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.delete_requested_at is None
        assert workspace.purge_after is None


def test_purge_deletes_only_due_workspaces_and_unshared_blobs(
    factory: sessionmaker[Session],
) -> None:
    storage = InMemoryStorage()
    due_hash = "a" * 64
    shared_hash = "b" * 64
    future_hash = "c" * 64
    for blob_hash in (due_hash, shared_hash, future_hash):
        storage.put(
            blob_hash, io.BytesIO(blob_hash.encode()), content_type="text/plain"
        )

    with factory() as session:
        due_ws, _ = _seed_workspace(
            session, slug="due-delete", owner_email="due@example.com"
        )
        future_ws, _ = _seed_workspace(
            session, slug="future-delete", owner_email="future@example.com"
        )
        other_ws, _ = _seed_workspace(
            session, slug="other-delete", owner_email="other@example.com"
        )
        due_ws.archived_at = _PINNED - timedelta(days=15)
        due_ws.delete_requested_at = _PINNED - timedelta(days=15)
        due_ws.purge_after = _PINNED - timedelta(days=1)
        future_ws.archived_at = _PINNED
        future_ws.delete_requested_at = _PINNED
        future_ws.purge_after = _PINNED + timedelta(days=14)
        _seed_document(
            session, workspace_id=due_ws.id, blob_hash=due_hash, title="due-doc"
        )
        _seed_document(
            session, workspace_id=due_ws.id, blob_hash=shared_hash, title="shared-due"
        )
        _seed_document(
            session,
            workspace_id=future_ws.id,
            blob_hash=future_hash,
            title="future-doc",
        )
        _seed_document(
            session,
            workspace_id=other_ws.id,
            blob_hash=shared_hash,
            title="shared-other",
        )
        session.commit()

    with factory() as session:
        report = purge_due_workspaces(
            session,
            storage=storage,
            clock=FrozenClock(_PINNED),
        )
        assert storage.exists(due_hash)
        session.commit()

    assert report.purged == 1
    with factory() as session, tenant_agnostic():
        slugs = set(session.scalars(select(Workspace.slug)).all())
    assert "due-delete" not in slugs
    assert "future-delete" in slugs
    assert "other-delete" in slugs
    assert not storage.exists(due_hash)
    assert storage.exists(shared_hash)
    assert storage.exists(future_hash)


def test_purge_rollback_does_not_delete_blobs_on_later_commit(
    factory: sessionmaker[Session],
) -> None:
    storage = InMemoryStorage()
    blob_hash = "d" * 64
    storage.put(blob_hash, io.BytesIO(b"due"), content_type="text/plain")

    with factory() as session:
        due_ws, _ = _seed_workspace(
            session, slug="rollback-delete", owner_email="rollback@example.com"
        )
        due_ws.archived_at = _PINNED - timedelta(days=15)
        due_ws.delete_requested_at = _PINNED - timedelta(days=15)
        due_ws.purge_after = _PINNED - timedelta(days=1)
        _seed_document(
            session, workspace_id=due_ws.id, blob_hash=blob_hash, title="rollback-doc"
        )
        session.commit()

    with factory() as session:
        report = purge_due_workspaces(
            session,
            storage=storage,
            clock=FrozenClock(_PINNED),
        )
        assert report.purged == 1
        session.rollback()
        assert storage.exists(blob_hash)
        session.commit()

    assert storage.exists(blob_hash)
    with factory() as session, tenant_agnostic():
        slugs = set(session.scalars(select(Workspace.slug)).all())
    assert "rollback-delete" in slugs
