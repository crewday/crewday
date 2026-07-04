"""Workspace export artifact restore/import service."""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final, Literal

from sqlalchemy import Table, delete, func, select
from sqlalchemy.orm import Session

from app.adapters.db.base import Base
from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import Workspace
from app.adapters.storage.ports import Storage
from app.domain.workspace.export_service import (
    WORKSPACE_EXPORT_SCHEMA_VERSION,
    WORKSPACE_EXPORT_TABLES,
)
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid

__all__ = [
    "SkippedPermission",
    "WorkspaceImportError",
    "WorkspaceImportReport",
    "restore_workspace_export",
]


WorkspaceImportMode = Literal["create_new", "replace"]

_MANIFEST_PATH: Final[str] = "manifest.json"
_FILES_PREFIX: Final[str] = "files/"
_HEX_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_SENSITIVE_TABLES: Final[frozenset[str]] = frozenset(
    (
        "permission_group_member",
        "role_grant",
        "user_workspace",
    )
)
_USER_REFERENCE_COLUMNS: Final[frozenset[str]] = frozenset(
    (
        "added_by_user_id",
        "created_by_user_id",
        "revoked_by_user_id",
        "user_id",
    )
)


class WorkspaceImportError(ValueError):
    """Raised when a workspace export artifact is invalid or cannot restore."""


@dataclass(frozen=True, slots=True)
class SkippedPermission:
    table: str
    reason: str
    row_id: str | None
    user_id: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceImportReport:
    mode: WorkspaceImportMode
    workspace_id: str
    workspace_slug: str
    source_workspace_id: str
    source_workspace_slug: str
    restored_tables: dict[str, int]
    restored_files: int
    skipped_permissions: tuple[SkippedPermission, ...]
    manual_follow_up_required: bool


@dataclass(frozen=True, slots=True)
class _Artifact:
    manifest: dict[str, Any]
    rows: dict[str, list[dict[str, Any]]]
    files: dict[str, bytes]


def restore_workspace_export(
    session: Session,
    artifact_content: bytes,
    *,
    mode: WorkspaceImportMode,
    storage: Storage,
    target_workspace_id: str | None = None,
) -> WorkspaceImportReport:
    """Restore a full workspace export artifact.

    ``create_new`` always allocates a new workspace id. ``replace`` uses
    ``target_workspace_id`` and replaces rows for that workspace inside one
    database savepoint.
    """

    artifact = _read_artifact(artifact_content)
    source_workspace = _manifest_workspace(artifact.manifest)
    source_workspace_id = source_workspace["id"]
    source_workspace_slug = source_workspace["slug"]

    # justification: workspace import writes/replaces rows keyed to an explicit
    # target workspace_id; runs before the imported workspace has an ambient context.
    with tenant_agnostic():
        if mode == "replace":
            if target_workspace_id is None:
                raise WorkspaceImportError("replace mode requires target_workspace_id")
            target_workspace = session.get(Workspace, target_workspace_id)
            if target_workspace is None:
                raise WorkspaceImportError("target workspace does not exist")
            workspace_id = target_workspace_id
            fallback_slug = target_workspace.slug
        elif mode == "create_new":
            if target_workspace_id is not None:
                raise WorkspaceImportError(
                    "create_new mode does not accept target_workspace_id"
                )
            workspace_id = new_ulid()
            fallback_slug = source_workspace_slug
        else:
            raise WorkspaceImportError(f"unsupported workspace import mode: {mode}")

        workspace_slug = _available_slug(
            session,
            preferred=source_workspace_slug,
            fallback=fallback_slug,
            excluding_workspace_id=workspace_id if mode == "replace" else None,
        )
        id_map = _build_id_map(artifact.rows, workspace_id=workspace_id)
        rows = _remapped_rows(
            artifact.rows,
            id_map=id_map,
            workspace_id=workspace_id,
            workspace_slug=workspace_slug,
        )
        rows, skipped_permissions = _skip_unverified_permissions(session, rows)
        file_payloads = _verified_file_payloads(artifact)

        with session.begin_nested():
            if mode == "replace":
                _delete_workspace_rows(session, workspace_id=workspace_id)
            restored_tables = _insert_rows(session, rows)
            for content_hash, payload in file_payloads.items():
                storage.put(content_hash, io.BytesIO(payload))

    return WorkspaceImportReport(
        mode=mode,
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        source_workspace_id=source_workspace_id,
        source_workspace_slug=source_workspace_slug,
        restored_tables=restored_tables,
        restored_files=len(file_payloads),
        skipped_permissions=tuple(skipped_permissions),
        manual_follow_up_required=bool(skipped_permissions),
    )


