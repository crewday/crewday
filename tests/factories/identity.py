"""Identity-context factories.

The only bounded context with a usable primitive today is
``identity`` — specifically :class:`app.tenancy.WorkspaceContext`.
Every factory in the other contexts is a placeholder until that
context's domain + DB models land.

See ``docs/specs/17-testing-quality.md`` §"Unit" and
``docs/specs/03-auth-and-tokens.md`` §"WorkspaceContext".
"""

from __future__ import annotations

import factory

from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid

__all__ = ["WorkspaceContextFactory", "build_workspace_context"]


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
