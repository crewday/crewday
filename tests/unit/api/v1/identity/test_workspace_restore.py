"""Unit coverage for full workspace export artifact restore/import."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.assets.models import AssetDocument
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.workspace.models import Workspace
from app.services.workspace.export_service import build_workspace_export
from app.services.workspace.import_service import (
    WorkspaceImportError,
    restore_workspace_export,
)
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests._fakes.storage import InMemoryStorage
from tests.factories.identity import bootstrap_user, bootstrap_workspace

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _seed_document(
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


def _export(
    factory: sessionmaker[Session],
    *,
    ctx: WorkspaceContext,
    storage: InMemoryStorage,
) -> bytes:
    with factory() as session:
        return build_workspace_export(session, ctx, storage=storage).content


def _rewrite_manifest(
    content: bytes, mutate: Callable[[dict[str, Any]], None]
) -> bytes:
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(content)) as source,
        zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as dest,
    ):
        for name in source.namelist():
            payload = source.read(name)
            if name == "manifest.json":
                manifest = json.loads(payload)
                mutate(manifest)
                payload = json.dumps(manifest).encode("utf-8")
            dest.writestr(name, payload)
    return buffer.getvalue()


def _rewrite_table(
    content: bytes,
    table_name: str,
    mutate: Callable[[list[dict[str, Any]]], None],
) -> bytes:
    buffer = io.BytesIO()
    table_path = f"data/{table_name}.json"
    with (
        zipfile.ZipFile(io.BytesIO(content)) as source,
        zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as dest,
    ):
        for name in source.namelist():
            payload = source.read(name)
            if name == table_path:
                rows = json.loads(payload)
                mutate(rows)
                payload = json.dumps(rows).encode("utf-8")
            dest.writestr(name, payload)
    return buffer.getvalue()


def test_create_new_restores_workspace_rows_and_files(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, ws_id = owner_ctx
    storage = InMemoryStorage()
    payload = b"restore document"
    blob_hash = hashlib.sha256(payload).hexdigest()
    storage.put(blob_hash, io.BytesIO(payload), content_type="text/plain")
    with factory() as session:
        source_property_id = _seed_document(
            session, workspace_id=ws_id, blob_hash=blob_hash, title="restore-doc"
        )
        session.commit()

    import_storage = InMemoryStorage()
    with factory() as session:
        report = restore_workspace_export(
            session,
            _export(factory, ctx=ctx, storage=storage),
            mode="create_new",
            storage=import_storage,
        )
        session.commit()

    assert report.workspace_id != ws_id
    assert report.workspace_slug == "ws-identity-2"
    assert report.restored_files == 1
    with factory() as session:
        docs = session.scalars(
            select(AssetDocument).where(
                AssetDocument.workspace_id == report.workspace_id
            )
        ).all()
        properties = session.scalars(select(Property)).all()
    assert [doc.title for doc in docs] == ["restore-doc"]
    assert docs[0].property_id != source_property_id
    assert import_storage.get(blob_hash).read() == payload
    assert {prop.id for prop in properties} >= {docs[0].property_id, source_property_id}


def test_replace_atomically_replaces_target_workspace(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, ws_id = owner_ctx
    storage = InMemoryStorage()
    payload = b"source document"
    source_hash = hashlib.sha256(payload).hexdigest()
    storage.put(source_hash, io.BytesIO(payload), content_type="text/plain")
    with factory() as session:
        _seed_document(
            session, workspace_id=ws_id, blob_hash=source_hash, title="source-doc"
        )
        target_owner = bootstrap_user(
            session, email="target-owner@example.com", display_name="Target"
        )
        target = bootstrap_workspace(
            session,
            slug="restore-target",
            name="Restore Target",
            owner_user_id=target_owner.id,
        )
        old_hash = hashlib.sha256(b"old").hexdigest()
        _seed_document(
            session, workspace_id=target.id, blob_hash=old_hash, title="old-doc"
        )
        session.commit()
        target_id = target.id

    with factory() as session:
        report = restore_workspace_export(
            session,
            _export(factory, ctx=ctx, storage=storage),
            mode="replace",
            target_workspace_id=target_id,
            storage=InMemoryStorage(),
        )
        session.commit()

    assert report.workspace_id == target_id
    assert report.workspace_slug == "ws-identity-2"
    with factory() as session:
        target = session.get(Workspace, target_id)
        docs = session.scalars(
            select(AssetDocument).where(AssetDocument.workspace_id == target_id)
        ).all()
    assert target is not None
    assert target.name == "Identity WS"
    assert [doc.title for doc in docs] == ["source-doc"]


def test_invalid_manifest_is_rejected(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ws_id = owner_ctx
    content = _rewrite_manifest(
        _export(factory, ctx=ctx, storage=InMemoryStorage()),
        lambda manifest: manifest.update({"schema_version": 999}),
    )
    with (
        factory() as session,
        pytest.raises(WorkspaceImportError, match="schema_version"),
    ):
        restore_workspace_export(
            session,
            content,
            mode="create_new",
            storage=InMemoryStorage(),
        )


def test_workspace_row_must_match_manifest_workspace(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ws_id = owner_ctx
    content = _rewrite_table(
        _export(factory, ctx=ctx, storage=InMemoryStorage()),
        "workspace",
        lambda rows: rows[0].update({"id": "wrong-workspace"}),
    )

    with (
        factory() as session,
        pytest.raises(WorkspaceImportError, match="workspace row"),
    ):
        restore_workspace_export(
            session,
            content,
            mode="create_new",
            storage=InMemoryStorage(),
        )


def test_workspace_scoped_rows_must_belong_to_manifest_workspace(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ws_id = owner_ctx
    content = _rewrite_table(
        _export(factory, ctx=ctx, storage=InMemoryStorage()),
        "role_grant",
        lambda rows: rows[0].update({"workspace_id": None}),
    )

    with (
        factory() as session,
        pytest.raises(WorkspaceImportError, match="invalid workspace_id"),
    ):
        restore_workspace_export(
            session,
            content,
            mode="create_new",
            storage=InMemoryStorage(),
        )


def test_exports_with_missing_referenced_files_are_rejected(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, ws_id = owner_ctx
    missing_hash = hashlib.sha256(b"missing").hexdigest()
    with factory() as session:
        _seed_document(
            session,
            workspace_id=ws_id,
            blob_hash=missing_hash,
            title="missing-doc",
        )
        session.commit()

    content = _export(factory, ctx=ctx, storage=InMemoryStorage())

    with (
        factory() as session,
        pytest.raises(WorkspaceImportError, match="referenced files are missing"),
    ):
        restore_workspace_export(
            session,
            content,
            mode="create_new",
            storage=InMemoryStorage(),
        )


def test_unverified_permission_grants_are_skipped(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ws_id = owner_ctx
    content = _export(factory, ctx=ctx, storage=InMemoryStorage())
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(content)) as source,
        zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as dest,
    ):
        for name in source.namelist():
            payload = source.read(name)
            if name == "data/role_grant.json":
                rows = json.loads(payload)
                rows[0]["user_id"] = "missing-user"
                payload = json.dumps(rows).encode("utf-8")
            dest.writestr(name, payload)
    content = buffer.getvalue()

    with factory() as session:
        report = restore_workspace_export(
            session,
            content,
            mode="create_new",
            storage=InMemoryStorage(),
        )
        session.commit()

    assert [(skip.table, skip.user_id) for skip in report.skipped_permissions] == [
        ("role_grant", "missing-user")
    ]
    assert report.manual_follow_up_required is True
    with factory() as session:
        grants = session.scalars(
            select(RoleGrant).where(RoleGrant.workspace_id == report.workspace_id)
        ).all()
    assert grants == []


def test_replace_rolls_back_when_import_insert_fails(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ws_id = owner_ctx
    with factory() as session:
        target_owner = bootstrap_user(
            session, email="rollback-owner@example.com", display_name="Rollback"
        )
        target = bootstrap_workspace(
            session,
            slug="rollback-target",
            name="Rollback Target",
            owner_user_id=target_owner.id,
        )
        session.commit()
        target_id = target.id

    content = _export(factory, ctx=ctx, storage=InMemoryStorage())
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(content)) as source,
        zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as dest,
    ):
        for name in source.namelist():
            payload = source.read(name)
            if name == "data/workspace.json":
                rows = json.loads(payload)
                rows[0]["default_currency"] = "USDX"
                payload = json.dumps(rows).encode("utf-8")
            dest.writestr(name, payload)
    broken_content = buffer.getvalue()

    with factory() as session:
        with pytest.raises(IntegrityError):
            restore_workspace_export(
                session,
                broken_content,
                mode="replace",
                target_workspace_id=target_id,
                storage=InMemoryStorage(),
            )
        session.rollback()

    with factory() as session:
        target = session.get(Workspace, target_id)
    assert target is not None
    assert target.name == "Rollback Target"