def _read_artifact(content: bytes) -> _Artifact:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            archive_names = archive.namelist()
            names = set(archive_names)
            if len(names) != len(archive_names):
                raise WorkspaceImportError(
                    "workspace export artifact contains duplicate entries"
                )
            if _MANIFEST_PATH not in names:
                raise WorkspaceImportError("workspace export manifest is missing")
            manifest = json.loads(archive.read(_MANIFEST_PATH))
            if not isinstance(manifest, dict):
                raise WorkspaceImportError("workspace export manifest is malformed")
            table_paths = _validate_manifest(manifest)
            rows = {
                table_name: _read_table_rows(archive, path)
                for table_name, path in table_paths.items()
            }
            _validate_row_counts(manifest, rows)
            files = _read_files(archive, manifest)
            _validate_archive_entries(names, table_paths=table_paths, files=files)
            _validate_source_workspace_rows(
                rows,
                source_workspace_id=_manifest_workspace(manifest)["id"],
            )
    except zipfile.BadZipFile as exc:
        raise WorkspaceImportError("workspace export artifact must be a ZIP") from exc
    except json.JSONDecodeError as exc:
        raise WorkspaceImportError("workspace export JSON is malformed") from exc
    return _Artifact(manifest=manifest, rows=rows, files=files)


def _validate_manifest(manifest: Mapping[str, Any]) -> dict[str, str]:
    if manifest.get("schema_version") != WORKSPACE_EXPORT_SCHEMA_VERSION:
        raise WorkspaceImportError("unsupported workspace export schema_version")
    _manifest_workspace(manifest)
    tables_raw = manifest.get("tables")
    if not isinstance(tables_raw, list):
        raise WorkspaceImportError("workspace export manifest tables are malformed")
    table_paths: dict[str, str] = {}
    for entry in tables_raw:
        if not isinstance(entry, dict):
            raise WorkspaceImportError("workspace export manifest table is malformed")
        name = entry.get("name")
        path = entry.get("path")
        row_count = entry.get("row_count")
        if (
            not isinstance(name, str)
            or not isinstance(path, str)
            or not isinstance(row_count, int)
        ):
            raise WorkspaceImportError("workspace export manifest table is malformed")
        if name in table_paths:
            raise WorkspaceImportError("workspace export manifest table set is invalid")
        table_paths[name] = path
    if set(table_paths) != set(WORKSPACE_EXPORT_TABLES):
        raise WorkspaceImportError("workspace export manifest table set is invalid")
    for table_name in WORKSPACE_EXPORT_TABLES:
        if table_paths[table_name] != f"data/{table_name}.json":
            raise WorkspaceImportError(
                "workspace export manifest table path is invalid"
            )
    return table_paths


def _manifest_workspace(manifest: Mapping[str, Any]) -> dict[str, str]:
    workspace = manifest.get("workspace")
    if not isinstance(workspace, dict):
        raise WorkspaceImportError("workspace export manifest workspace is malformed")
    workspace_id = workspace.get("id")
    slug = workspace.get("slug")
    name = workspace.get("name")
    if not isinstance(workspace_id, str) or not isinstance(slug, str):
        raise WorkspaceImportError("workspace export manifest workspace is malformed")
    if not isinstance(name, str):
        raise WorkspaceImportError("workspace export manifest workspace is malformed")
    return {"id": workspace_id, "slug": slug, "name": name}


def _read_table_rows(archive: zipfile.ZipFile, path: str) -> list[dict[str, Any]]:
    try:
        raw = json.loads(archive.read(path))
    except KeyError as exc:
        raise WorkspaceImportError(
            f"workspace export table is missing: {path}"
        ) from exc
    if not isinstance(raw, list) or not all(isinstance(row, dict) for row in raw):
        raise WorkspaceImportError(f"workspace export table is malformed: {path}")
    return [dict(row) for row in raw]


def _validate_row_counts(
    manifest: Mapping[str, Any], rows: Mapping[str, list[dict[str, Any]]]
) -> None:
    tables = manifest["tables"]
    assert isinstance(tables, list)
    for entry in tables:
        assert isinstance(entry, dict)
        name = str(entry["name"])
        if len(rows[name]) != entry["row_count"]:
            raise WorkspaceImportError(
                f"workspace export row_count mismatch for table: {name}"
            )


