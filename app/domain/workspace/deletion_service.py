"""Workspace deletion scheduling and purge service."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import Table, event, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.adapters.db.base import Base
from app.adapters.db.workspace.models import Workspace
from app.adapters.storage.ports import Storage
from app.tenancy import tenant_agnostic
from app.util.clock import Clock, aware_utc

__all__ = ["WorkspacePurgeReport", "purge_due_workspaces"]

_log = logging.getLogger(__name__)
_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PENDING_BLOB_DELETE_KEY = "workspace_purge_pending_blob_delete"
_BLOB_DELETE_LISTENER_KEY = "workspace_purge_blob_delete_listener"


@dataclass(frozen=True, slots=True)
class WorkspacePurgeReport:
    purged: int
    workspace_ids: tuple[str, ...]
    deleted_blob_hashes: tuple[str, ...]


def purge_due_workspaces(
    session: Session,
    *,
    storage: Storage,
    clock: Clock,
    limit: int | None = None,
) -> WorkspacePurgeReport:
    """Hard-delete workspaces whose owner-delete deadline has elapsed."""
    now = aware_utc(clock.now())
    # justification: deployment purge worker; Workspace is the tenant root (no
    # workspace_id); blob-ref scans span the whole install by design.
    with tenant_agnostic():
        stmt = (
            select(Workspace)
            .where(Workspace.purge_after.is_not(None))
            .where(Workspace.purge_after <= now)
            .order_by(Workspace.purge_after.asc(), Workspace.id.asc())
        )
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be positive")
            stmt = stmt.limit(limit)
        workspaces = tuple(session.scalars(stmt).all())
        if not workspaces:
            return WorkspacePurgeReport(
                purged=0, workspace_ids=(), deleted_blob_hashes=()
            )

        workspace_ids = tuple(workspace.id for workspace in workspaces)
        candidate_blob_hashes = _collect_workspace_blob_hashes(
            session, workspace_ids=workspace_ids
        )
        for workspace in workspaces:
            session.delete(workspace)
        session.flush()

        still_referenced = _collect_existing_blob_hashes(
            session,
            candidate_blob_hashes=candidate_blob_hashes,
        )

    deletable_blob_hashes = tuple(sorted(candidate_blob_hashes - still_referenced))
    if deletable_blob_hashes:
        _schedule_blob_delete_after_commit(
            session,
            storage=storage,
            blob_hashes=deletable_blob_hashes,
        )
    _log.info(
        "workspace purge completed",
        extra={
            "event": "workspace.purge",
            "purged": len(workspace_ids),
            "workspace_ids": workspace_ids,
            "deleted_blobs": len(deletable_blob_hashes),
        },
    )
    return WorkspacePurgeReport(
        purged=len(workspace_ids),
        workspace_ids=workspace_ids,
        deleted_blob_hashes=deletable_blob_hashes,
    )


def _schedule_blob_delete_after_commit(
    session: Session, *, storage: Storage, blob_hashes: tuple[str, ...]
) -> None:
    pending = session.info.setdefault(_PENDING_BLOB_DELETE_KEY, [])
    pending.append((storage, blob_hashes))
    if session.info.get(_BLOB_DELETE_LISTENER_KEY):
        return
    event.listen(session, "after_commit", _delete_pending_blobs_after_commit, once=True)
    event.listen(
        session, "after_rollback", _discard_pending_blobs_after_rollback, once=True
    )
    session.info[_BLOB_DELETE_LISTENER_KEY] = True


def _delete_pending_blobs_after_commit(session: Session) -> None:
    pending = session.info.pop(_PENDING_BLOB_DELETE_KEY, [])
    session.info.pop(_BLOB_DELETE_LISTENER_KEY, None)
    for storage, blob_hashes in pending:
        _delete_blobs_after_commit(storage, blob_hashes=blob_hashes)


def _discard_pending_blobs_after_rollback(session: Session) -> None:
    session.info.pop(_PENDING_BLOB_DELETE_KEY, None)
    session.info.pop(_BLOB_DELETE_LISTENER_KEY, None)


def _delete_blobs_after_commit(
    storage: Storage, *, blob_hashes: tuple[str, ...]
) -> None:
    for blob_hash in blob_hashes:
        storage.delete(blob_hash)


def _collect_workspace_blob_hashes(
    session: Session, *, workspace_ids: tuple[str, ...]
) -> set[str]:
    refs: set[str] = set()
    if not workspace_ids:
        return refs
    for table in Base.metadata.sorted_tables:
        if "workspace_id" not in table.c:
            continue
        blob_columns = _blob_columns(table)
        if not blob_columns:
            continue
        rows = session.execute(
            select(*blob_columns).where(table.c.workspace_id.in_(workspace_ids))
        ).mappings()
        for row in rows:
            for value in row.values():
                refs.update(_hashes_from_value(value))
    return refs


def _collect_existing_blob_hashes(
    session: Session, *, candidate_blob_hashes: set[str]
) -> set[str]:
    if not candidate_blob_hashes:
        return set()
    refs: set[str] = set()
    for table in Base.metadata.sorted_tables:
        blob_columns = _blob_columns(table)
        if not blob_columns:
            continue
        rows = session.execute(select(*blob_columns)).mappings()
        for row in rows:
            for value in row.values():
                refs.update(_hashes_from_value(value) & candidate_blob_hashes)
            if refs == candidate_blob_hashes:
                return refs
    return refs


def _blob_columns(table: Table) -> list[ColumnElement[object]]:
    return [
        column
        for column in table.c
        if column.name.endswith("blob_hash")
        or column.name.endswith("blob_hashes")
        or column.name == "attachments_json"
    ]


def _hashes_from_value(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        if _HEX_SHA256_RE.fullmatch(value):
            found.add(value)
    elif isinstance(value, list):
        for item in value:
            found.update(_hashes_from_value(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.update(_hashes_from_value(item))
    return found
