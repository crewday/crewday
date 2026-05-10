"""Workspace-scoped admin surface aggregator.

This router is the reserved seat for future workspace-scoped admin
views that don't fit any single bounded context. Signup abuse
surfacing is **not** here: cd-1h7k resolved `/admin/signups` as a
deployment-admin surface because pre-workspace signup signals have no
workspace to scope to.

Mounted by :mod:`app.api.factory` via :data:`app.api.v1.CONTEXT_ROUTERS`
under the workspace prefix, so every route lands at
``/w/<slug>/api/v1/admin/...``. The tenancy middleware resolves the
active :class:`~app.tenancy.WorkspaceContext` from the ``<slug>``
segment before any handler runs — admin endpoints therefore always
operate on a concrete workspace, never on the bare host.

**Not the deployment-scoped admin tree.** :mod:`app.api.admin`
(``/admin/api/v1/*``) is a separate, deployment-operator surface
gated on ``(scope_kind='deployment', grant_role='manager')``. The two
trees never overlap: the deployment admin mounts LLM provider
config, cross-workspace usage, deployment-wide audit, and signup
abuse signals; this workspace admin seat remains available for
future per-workspace security or health views.

See ``docs/specs/15-security-privacy.md`` §"Self-serve abuse
mitigations", ``docs/specs/12-rest-api.md`` §"Base URL", and
``docs/specs/13-cli.md`` §"CLI surface".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.adapters.storage.ports import Storage
from app.api.admin._workspace_state import (
    archive_workspace_if_needed,
    schedule_workspace_deletion_if_needed,
)
from app.api.deps import current_workspace_context, db_session, get_storage
from app.api.v1._problem_json import IDENTITY_PROBLEM_RESPONSES
from app.audit import write_audit
from app.auth import session as auth_session
from app.auth.passkey import (
    LastPasskeyCredential,
    PasskeyNotFound,
    admin_revoke_passkey,
)
from app.auth.webauthn import base64url_to_bytes
from app.authz.owners import is_owner_member
from app.services.workspace.export_service import (
    WORKSPACE_EXPORT_MEDIA_TYPE,
    build_workspace_export,
)
from app.services.workspace.settings_service import OwnersOnlyError
from app.tenancy import WorkspaceContext, tenant_agnostic

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_Storage = Annotated[Storage, Depends(get_storage)]

router = APIRouter(tags=["workspace_admin"], responses=IDENTITY_PROBLEM_RESPONSES)


class WorkspaceArchiveResponse(BaseModel):
    id: str
    archived_at: str


class WorkspaceDeleteResponse(BaseModel):
    id: str
    archived_at: str
    delete_requested_at: str
    purge_after: str


@router.post(
    "/workspace/export",
    operation_id="workspace_admin.workspace.export",
    summary="Download a full workspace export",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "content": {
                WORKSPACE_EXPORT_MEDIA_TYPE: {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
            "description": "ZIP workspace export artifact.",
        }
    },
    openapi_extra={
        "x-cli": {
            "group": "workspace-admin",
            "verb": "workspace-export",
            "summary": "Download a full workspace export artifact",
            "mutates": False,
        },
        "x-owner-only": True,
    },
)
def export_workspace(ctx: _Ctx, session: _Db, storage: _Storage) -> Response:
    try:
        artifact = build_workspace_export(
            session,
            ctx,
            storage=storage,
        )
    except OwnersOnlyError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "owners_only"},
        ) from exc

    file_count = len(artifact.manifest["files"])
    missing_file_count = len(artifact.manifest["missing_files"])
    write_audit(
        session,
        ctx,
        entity_kind="workspace",
        entity_id=ctx.workspace_id,
        action="workspace.export_requested",
        diff={
            "filename": artifact.filename,
            "content_type": artifact.content_type,
            "size_bytes": len(artifact.content),
            "table_count": len(artifact.manifest["tables"]),
            "file_count": file_count,
            "missing_file_count": missing_file_count,
        },
        via="api",
    )

    return Response(
        content=artifact.content,
        media_type=artifact.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
        },
    )


def _clear_current_session_workspace(
    session: Session, *, ctx: WorkspaceContext, request: Request
) -> None:
    if ctx.principal_kind != "session":
        return
    cookie_value = request.cookies.get(
        auth_session.SESSION_COOKIE_NAME
    ) or request.cookies.get("crewday_session")
    if not cookie_value:
        return
    with tenant_agnostic():
        row = session.get(SessionRow, auth_session.hash_cookie_value(cookie_value))
    if row is None:
        return
    if row.user_id == ctx.actor_id and row.workspace_id == ctx.workspace_id:
        row.workspace_id = None


@router.post(
    "/workspace/archive",
    response_model=WorkspaceArchiveResponse,
    operation_id="workspace_admin.workspace.archive",
    summary="Archive the current workspace",
    openapi_extra={
        "x-cli": {
            "group": "workspace-admin",
            "verb": "workspace-archive",
            "summary": "Archive the current workspace",
            "mutates": True,
        },
        "x-owner-only": True,
    },
)
def archive_current_workspace(
    request: Request,
    ctx: _Ctx,
    session: _Db,
) -> WorkspaceArchiveResponse:
    if not is_owner_member(
        session, workspace_id=ctx.workspace_id, user_id=ctx.actor_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "owners_only"},
        )

    with tenant_agnostic():
        workspace = session.get(Workspace, ctx.workspace_id)
        if workspace is None:
            raise RuntimeError(
                f"workspace {ctx.workspace_id!r} present in ctx but absent in DB"
            )
        archived_at, changed = archive_workspace_if_needed(
            session,
            workspace,
            when=datetime.now(UTC),
        )
        if changed:
            write_audit(
                session,
                ctx,
                entity_kind="workspace",
                entity_id=ctx.workspace_id,
                action="workspace.archived",
                diff={"archived_at": archived_at.astimezone(UTC).isoformat()},
                via="api",
            )
        _clear_current_session_workspace(session, ctx=ctx, request=request)
        session.flush()

    return WorkspaceArchiveResponse(
        id=ctx.workspace_id,
        archived_at=archived_at.astimezone(UTC).isoformat(),
    )


@router.post(
    "/workspace/delete",
    response_model=WorkspaceDeleteResponse,
    operation_id="workspace_admin.workspace.delete",
    summary="Schedule deletion of the current workspace",
    openapi_extra={
        "x-cli": {
            "group": "workspace-admin",
            "verb": "workspace-delete",
            "summary": "Archive and schedule deletion of the current workspace",
            "mutates": True,
        },
        "x-owner-only": True,
    },
)
def delete_current_workspace(
    request: Request,
    ctx: _Ctx,
    session: _Db,
) -> WorkspaceDeleteResponse:
    if not is_owner_member(
        session, workspace_id=ctx.workspace_id, user_id=ctx.actor_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "owners_only"},
        )

    now = datetime.now(UTC)
    with tenant_agnostic():
        workspace = session.get(Workspace, ctx.workspace_id)
        if workspace is None:
            raise RuntimeError(
                f"workspace {ctx.workspace_id!r} present in ctx but absent in DB"
            )
        archived_at, archived_changed = archive_workspace_if_needed(
            session,
            workspace,
            when=now,
        )
        delete_requested_at, purge_after, delete_changed = (
            schedule_workspace_deletion_if_needed(session, workspace, when=now)
        )
        if archived_changed:
            write_audit(
                session,
                ctx,
                entity_kind="workspace",
                entity_id=ctx.workspace_id,
                action="workspace.archived",
                diff={"archived_at": archived_at.astimezone(UTC).isoformat()},
                via="api",
            )
        if delete_changed:
            write_audit(
                session,
                ctx,
                entity_kind="workspace",
                entity_id=ctx.workspace_id,
                action="workspace.delete_requested",
                diff={
                    "archived_at": archived_at.astimezone(UTC).isoformat(),
                    "delete_requested_at": delete_requested_at.astimezone(
                        UTC
                    ).isoformat(),
                    "purge_after": purge_after.astimezone(UTC).isoformat(),
                },
                via="api",
            )
        _clear_current_session_workspace(session, ctx=ctx, request=request)
        session.flush()

    return WorkspaceDeleteResponse(
        id=ctx.workspace_id,
        archived_at=archived_at.astimezone(UTC).isoformat(),
        delete_requested_at=delete_requested_at.astimezone(UTC).isoformat(),
        purge_after=purge_after.astimezone(UTC).isoformat(),
    )


def _assert_current_workspace_membership(
    session: Session, *, ctx: WorkspaceContext, user_id: str
) -> None:
    row = session.get(UserWorkspace, (user_id, ctx.workspace_id))
    if row is None or row.workspace_id != ctx.workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "employee_not_found"},
        )


def _assert_session_principal(ctx: WorkspaceContext) -> None:
    if ctx.principal_kind != "session":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "session_only_endpoint"},
            headers={"WWW-Authenticate": 'error="session_only_endpoint"'},
        )


def _assert_actor_owns_all_target_workspaces(
    session: Session, *, ctx: WorkspaceContext, target_user_id: str
) -> None:
    with tenant_agnostic():
        target_workspace_ids = list(
            session.scalars(
                select(UserWorkspace.workspace_id).where(
                    UserWorkspace.user_id == target_user_id
                )
            ).all()
        )
        actor_owns_all = all(
            is_owner_member(
                session,
                workspace_id=workspace_id,
                user_id=ctx.actor_id,
            )
            for workspace_id in target_workspace_ids
        )
    if not actor_owns_all:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "permission_denied"},
        )


@router.delete(
    "/users/{user_id}/passkeys/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="admin.users.passkeys.revoke",
    summary="Revoke one passkey for a workspace member",
    openapi_extra={
        "x-cli": {
            "group": "admin",
            "verb": "user-passkey-revoke",
            "summary": "Revoke one passkey for a workspace member",
            "mutates": True,
        },
        "x-interactive-only": True,
    },
)
def delete_user_passkey(
    user_id: str,
    credential_id: str,
    ctx: _Ctx,
    session: _Db,
) -> Response:
    """Revoke exactly one passkey credential for ``user_id``.

    The target must be a member of the caller's current workspace, and
    the actor must be an ``owners`` member on every workspace the target
    belongs to. Credential ids that are malformed, unknown, or owned by
    another user all collapse to ``404 passkey_not_found``.
    """
    _assert_session_principal(ctx)
    _assert_current_workspace_membership(session, ctx=ctx, user_id=user_id)
    _assert_actor_owns_all_target_workspaces(
        session,
        ctx=ctx,
        target_user_id=user_id,
    )

    try:
        credential_id_bytes = base64url_to_bytes(credential_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "passkey_not_found"},
        ) from exc

    try:
        admin_revoke_passkey(
            ctx,
            session,
            target_user_id=user_id,
            credential_id=credential_id_bytes,
        )
    except PasskeyNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "passkey_not_found"},
        ) from exc
    except LastPasskeyCredential as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "last_credential"},
        ) from exc

    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
