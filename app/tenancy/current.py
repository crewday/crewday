"""Current-request :class:`WorkspaceContext` carrier.

Uses :mod:`contextvars` rather than threadlocals: async-safe and
auto-scoped per asyncio task. The FastAPI middleware resolves a
context from the URL + session, calls :func:`set_current`, keeps the
returned :class:`~contextvars.Token`, and pairs it with
:func:`reset_current` in a ``finally`` block so the context does not
leak across requests.

See ``docs/specs/01-architecture.md`` §"WorkspaceContext" and
§"Tenant filter enforcement".
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from app.tenancy.context import WorkspaceContext

__all__ = [
    "get_current",
    "is_tenant_agnostic",
    "reset_current",
    "set_current",
    "tenant_agnostic",
]


_current_ctx: ContextVar[WorkspaceContext | None] = ContextVar(
    "crewday_current_workspace_ctx",
    default=None,
)
_tenant_agnostic: ContextVar[bool] = ContextVar(
    "crewday_tenant_agnostic",
    default=False,
)


def get_current() -> WorkspaceContext | None:
    """Return the context set for this task, or ``None`` if unset."""
    return _current_ctx.get()


def set_current(
    ctx: WorkspaceContext | None,
) -> Token[WorkspaceContext | None]:
    """Install ``ctx`` for this task and return the restore token.

    Callers MUST pair this with :func:`reset_current` (usually in a
    ``finally`` block) so the context does not leak into unrelated
    tasks sharing the worker.
    """
    return _current_ctx.set(ctx)


def reset_current(token: Token[WorkspaceContext | None]) -> None:
    """Restore the context to the value captured in ``token``."""
    _current_ctx.reset(token)


def is_tenant_agnostic() -> bool:
    """Return ``True`` inside a :func:`tenant_agnostic` block."""
    return _tenant_agnostic.get()


@contextmanager
def tenant_agnostic() -> Iterator[None]:
    """Flip the tenant-filter off inside this block.

    Every call MUST carry a ``# justification:`` comment at the call
    site (CI gate enforces — see
    ``docs/specs/01-architecture.md`` §"Tenant filter enforcement").
    Nesting restores the outer flag on exit.
    """
    token = _tenant_agnostic.set(True)
    try:
        yield
    finally:
        _tenant_agnostic.reset(token)
