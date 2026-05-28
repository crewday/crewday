"""Shared workspace search + cursor pagination for admin lists."""

from __future__ import annotations

import hashlib

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.adapters.db.workspace.models import Workspace
from app.api.pagination import CursorPage, decode_cursor, paginate
from app.domain.errors import InvalidCursor
from app.tenancy import tenant_agnostic

_CURSOR_PREFIX = "admin-workspaces"


def normalize_workspace_search(q: str | None) -> str | None:
    """Return a stripped search term, or ``None`` for no filter."""
    if q is None:
        return None
    normalized = q.strip()
    return normalized or None


def _matches_workspace_search(workspace: Workspace, q: str | None) -> bool:
    if q is None:
        return True
    needle = q.casefold()
    return needle in workspace.name.casefold() or needle in workspace.slug.casefold()


def _workspace_cursor_scope(q: str | None) -> str:
    normalized = "" if q is None else q.casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _workspace_cursor_key(workspace: Workspace, q: str | None) -> str:
    return f"{_CURSOR_PREFIX}:{_workspace_cursor_scope(q)}:{workspace.id}"


def _decode_workspace_cursor(
    cursor: str | None, q: str | None
) -> tuple[str | None, bool]:
    raw = decode_cursor(cursor)
    if raw is None:
        return None, True

    try:
        prefix, scope, workspace_id = raw.split(":", 2)
    except ValueError as exc:
        raise InvalidCursor("cursor is invalid for workspace list") from exc
    if prefix != _CURSOR_PREFIX or workspace_id == "":
        raise InvalidCursor("cursor is invalid for workspace list")
    return workspace_id, scope == _workspace_cursor_scope(q)


def _workspace_search_statement(q: str | None) -> Select[tuple[Workspace]]:
    stmt = select(Workspace)
    if q is None:
        return stmt
    pattern = f"%{q}%"
    return stmt.where(or_(Workspace.name.ilike(pattern), Workspace.slug.ilike(pattern)))


def list_workspace_page(
    session: Session,
    *,
    q: str | None,
    cursor: str | None,
    limit: int,
) -> CursorPage[Workspace]:
    """Return a cursor-paginated page of workspaces ordered oldest-first."""
    search = normalize_workspace_search(q)
    after_id, cursor_scope_matches = _decode_workspace_cursor(cursor, search)
    if not cursor_scope_matches:
        return CursorPage(items=(), next_cursor=None, has_more=False)
    stmt = _workspace_search_statement(search)
    if after_id is not None:
        with tenant_agnostic():
            cursor_workspace = session.get(Workspace, after_id)
        if cursor_workspace is None or not _matches_workspace_search(
            cursor_workspace, search
        ):
            return CursorPage(items=(), next_cursor=None, has_more=False)
        stmt = stmt.where(
            (Workspace.created_at > cursor_workspace.created_at)
            | (
                (Workspace.created_at == cursor_workspace.created_at)
                & (Workspace.id > cursor_workspace.id)
            )
        )
    stmt = stmt.order_by(Workspace.created_at.asc(), Workspace.id.asc()).limit(
        limit + 1
    )
    with tenant_agnostic():
        rows = tuple(session.scalars(stmt).all())
    return paginate(
        rows,
        limit=limit,
        key_getter=lambda workspace: _workspace_cursor_key(workspace, search),
    )
