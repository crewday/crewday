"""HTTP tests for owner workspace archive."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.workspace.models import Workspace
from app.api.v1.admin import router as admin_router
from app.auth import session as auth_session
from app.tenancy import WorkspaceContext
from tests.unit.api.v1.identity.conftest import build_client

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client(
        [(f"/w/{ctx.workspace_slug}/api/v1/admin", admin_router)],
        factory,
        ctx,
    )


def _path(ctx: WorkspaceContext) -> str:
    return f"/w/{ctx.workspace_slug}/api/v1/admin/workspace/archive"


def test_owner_archives_workspace_and_writes_audit(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, ws_id = owner_ctx

    response = _client(ctx, factory).post(_path(ctx))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == ws_id
    assert body["archived_at"].endswith("+00:00")

    with factory() as session:
        workspace = session.get(Workspace, ws_id)
        assert workspace is not None
        assert workspace.archived_at is not None
        audit = session.scalar(
            select(AuditLog).where(AuditLog.action == "workspace.archived")
        )
        assert audit is not None
        assert audit.workspace_id == ws_id
        assert audit.actor_id == ctx.actor_id
        assert audit.diff == {"archived_at": body["archived_at"]}


def test_archive_is_idempotent_and_keeps_original_timestamp(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, ws_id = owner_ctx
    original = _PINNED - timedelta(days=1)
    with factory() as session:
        workspace = session.get(Workspace, ws_id)
        assert workspace is not None
        workspace.archived_at = original
        session.commit()

    response = _client(ctx, factory).post(_path(ctx))

    assert response.status_code == 200, response.text
    assert response.json()["archived_at"] == original.isoformat()
    with factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "workspace.archived")
        )
        assert count == 0


def test_non_owner_cannot_archive_workspace(
    worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
) -> None:
    ctx, factory, ws_id, _worker_id = worker_ctx

    response = _client(ctx, factory).post(_path(ctx))

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "owners_only"
    with factory() as session:
        workspace = session.get(Workspace, ws_id)
        assert workspace is not None
        assert workspace.archived_at is None


def test_archive_clears_current_session_workspace(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, ws_id = owner_ctx
    cookie = "archive-current-session-cookie"
    with factory() as session:
        session.add(
            SessionRow(
                id=auth_session.hash_cookie_value(cookie),
                user_id=ctx.actor_id,
                workspace_id=ws_id,
                expires_at=_PINNED + timedelta(days=7),
                absolute_expires_at=_PINNED + timedelta(days=90),
                last_seen_at=_PINNED,
                ua_hash=None,
                ip_hash=None,
                fingerprint_hash=None,
                created_at=_PINNED,
            )
        )
        session.commit()

    client = _client(ctx, factory)
    client.cookies.set(auth_session.SESSION_COOKIE_NAME, cookie)
    response = client.post(_path(ctx))

    assert response.status_code == 200, response.text
    with factory() as session:
        row = session.get(SessionRow, auth_session.hash_cookie_value(cookie))
        assert row is not None
        assert row.workspace_id is None