def _read_files(
    archive: zipfile.ZipFile, manifest: Mapping[str, Any]
) -> dict[str, bytes]:
    files_raw = manifest.get("files")
    missing_files_raw = manifest.get("missing_files")
    if not isinstance(files_raw, list) or not isinstance(missing_files_raw, list):
        raise WorkspaceImportError("workspace export file manifest is malformed")
    _validate_missing_files(missing_files_raw)
    if missing_files_raw:
        raise WorkspaceImportError(
            "workspace export cannot be restored because referenced files are missing"
        )
    files: dict[str, bytes] = {}
    file_paths: set[str] = set()
    for entry in files_raw:
        if not isinstance(entry, dict):
            raise WorkspaceImportError("workspace export file entry is malformed")
        content_hash = entry.get("content_hash")
        path = entry.get("path")
        size_bytes = entry.get("size_bytes")
        if (
            not isinstance(content_hash, str)
            or not _HEX_SHA256_RE.fullmatch(content_hash)
            or not isinstance(path, str)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
        ):
            raise WorkspaceImportError("workspace export file entry is malformed")
        if path != f"{_FILES_PREFIX}{content_hash[:2]}/{content_hash}":
            raise WorkspaceImportError("workspace export file path is invalid")
        if content_hash in files or path in file_paths:
            raise WorkspaceImportError("workspace export file manifest is invalid")
        try:
            payload = archive.read(path)
        except KeyError as exc:
            raise WorkspaceImportError(
                f"workspace export file is missing: {path}"
            ) from exc
        if len(payload) != size_bytes:
            raise WorkspaceImportError("workspace export file size mismatch")
        if hashlib.sha256(payload).hexdigest() != content_hash:
            raise WorkspaceImportError("workspace export file content hash mismatch")
        files[content_hash] = payload
        file_paths.add(path)
    return files


def _validate_missing_files(entries: list[Any]) -> None:
    for entry in entries:
        if not isinstance(entry, dict):
            raise WorkspaceImportError(
                "workspace export missing file entry is malformed"
            )
        content_hash = entry.get("content_hash")
        if not isinstance(content_hash, str) or not _HEX_SHA256_RE.fullmatch(
            content_hash
        ):
            raise WorkspaceImportError(
                "workspace export missing file entry is malformed"
            )


def _validate_archive_entries(
    names: set[str],
    *,
    table_paths: Mapping[str, str],
    files: Mapping[str, bytes],
) -> None:
    expected_names = {
        _MANIFEST_PATH,
        *table_paths.values(),
        *(
            f"{_FILES_PREFIX}{content_hash[:2]}/{content_hash}"
            for content_hash in files
        ),
    }
    if names != expected_names:
        raise WorkspaceImportError("workspace export artifact entries are invalid")


def _validate_source_workspace_rows(
    rows: Mapping[str, list[dict[str, Any]]], *, source_workspace_id: str
) -> None:
    workspace_rows = rows["workspace"]
    if len(workspace_rows) != 1 or workspace_rows[0].get("id") != source_workspace_id:
        raise WorkspaceImportError(
            "workspace export workspace row does not match manifest"
        )
    for table_name in WORKSPACE_EXPORT_TABLES:
        if table_name == "workspace":
            continue
        table = Base.metadata.tables[table_name]
        if "workspace_id" not in table.c:
            continue
        for row in rows[table_name]:
            if row.get("workspace_id") != source_workspace_id:
                raise WorkspaceImportError(
                    f"workspace export row has invalid workspace_id: {table_name}"
                )


def _build_id_map(
    rows: Mapping[str, list[dict[str, Any]]], *, workspace_id: str
) -> dict[str, str]:
    id_map: dict[str, str] = {}
    workspace_rows = rows["workspace"]
    if len(workspace_rows) != 1 or not isinstance(workspace_rows[0].get("id"), str):
        raise WorkspaceImportError(
            "workspace export must contain exactly one workspace"
        )
    id_map[workspace_rows[0]["id"]] = workspace_id

    for table_name, table_rows in rows.items():
        if table_name == "workspace":
            continue
        table = Base.metadata.tables[table_name]
        primary_key = tuple(table.primary_key.columns)
        if len(primary_key) != 1 or primary_key[0].name != "id":
            continue
        for row in table_rows:
            old_id = row.get("id")
            if isinstance(old_id, str):
                id_map[old_id] = new_ulid()
    return id_map


def _remapped_rows(
    rows: Mapping[str, list[dict[str, Any]]],
    *,
    id_map: Mapping[str, str],
    workspace_id: str,
    workspace_slug: str,
) -> dict[str, list[dict[str, Any]]]:
    remapped: dict[str, list[dict[str, Any]]] = {}
    for table_name in WORKSPACE_EXPORT_TABLES:
        table = Base.metadata.tables[table_name]
        table_rows: list[dict[str, Any]] = []
        for row in rows[table_name]:
            next_row = {
                key: _remap_value(value, id_map)
                for key, value in row.items()
                if key in table.c
            }
            if "workspace_id" in table.c and next_row.get("workspace_id") is not None:
                next_row["workspace_id"] = workspace_id
            if table_name == "workspace":
                next_row["id"] = workspace_id
                next_row["slug"] = workspace_slug
            table_rows.append(_coerce_row(table, next_row))
        remapped[table_name] = table_rows
    return remapped


