"""WorkspaceContext — canonical per-request tenancy + actor record.

Every domain service function accepts a :class:`WorkspaceContext` as its
first argument. The context is resolved once per request by the tenancy
middleware and passed down into every service call and repository method.

See ``docs/specs/01-architecture.md`` §"WorkspaceContext".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["ActorGrantRole", "ActorKind", "WorkspaceContext"]


ActorKind = Literal["user", "agent", "system"]
ActorGrantRole = Literal["manager", "worker", "client", "guest"]


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    """Canonical per-request tenancy + actor record.

    Immutable: equality compares every field. The middleware builds one
    instance per request; downstream services consume it read-only.
    """

    workspace_id: str
    workspace_slug: str
    actor_id: str
    actor_kind: ActorKind
    actor_grant_role: ActorGrantRole
    actor_was_owner_member: bool
    audit_correlation_id: str
