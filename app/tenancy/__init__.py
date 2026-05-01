"""Shared tenancy primitives.

Exports the :class:`WorkspaceContext` dataclass, slug validation,
current-context helpers, and the scoped-table registry.

See ``docs/specs/01-architecture.md`` §"Multi-tenancy runtime".
"""

from app.tenancy.context import (
    ActorGrantRole,
    ActorKind,
    PrincipalKind,
    WorkspaceContext,
)
from app.tenancy.current import (
    get_current,
    is_tenant_agnostic,
    reset_current,
    set_current,
    tenant_agnostic,
)
from app.tenancy.deployment import (
    DEPLOYMENT_SCOPE_CATALOG,
    DEPLOYMENT_SCOPE_PREFIX,
    DeploymentActorKind,
    DeploymentContext,
)
from app.tenancy.registry import (
    ScopeThroughJoin,
    get_scope_through_join,
    is_scoped,
    register,
    register_scope_through_join,
    scope_through_join_tables,
    scoped_tables,
)
from app.tenancy.slug import (
    RESERVED_SLUGS,
    SLUG_PATTERN,
    InvalidSlug,
    is_homoglyph_collision,
    is_reserved,
    normalise_for_collision,
    normalise_slug,
    validate_slug,
)

__all__ = [
    "DEPLOYMENT_SCOPE_CATALOG",
    "DEPLOYMENT_SCOPE_PREFIX",
    "RESERVED_SLUGS",
    "SLUG_PATTERN",
    "ActorGrantRole",
    "ActorKind",
    "DeploymentActorKind",
    "DeploymentContext",
    "InvalidSlug",
    "PrincipalKind",
    "ScopeThroughJoin",
    "WorkspaceContext",
    "get_current",
    "get_scope_through_join",
    "is_homoglyph_collision",
    "is_reserved",
    "is_scoped",
    "is_tenant_agnostic",
    "normalise_for_collision",
    "normalise_slug",
    "register",
    "register_scope_through_join",
    "reset_current",
    "scope_through_join_tables",
    "scoped_tables",
    "set_current",
    "tenant_agnostic",
    "validate_slug",
]
