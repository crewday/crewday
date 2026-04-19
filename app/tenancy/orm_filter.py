"""Auto-inject ``AND workspace_id = :current_workspace_id`` on every query.

SQLAlchemy ``do_orm_execute`` hook. For each ORM ``execute()`` that ships a
``Select``, ``Update``, or ``Delete`` against a workspace-scoped table, this
module rewrites the statement to pin ``workspace_id`` to the active
:class:`~app.tenancy.context.WorkspaceContext`. Developers write queries
normally; the filter is never threaded by hand.

Failure modes:

* Query against a scoped table with **no** :class:`WorkspaceContext` and
  **not** inside :func:`~app.tenancy.current.tenant_agnostic` raises
  :class:`TenantFilterMissing` **before** any SQL reaches the DB.
* Query inside a ``tenant_agnostic()`` block skips the filter. Every call
  site MUST carry a ``# justification:`` comment; CI grep
  (``scripts/check_tenant_agnostic.py``) enforces.
* ``Insert`` statements are not touched — rows must already carry
  ``workspace_id`` (the caller's domain service is responsible; the
  DB-level ``NOT NULL`` plus FK back the invariant).

Coverage boundaries (know these before relying on the filter):

* **Top-level FROM clause**: plain tables, joins (including self-joins
  with :func:`~sqlalchemy.orm.aliased`), and aliases of scoped tables
  are walked and filtered. Aliases have the predicate injected against
  the alias's own columns (``alias.c.workspace_id``), so the generated
  SQL filters the exact FROM element the caller wrote — not a stray
  cartesian-producing bare table.
* **Subquery / CTE in top-level FROM** pointing at a scoped table:
  fail closed — the walker raises :class:`TenantFilterMissing`
  because the rewriter cannot safely add a predicate inside an opaque
  selectable without reshaping the query.
* **Subquery in ``WHERE`` / ``SET`` / scalar subquery / correlated
  select / UNION leg** referencing a scoped table: **not** walked and
  therefore **not** auto-filtered. The outer target is what the walker
  sees; predicates buried deeper pass through unchanged. Developers
  writing these shapes MUST thread ``workspace_id`` into the inner
  query by hand until this is recursively rewritten (tracked in a
  follow-up Beads task). The ``text()`` escape is also opaque.

See ``docs/specs/01-architecture.md`` §"Tenant filter enforcement" and
``docs/specs/15-security-privacy.md`` §"Tenant isolation".
"""

from __future__ import annotations

from sqlalchemy import Delete, Select, Table, Update, event
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker
from sqlalchemy.sql.selectable import CTE, Alias, FromClause, Join, Subquery

from app.tenancy import current, registry

__all__ = ["TenantFilterMissing", "install_tenant_filter"]


class TenantFilterMissing(RuntimeError):
    """Raised when a scoped-table query runs without tenant context.

    The offending table name travels on the exception so tests and logs
    can pinpoint which repository lacks a :class:`WorkspaceContext`.
    """

    table: str

    def __init__(self, *, table: str) -> None:
        super().__init__(
            f"query against tenant-scoped table {table!r} ran without a "
            "WorkspaceContext (use tenant_agnostic() if intentional)"
        )
        self.table = table


def install_tenant_filter(target: type[Session] | sessionmaker[Session]) -> None:
    """Register the :class:`~sqlalchemy.orm.SessionEvents` hook on ``target``.

    ``target`` may be a :class:`~sqlalchemy.orm.Session` subclass or a
    :class:`~sqlalchemy.orm.sessionmaker`. SQLAlchemy's event system
    accepts either. The hook fires on every ``session.execute()``.

    Idempotent: :func:`sqlalchemy.event.contains` gates against
    double-registration so the second call is a no-op. Without this
    guard, a re-registered listener would append a *second* identical
    ``workspace_id`` filter on every query — a correctness (not just
    performance) concern, because hidden duplicate predicates mask
    bugs in the underlying filter logic.
    """
    if not event.contains(target, "do_orm_execute", _do_orm_execute):
        event.listen(target, "do_orm_execute", _do_orm_execute)


def _do_orm_execute(orm_execute_state: ORMExecuteState) -> None:
    stmt = orm_execute_state.statement
    if not isinstance(stmt, Select | Update | Delete):
        return

    # Bail before walking: ``tenant_agnostic()`` is the single escape hatch
    # and must skip every code path — including the subquery-detector
    # that can otherwise fail closed on legitimate cross-tenant reads.
    if current.is_tenant_agnostic():
        return

    scoped = _collect_scoped_targets(stmt)
    if not scoped:
        return

    ctx = current.get_current()
    if ctx is None:
        # Deterministic offender for reproducible error messages across
        # Python versions — scoped target order depends on the walker.
        offender = sorted(_target_name(t) for t in scoped)[0]
        raise TenantFilterMissing(table=offender)

    new_stmt: Select[tuple[object, ...]] | Update | Delete = stmt
    workspace_id = ctx.workspace_id
    for target in scoped:
        # ``target.c.workspace_id`` resolves to the aliased column when
        # ``target`` is an :class:`Alias`, and to the bare column when
        # ``target`` is a :class:`Table`. Using the target's own column
        # avoids the bug where filtering a query built on ``aliased(...)``
        # would add a second bare-table FROM element (cartesian product).
        new_stmt = new_stmt.where(target.c.workspace_id == workspace_id)
    orm_execute_state.statement = new_stmt


