"""Workspace deletion purge worker tick."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.adapters.db.session import make_uow
from app.adapters.storage.ports import Storage
from app.api.factory import _build_storage
from app.config import get_settings
from app.domain.workspace.deletion_service import (
    WorkspacePurgeReport,
    purge_due_workspaces,
)
from app.util.clock import Clock, SystemClock

__all__ = ["purge_due_workspace_deletions"]

_log = logging.getLogger(__name__)


def purge_due_workspace_deletions(
    *,
    clock: Clock | None = None,
    storage: Storage | None = None,
    limit: int | None = None,
    require_storage: bool = False,
) -> WorkspacePurgeReport:
    """Purge workspaces whose owner-delete grace period has elapsed."""
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_storage = (
        storage if storage is not None else _build_storage(get_settings())
    )
    if resolved_storage is None:
        if require_storage:
            raise RuntimeError("storage backend is unavailable")
        _log.warning(
            "workspace purge tick: storage backend unavailable; skipping",
            extra={"event": "workspace.purge.skipped_no_storage"},
        )
        return WorkspacePurgeReport(purged=0, workspace_ids=(), deleted_blob_hashes=())

    with make_uow() as session:
        assert isinstance(session, Session)
        return purge_due_workspaces(
            session,
            storage=resolved_storage,
            clock=resolved_clock,
            limit=limit,
        )
