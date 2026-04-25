"""Identity context — users, passkeys, sessions, API tokens, role grants.

See docs/specs/01-architecture.md §"Context map",
docs/specs/03-auth-and-tokens.md, and docs/specs/05-employees-and-roles.md.

Public surface re-exports the context's repository ports per
docs/specs/01-architecture.md §"Boundary rules" rule 1: "the context's
repository port plus any context-specific adapter Protocols". Sibling
contexts and the SA-backed adapter both pull
``PermissionGroupRepository`` / ``RoleGrantRepository`` from here.
"""

from __future__ import annotations

from app.domain.identity.ports import (
    PermissionGroupMemberRow,
    PermissionGroupRepository,
    PermissionGroupRow,
    PermissionGroupSlugTakenError,
    RoleGrantRepository,
    RoleGrantRow,
)

__all__ = [
    "PermissionGroupMemberRow",
    "PermissionGroupRepository",
    "PermissionGroupRow",
    "PermissionGroupSlugTakenError",
    "RoleGrantRepository",
    "RoleGrantRow",
]