def _target_name(target: Table | Alias) -> str:
    """Return the underlying table name for a walked FROM target."""
    if isinstance(target, Table):
        return target.name
    element = target.element
    if isinstance(element, Table):
        return element.name
    # Defensive fallback — walker only ever hands back Table or Alias-of-Table.
    return getattr(element, "name", "<unknown>")


def _collect_scoped_targets(
    stmt: Select[tuple[object, ...]] | Update | Delete,
) -> list[Table | Alias]:
    """Return scoped FROM targets for ``stmt``.

    Targets are :class:`~sqlalchemy.Table` or :class:`Alias` instances;
    the caller injects the predicate against ``target.c.workspace_id``
    so an aliased FROM element is filtered on its alias columns, not
    the base table's.

    For :class:`~sqlalchemy.sql.Select` every element of
    ``get_final_froms()`` is walked: plain tables and aliases of scoped
    tables are collected (deduplicated by object identity, so a
    self-join with an aliased side is filtered on **both** sides);
    joins are walked recursively; subqueries/CTEs that reach scoped
    tables raise :class:`TenantFilterMissing` because the rewriter
    cannot safely inject a filter inside an opaque selectable.

    For :class:`~sqlalchemy.sql.Update` / :class:`~sqlalchemy.sql.Delete`
    the sole target is ``stmt.table``.
    """
    if isinstance(stmt, Update | Delete):
        target = stmt.table
        if isinstance(target, Table) and registry.is_scoped(target.name):
            return [target]
        if isinstance(target, Alias):
            element = target.element
            if isinstance(element, Table) and registry.is_scoped(element.name):
                return [target]
        return []

    # stmt is a Select
    collected: list[Table | Alias] = []
    seen: set[int] = set()
    for from_clause in stmt.get_final_froms():
        _walk_from(from_clause, collected, seen)
    return collected


def _walk_from(
    node: object,
    collected: list[Table | Alias],
    seen: set[int],
) -> None:
    """Depth-walk a FROM element and append every scoped target to ``collected``.

    ``seen`` deduplicates by **object identity** (``id(node)``), not by
    ``Table.name``: a self-join with an ``aliased(Model)`` right side
    shares the underlying ``Table.name`` with the left but is a distinct
    :class:`Alias` object — both sides need a workspace filter, else a
    cross-workspace self-join slips through. Subquery / CTE nodes that
    wrap a scoped table raise :class:`TenantFilterMissing`: the hook
    cannot safely rewrite inside an opaque selectable, so it fails
    closed rather than emit an unfiltered query. Callers who need that
    wrap the block in :func:`~app.tenancy.current.tenant_agnostic`.
    """
    if isinstance(node, Table):
        if registry.is_scoped(node.name) and id(node) not in seen:
            collected.append(node)
            seen.add(id(node))
        return
    if isinstance(node, Join):
        _walk_from(node.left, collected, seen)
        _walk_from(node.right, collected, seen)
        return
    if isinstance(node, Subquery | CTE):
        # Peek inside for scoped tables; if any, fail closed. The rewriter
        # can't reach into a compiled selectable to add a predicate on the
        # inner table without changing the query shape.
        inner = node.element
        if _subselect_has_scoped(inner):
            offender = _first_scoped_in(inner)
            raise TenantFilterMissing(table=offender)
        return
    if isinstance(node, Alias):
        # Alias of a scoped Table: keep the Alias in ``collected`` so the
        # caller filters on ``alias.c.workspace_id``. Don't unwrap to the
        # base Table — that would add a bare-table FROM and produce a
        # cartesian product with the alias.
        element = node.element
        if isinstance(element, Table):
            if registry.is_scoped(element.name) and id(node) not in seen:
                collected.append(node)
                seen.add(id(node))
            return
        # Alias wrapping a selectable (subquery-as-alias): descend once
        # into the wrapped element so the fail-closed path can trip if it
        # hides a scoped table.
        _walk_from(element, collected, seen)
        return
    # Anything else (Values, etc.): walk one element deep if it has an
    # ``element`` attribute so odd wrappers around scoped tables are
    # still caught. Leave unrecognised shapes alone.
    if isinstance(node, FromClause):
        inner_attr = getattr(node, "element", None)
        if inner_attr is not None and inner_attr is not node:
            _walk_from(inner_attr, collected, seen)


def _subselect_has_scoped(selectable: object) -> bool:
    """Return ``True`` if any table referenced by ``selectable`` is scoped."""
    return _first_scoped_in(selectable) != ""


def _first_scoped_in(selectable: object) -> str:
    """Return the first scoped table name inside ``selectable``, else ``""``.

    Walks the selectable's ``froms`` recursively. Used only to name the
    offending table in :class:`TenantFilterMissing` when a subquery or
    CTE hides a scoped table from the top-level rewriter.
    """
    froms_attr = getattr(selectable, "get_final_froms", None)
    if froms_attr is None:
        return ""
    froms = froms_attr()
    for f in froms:
        name = _first_scoped_name(f)
        if name:
            return name
    return ""


def _first_scoped_name(node: object) -> str:
    """Recursively find the first scoped table name reachable from ``node``."""
    if isinstance(node, Table):
        return node.name if registry.is_scoped(node.name) else ""
    if isinstance(node, Join):
        left = _first_scoped_name(node.left)
        if left:
            return left
        return _first_scoped_name(node.right)
    if isinstance(node, Subquery | CTE):
        return _first_scoped_in(node.element)
    inner_attr = getattr(node, "element", None)
    if inner_attr is not None and inner_attr is not node:
        return _first_scoped_name(inner_attr)
    return ""
