"""Registry of workspace-scoped table names.

The SQLAlchemy ``do_orm_execute`` hook in :mod:`app.tenancy.orm_filter`
reads this registry to decide whether to inject
``AND workspace_id = :current_workspace_id`` (or, for tables that scope
through a junction, an ``IN (SELECT ... FROM <via> WHERE ...)`` predicate)
into a query. Migrations register each workspace-scoped table at import
time; cross-tenant tables (``workspace``, ``user``, ``role_grant``, …)
stay out.

Two kinds of scoping are supported:

* :func:`register` — for tables that carry a literal ``workspace_id``
  column. The hook injects ``<table>.workspace_id = :ctx_workspace``
  directly.
* :func:`register_scope_through_join` — for tables whose workspace
  boundary is enforced via a junction table (``unit`` / ``area`` /
  ``property_closure`` reach the boundary through ``property_workspace``).
  The hook either verifies the caller already joined the junction with
  the right predicate or auto-injects an ``IN (SELECT ...)`` filter.

See ``docs/specs/01-architecture.md`` §"Tenant filter enforcement" and
``docs/specs/15-security-privacy.md`` §"Row-level security".
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

__all__ = [
    "ScopeThroughJoin",
    "_reset_for_tests",
    "get_scope_through_join",
    "is_scoped",
    "register",
    "register_scope_through_join",
    "scope_through_join_tables",
    "scoped_tables",
]


@dataclass(frozen=True, slots=True)
class ScopeThroughJoin:
    """How to enforce workspace scope on a table that lacks ``workspace_id``.

    ``unit``, ``area``, and ``property_closure`` belong to a property
    via ``property_id``; the workspace boundary is enforced by joining
    through ``property_workspace`` (which carries ``workspace_id``).
    Repositories writing such queries can either spell the join out
    themselves or let the ORM tenant filter inject an ``IN (SELECT ...)``
    predicate equivalent to that join.

    Attributes:
        via_table: The junction-table name (e.g. ``"property_workspace"``).
        via_local_column: The column on the scoped table that joins to
            the junction (``unit.property_id`` -> ``"property_id"``).
        via_remote_column: The column on the junction the local column
            joins against (``property_workspace.property_id`` -> ``"property_id"``).
        workspace_column: The junction column carrying ``workspace_id``.
            Defaults to ``"workspace_id"``.
    """

    via_table: str
    via_local_column: str
    via_remote_column: str
    workspace_column: str = "workspace_id"


_lock = threading.Lock()
_WORKSPACE_SCOPED_TABLES: set[str] = set()
_SCOPE_THROUGH_JOIN_TABLES: dict[str, ScopeThroughJoin] = {}


def register(table_name: str) -> None:
    """Mark ``table_name`` as workspace-scoped via a local ``workspace_id``.

    Idempotent: a second call with the same name is a no-op. Thread-safe:
    multiple ASGI workers in the same process may register concurrently
    at startup.
    """
    with _lock:
        _WORKSPACE_SCOPED_TABLES.add(table_name)


def register_scope_through_join(
    table_name: str,
    *,
    via_table: str,
    via_local_column: str,
    via_remote_column: str,
    workspace_column: str = "workspace_id",
) -> None:
    """Mark ``table_name`` as workspace-scoped via a junction join.

    Use this for tables that do **not** carry a ``workspace_id`` column
    of their own (``unit``, ``area``, ``property_closure``). The ORM
    tenant filter (:mod:`app.tenancy.orm_filter`) will either verify
    that ``via_table`` is already joined with a
    ``via_table.workspace_column == :ctx_workspace`` predicate or
    auto-inject the equivalent ``IN (SELECT ...)`` filter; bare reads
    without a :class:`~app.tenancy.WorkspaceContext` raise
    :class:`~app.tenancy.orm_filter.TenantFilterMissing` exactly like
    plain :func:`register` tables.

    UPDATE / DELETE against scope-through-join tables fail closed: the
    injection helper for those bulk DML statements only knows how to
    add a local ``workspace_id`` predicate, and dropping a join
    relationship into an ``UPDATE FROM`` would change the statement
    shape in ways the rewriter cannot prove safe across SQLAlchemy
    minor versions. Callers that genuinely need a bulk write thread
    the ``IN (SELECT ... FROM via WHERE workspace_id = ctx)`` predicate
    by hand or wrap the block in :func:`tenant_agnostic`.

    Idempotent: re-registering with the same parameters is a no-op.
    Re-registering with a different :class:`ScopeThroughJoin` shape
    raises :class:`ValueError` — silent overwrite would mask a wiring
    bug. Thread-safe.

    See ``docs/specs/01-architecture.md`` §"Tenant filter enforcement"
    and ``docs/specs/15-security-privacy.md`` §"Row-level security".
    """
    spec = ScopeThroughJoin(
        via_table=via_table,
        via_local_column=via_local_column,
        via_remote_column=via_remote_column,
        workspace_column=workspace_column,
    )
    with _lock:
        existing = _SCOPE_THROUGH_JOIN_TABLES.get(table_name)
        if existing is not None and existing != spec:
            raise ValueError(
                f"table {table_name!r} already registered as scope-through-join "
                f"with a different shape ({existing!r}); refusing to overwrite "
                f"with {spec!r}"
            )
        _SCOPE_THROUGH_JOIN_TABLES[table_name] = spec


def is_scoped(table_name: str) -> bool:
    """Return ``True`` if ``table_name`` is workspace-scoped (either kind)."""
    # Membership on a set / dict is atomic in CPython; no lock needed for reads.
    return (
        table_name in _WORKSPACE_SCOPED_TABLES
        or table_name in _SCOPE_THROUGH_JOIN_TABLES
    )


def get_scope_through_join(table_name: str) -> ScopeThroughJoin | None:
    """Return the :class:`ScopeThroughJoin` spec for ``table_name``, if any.

    Returns :data:`None` for tables registered via plain :func:`register`
    or unregistered tables. Callers use this to decide whether to apply
    the local-column injection path or the join-rewriting path.
    """
    return _SCOPE_THROUGH_JOIN_TABLES.get(table_name)


def scoped_tables() -> frozenset[str]:
    """Return an immutable snapshot of every registered scoped table name."""
    with _lock:
        return frozenset(_WORKSPACE_SCOPED_TABLES | _SCOPE_THROUGH_JOIN_TABLES.keys())


def scope_through_join_tables() -> dict[str, ScopeThroughJoin]:
    """Return a snapshot of every scope-through-join registration."""
    with _lock:
        return dict(_SCOPE_THROUGH_JOIN_TABLES)


def _reset_for_tests() -> None:
    """Clear the registry. Tests use this to isolate cases.

    Underscore-prefixed: not part of the public surface.
    """
    with _lock:
        _WORKSPACE_SCOPED_TABLES.clear()
        _SCOPE_THROUGH_JOIN_TABLES.clear()