def _remap_value(value: Any, id_map: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return id_map.get(value, value)
    if isinstance(value, list):
        return [_remap_value(item, id_map) for item in value]
    if isinstance(value, dict):
        return {key: _remap_value(item, id_map) for key, item in value.items()}
    return value


def _coerce_row(table: Table, row: Mapping[str, Any]) -> dict[str, Any]:
    coerced: dict[str, Any] = {}
    for key, value in row.items():
        column = table.c[key]
        coerced[key] = _coerce_value(column.type, value)
    return coerced


def _coerce_value(column_type: Any, value: Any) -> Any:
    if value is None:
        return None
    try:
        python_type = column_type.python_type
    except NotImplementedError:
        return value
    if python_type is datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    if python_type is date:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value)
    if python_type is Decimal and isinstance(value, str):
        return Decimal(value)
    if python_type is bytes and isinstance(value, str):
        return bytes.fromhex(value)
    return value


def _skip_unverified_permissions(
    session: Session, rows: Mapping[str, list[dict[str, Any]]]
) -> tuple[dict[str, list[dict[str, Any]]], list[SkippedPermission]]:
    user_ids = _user_ids_referenced_by_identity_sensitive_rows(rows)
    existing_user_ids = set(
        session.scalars(select(User.id).where(User.id.in_(sorted(user_ids)))).all()
    )
    skipped: list[SkippedPermission] = []
    filtered = {table_name: list(table_rows) for table_name, table_rows in rows.items()}
    for table_name in _IDENTITY_SENSITIVE_TABLES:
        kept: list[dict[str, Any]] = []
        for row in rows[table_name]:
            missing_user_id = _missing_user_reference(row, existing_user_ids)
            if missing_user_id is None:
                kept.append(row)
                continue
            skipped.append(
                SkippedPermission(
                    table=table_name,
                    reason="unverified_identity",
                    row_id=str(row.get("id")) if row.get("id") is not None else None,
                    user_id=missing_user_id,
                )
            )
        filtered[table_name] = kept
    return filtered, skipped


def _user_ids_referenced_by_identity_sensitive_rows(
    rows: Mapping[str, list[dict[str, Any]]],
) -> set[str]:
    user_ids: set[str] = set()
    for table_name in _IDENTITY_SENSITIVE_TABLES:
        for row in rows[table_name]:
            for column in _USER_REFERENCE_COLUMNS:
                value = row.get(column)
                if isinstance(value, str):
                    user_ids.add(value)
    return user_ids


def _missing_user_reference(
    row: Mapping[str, Any], existing_user_ids: set[str]
) -> str | None:
    for column in _USER_REFERENCE_COLUMNS:
        value = row.get(column)
        if value is not None and value not in existing_user_ids:
            return str(value)
    return None


def _verified_file_payloads(artifact: _Artifact) -> dict[str, bytes]:
    return dict(artifact.files)


def _delete_workspace_rows(session: Session, *, workspace_id: str) -> None:
    property_table = Base.metadata.tables["property_workspace"]
    owned_property_ids = tuple(
        session.scalars(
            select(property_table.c.property_id)
            .where(property_table.c.workspace_id == workspace_id)
            .where(property_table.c.membership_role == "owner_workspace")
        ).all()
    )
    for table in reversed(Base.metadata.sorted_tables):
        if table.name not in WORKSPACE_EXPORT_TABLES:
            continue
        if table.name == "workspace":
            session.execute(delete(table).where(table.c.id == workspace_id))
        elif table.name == "property":
            if owned_property_ids:
                session.execute(delete(table).where(table.c.id.in_(owned_property_ids)))
        elif "workspace_id" in table.c:
            session.execute(delete(table).where(table.c.workspace_id == workspace_id))


def _insert_rows(
    session: Session, rows: Mapping[str, list[dict[str, Any]]]
) -> dict[str, int]:
    restored: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        if table.name not in WORKSPACE_EXPORT_TABLES:
            continue
        table_rows = rows[table.name]
        if table_rows:
            session.execute(table.insert(), table_rows)
        restored[table.name] = len(table_rows)
    return restored


def _available_slug(
    session: Session,
    *,
    preferred: str,
    fallback: str,
    excluding_workspace_id: str | None,
) -> str:
    base = _clean_slug(preferred) or _clean_slug(fallback) or "workspace"
    candidate = base
    suffix = 2
    while _slug_exists(
        session, slug=candidate, excluding_workspace_id=excluding_workspace_id
    ):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _clean_slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return re.sub(r"-{2,}", "-", cleaned)


def _slug_exists(
    session: Session, *, slug: str, excluding_workspace_id: str | None
) -> bool:
    stmt = select(func.count()).select_from(Workspace).where(Workspace.slug == slug)
    if excluding_workspace_id is not None:
        stmt = stmt.where(Workspace.id != excluding_workspace_id)
    return session.scalar(stmt) != 0
