"""authz — permission groups, group members, role grants.

Importing this package registers every authz table as
workspace-scoped in :mod:`app.tenancy.registry`. Unlike the
``identity`` context — where ``user`` / ``session`` / ``api_token``
stay tenant-agnostic so sign-in can run before a
:class:`~app.tenancy.WorkspaceContext` exists — every table in this
package is workspace-scoped except ``deployment_owner``, which is a
bare-host admin table and is always queried under
``tenant_agnostic()``.

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
    DeploymentOwner,
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.tenancy.registry import register

register("permission_group")
register("permission_group_member")
register("role_grant")

__all__ = [
    "DeploymentOwner",
    "PermissionGroup",
    "PermissionGroupMember",
    "RoleGrant",
    "seed_owners_system_group",
]
