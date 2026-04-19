"""authz — permission groups, group members, role grants.

Importing this package registers every authz table as
workspace-scoped in :mod:`app.tenancy.registry`. Unlike the
``identity`` context — where ``user`` / ``session`` / ``api_token``
stay tenant-agnostic so sign-in can run before a
:class:`~app.tenancy.WorkspaceContext` exists — every table in this
package carries a ``workspace_id`` column and is always queried
under a live context (the resolver reads the active user's grants
only after the workspace has been picked).

Re-exports the seed helper :func:`seed_owners_system_group` from
:mod:`app.adapters.db.authz.bootstrap` so the production signup
flow (cd-3i5) and the integration-test factories share a single
call surface.

See ``docs/specs/02-domain-model.md`` §"permission_group",
§"permission_group_member", §"role_grants" and
``docs/specs/05-employees-and-roles.md`` §"Roles & groups".
"""

from __future__ import annotations

from app.adapters.db.authz.bootstrap import seed_owners_system_group
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.tenancy.registry import register

register("permission_group")
register("permission_group_member")
register("role_grant")

__all__ = [
    "PermissionGroup",
    "PermissionGroupMember",
    "RoleGrant",
    "seed_owners_system_group",
]
