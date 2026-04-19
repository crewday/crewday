"""Registry of workspace-scoped table names.

The SQLAlchemy ``before_compile`` hook (a later task) reads this registry
to decide whether to inject ``AND workspace_id = :current_workspace_id``
into a query. Migrations register each workspace-scoped table at import
time; cross-tenant tables (``workspace``, ``user``, ``role_grant``, …)
stay out.

See ``docs/specs/01-architecture.md`` §"Tenant filter enforcement".
"""

from __future__ import annotations

import threading

__all__ = ["_reset_for_tests", "is_scoped", "register", "scoped_tables"]


_lock = threading.Lock()
_WORKSPACE_SCOPED_TABLES: set[str] = set()


def register(table_name: str) -> None:
    """Mark ``table_name`` as workspace-scoped.

    Idempotent: a second call with the same name is a no-op. Thread-safe:
    multiple ASGI workers in the same process may register concurrently
    at startup.
    """
    with _lock:
        _WORKSPACE_SCOPED_TABLES.add(table_name)


def is_scoped(table_name: str) -> bool:
    """Return ``True`` if ``table_name`` was registered as scoped."""
    # Membership on a set is atomic in CPython; no lock needed for reads.
    return table_name in _WORKSPACE_SCOPED_TABLES


def scoped_tables() -> frozenset[str]:
    """Return an immutable snapshot of registered table names."""
    with _lock:
        return frozenset(_WORKSPACE_SCOPED_TABLES)


def _reset_for_tests() -> None:
    """Clear the registry. Tests use this to isolate cases.

    Underscore-prefixed: not part of the public surface.
    """
    with _lock:
        _WORKSPACE_SCOPED_TABLES.clear()
