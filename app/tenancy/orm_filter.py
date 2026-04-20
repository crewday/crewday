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

import weakref

from sqlalchemy import Delete, Select, Table, Update, event
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.sql.selectable import CTE, Alias, FromClause, Join, Subquery

from app.tenancy import current, registry

__all__ = ["TenantFilterMissing", "install_tenant_filter"]

# Track which targets have the hook installed. ``WeakSet`` lets the
# entry drop automatically when a ``sessionmaker`` (or ``Session``
# subclass) is garbage-collected, so a new object allocated at the
# **same memory address** is correctly treated as "not yet installed".
# :func:`sqlalchemy.event.contains` keys by ``id(target)`` via a plain
# dict, so it returns a **false positive** after address reuse (cd-3yhd
# flake: a new per-test ``sessionmaker`` inherited a stale True,
# ``install_tenant_filter`` no-op'd, and the fresh ``Session`` had no
# listener — unfiltered SQL hit the wire). Gating on a ``WeakSet``
# we own closes that hole without touching SQLAlchemy internals.
_installed_targets: weakref.WeakSet[type[Session] | sessionmaker[Session]] = (
    weakref.WeakSet()
)


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

    Idempotent by **identity**: a module-level :class:`weakref.WeakSet`
    tracks installed targets so a second call on the same live object
    is a no-op. A double ``event.listen`` appends a *second* identical
    ``workspace_id`` filter on every query — a correctness (not just
    performance) concern because hidden duplicate predicates mask bugs
    in the underlying filter logic.

    Not reusing :func:`sqlalchemy.event.contains` here: it keys by
    ``id(target)``, which is stable only while ``target`` is alive.
    When a ``sessionmaker`` is garbage-collected and a new one is
    allocated at the same address (common in per-test fixtures),
    :func:`event.contains` returns a **false positive** for the new
    object and the install silently no-ops — so the fresh
    ``Session`` has no listener and queries escape unfiltered
    (cd-3yhd flake). ``WeakSet`` drops the entry when the target is
    GC'd, so address reuse re-registers correctly.
    """
    if target in _installed_targets:
        return
    event.listen(target, "do_orm_execute", _do_orm_execute)
    _installed_targets.add(target)


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

    For :class:`~sqlalchemy.sql.Select`, walks the **pre-compile** top
    level FROM sources directly (``_raw_columns`` from the SELECT list,
    ``_from_obj`` from ``select_from``, and ``_setup_joins`` for
    ``.join()`` targets) rather than :meth:`Select.get_final_froms`.
    ``get_final_froms`` materialises an intermediate :class:`Join`
    tree whose annotation state is tied to the ORM compile cache;
    walking the raw pre-compile structure is both stable (cd-3yhd
    investigation path) and cheaper (no compile step).

    Plain tables and aliases of scoped tables are collected
    (deduplicated by object identity, so a self-join with an aliased
    side is filtered on **both** sides). Subqueries / CTEs that reach
    scoped tables raise :class:`TenantFilterMissing` because the
    rewriter cannot safely inject a filter inside an opaque
    selectable. ``WHERE``-clause subqueries are deliberately **not**
    walked — the invariant documented on the module docstring.

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

    # stmt is a Select. Gather the top-level FROM sources without
    # triggering a compile. ``_raw_columns`` + ``_from_obj`` +
    # ``_setup_joins`` is what the ORM itself feeds to the compiler;
    # walking them directly is stable across test orderings because
    # the cached annotations on ``Select.get_final_froms()`` are what
    # vary (cd-3yhd).
    collected: list[Table | Alias] = []
    seen: set[int] = set()
    for source in _iter_select_from_sources(stmt):
        _walk_from(source, collected, seen)
    return collected


def _iter_select_from_sources(
    stmt: Select[tuple[object, ...]],
) -> list[FromClause]:
    """Return top-level FROM sources from ``stmt`` without compiling.

    Order matches SQLAlchemy's own compile-time pipeline: FROMs
    implied by the SELECT list come first, then explicit
    ``select_from(...)`` entries, then ``.join(...)`` right-hand
    sides. Deduplicates by object identity so each source is walked
    once.
    """
    sources: list[FromClause] = []
    seen: set[int] = set()

    def _add(src: object) -> None:
        if isinstance(src, FromClause) and id(src) not in seen:
            seen.add(id(src))
            sources.append(src)

    for raw_col in stmt._raw_columns:
        for fr in raw_col._from_objects:
            _add(fr)
    for from_obj in stmt._from_obj:
        _add(from_obj)
    for setup_join in stmt._setup_joins:
        target = setup_join[0]
        # ``.join(Parent.children)`` stores an ``InstrumentedAttribute``;
        # unwrap to the related mapper's local table.
        if isinstance(target, QueryableAttribute):
            prop = getattr(target, "property", None)
            entity = getattr(prop, "entity", None)
            local_table = getattr(entity, "local_table", None)
            if local_table is not None:
                _add(local_table)
            continue
        _add(target)
    return sources


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

    Still handles :class:`Join` in case a consumer ever hands us a
    pre-joined FROM element, even though :func:`_iter_select_from_sources`
    returns already-unjoined sources today.
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
        offender = _first_scoped_name_in_selectable(inner)
        if offender:
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


def _first_scoped_name_in_selectable(selectable: object) -> str:
    """Return the first scoped table name inside ``selectable``, else ``""``.

    Used only to name the offending table in :class:`TenantFilterMissing`
    when a subquery or CTE hides a scoped table from the top-level
    rewriter. Walks by recursing into ``_raw_columns`` / ``_from_obj`` /
    ``_setup_joins`` for :class:`~sqlalchemy.sql.Select` and falling
    back to a structural walk for other selectables — never calls
    :meth:`Select.get_final_froms` (cd-3yhd flake).
    """
    if isinstance(selectable, Select):
        for source in _iter_select_from_sources(selectable):
            name = _first_scoped_name(source)
            if name:
                return name
        return ""
    # Non-Select selectables (e.g. compound selects): fall back to the
    # structural walk, which handles Table / Join / Alias / Subquery.
    return _first_scoped_name(selectable)


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
        return _first_scoped_name_in_selectable(node.element)
    inner_attr = getattr(node, "element", None)
    if inner_attr is not None and inner_attr is not node:
        return _first_scoped_name(inner_attr)
    return ""
