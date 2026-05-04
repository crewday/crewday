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

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.workspace.models import UserWorkspace
from app.api.deps import current_workspace_context, db_session
from app.api.v1._problem_json import IDENTITY_PROBLEM_RESPONSES
from app.auth.passkey import (
    LastPasskeyCredential,
    PasskeyNotFound,
    admin_revoke_passkey,
)
from app.auth.webauthn import base64url_to_bytes
from app.authz.owners import is_owner_member
from app.tenancy import WorkspaceContext, tenant_agnostic

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]

router = APIRouter(tags=["workspace_admin"], responses=IDENTITY_PROBLEM_RESPONSES)


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
