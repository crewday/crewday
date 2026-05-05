"""Workspace notification audience queries."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.tenancy import tenant_agnostic

__all__ = ["list_owner_manager_user_ids", "list_owner_user_ids"]


def list_owner_user_ids(session: Session, *, workspace_id: str) -> tuple[str, ...]:
    with tenant_agnostic():
        owner_ids = session.scalars(
            select(PermissionGroupMember.user_id)
            .join(PermissionGroup, PermissionGroup.id == PermissionGroupMember.group_id)
            .where(PermissionGroup.workspace_id == workspace_id)
            .where(PermissionGroupMember.workspace_id == workspace_id)
            .where(PermissionGroup.slug == "owners")
        ).all()
    return tuple(sorted(set(owner_ids)))


def list_owner_manager_user_ids(
    session: Session, *, workspace_id: str
) -> tuple[str, ...]:
    owner_ids = list_owner_user_ids(session, workspace_id=workspace_id)
    with tenant_agnostic():
        manager_ids = session.scalars(
            select(RoleGrant.user_id).where(
                RoleGrant.workspace_id == workspace_id,
                RoleGrant.scope_kind == "workspace",
                RoleGrant.grant_role == "manager",
                RoleGrant.revoked_at.is_(None),
            )
        ).all()
    return tuple(sorted(set(owner_ids).union(manager_ids)))
