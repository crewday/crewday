"""Identity-context factories.

Builders for the identity primitives (workspace, user membership)
shared across every test tier. Production signup (cd-3i5) ships its
own flow; the helpers here exist purely to seed a DB for the
integration + API suites before that flow lands.

See ``docs/specs/17-testing-quality.md`` §"Unit" and
``docs/specs/03-auth-and-tokens.md`` §"WorkspaceContext".
"""

from __future__ import annotations

import factory
from sqlalchemy.orm import Session

from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "WorkspaceContextFactory",
    "bootstrap_workspace",
    "build_workspace_context",
]


class WorkspaceContextFactory(factory.Factory):
    """Build a :class:`~app.tenancy.WorkspaceContext` with deterministic
    defaults.

    factory-boy is not annotated, so the class-level attributes look
    untyped; the built instance is still a ``WorkspaceContext`` at
    runtime. Prefer :func:`build_workspace_context` at call sites for
    a typed wrapper.
    """

    class Meta:
        model = WorkspaceContext

    workspace_id = factory.LazyFunction(new_ulid)
    workspace_slug = factory.Sequence(lambda n: f"ws-{n}")
    actor_id = factory.LazyFunction(new_ulid)
    actor_kind = "user"
    actor_grant_role = "manager"
    actor_was_owner_member = True
    audit_correlation_id = factory.LazyFunction(new_ulid)


def build_workspace_context(**overrides: object) -> WorkspaceContext:
    """Return a :class:`WorkspaceContext` built from the factory.

    Typed wrapper that hides factory-boy's untyped call surface from
    callers. ``overrides`` is declared ``object`` to keep the factory
    untyped kwargs permissive while avoiding a public ``Any``.
    """
    built = WorkspaceContextFactory(**overrides)
    assert isinstance(built, WorkspaceContext)
    return built


def bootstrap_workspace(
    session: Session,
    *,
    slug: str,
    name: str,
    owner_user_id: str,
    clock: Clock | None = None,
) -> Workspace:
    """Seed a :class:`Workspace` + owner :class:`UserWorkspace` row.

    Test-only. Production signup (cd-3i5) will ship its own flow that
    also seeds ``role_grants``, emits ``workspace.created`` audit, and
    honours quota. This helper does none of that; its sole purpose is
    to unblock integration tests that need a live tenant row before the
    signup domain service lands.

    The helper runs under :func:`~app.tenancy.tenant_agnostic` because
    it creates the tenancy anchor before any
    :class:`~app.tenancy.WorkspaceContext` exists — there is literally
    nothing to filter against yet.
    """
    now = (clock if clock is not None else SystemClock()).now()
    workspace_id = new_ulid()
    # justification: seeding the tenancy anchor before a WorkspaceContext
    # exists; the ORM tenant filter has no ctx to apply here.
    with tenant_agnostic():
        workspace = Workspace(
            id=workspace_id,
            slug=slug,
            name=name,
            plan="free",
            quota_json={},
            created_at=now,
        )
        session.add(workspace)
        session.flush()
        session.add(
            UserWorkspace(
                user_id=owner_user_id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=now,
            )
        )
        session.flush()
    return workspace
