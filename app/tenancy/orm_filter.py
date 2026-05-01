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
* **Subquery in ``WHERE`` / ``SET`` / scalar / correlated-EXISTS /
  UNION leg / CTE** referencing a scoped table: walked recursively
  via :func:`sqlalchemy.sql.visitors.cloned_traverse`. Each inner
  :class:`~sqlalchemy.sql.Select` that has a scoped table in its own
  top-level FROM list gets the predicate injected against that inner
  FROM. UNION / INTERSECT legs are descended; correlated EXISTS gets
  the predicate on the inner select; ``Bar.any(Foo.…)``-style
  ``EXISTS`` selects work the same way.
* **Subquery / CTE wrapping a non-Select** (e.g. a raw ``text()``
  selectable that hides a scoped table reference) cannot be rewritten
  because the rewriter has no AST to inject into; if such a wrapper
  reaches a scoped table the hook fails closed with
  :class:`TenantFilterMissing`.
* **Raw ``text()``-based queries** are opaque to the hook entirely;
  developers writing them must thread ``workspace_id`` manually or
  wrap the call in :func:`tenant_agnostic` with a justification.

See ``docs/specs/01-architecture.md`` §"Tenant filter enforcement" and
``docs/specs/15-security-privacy.md`` §"Tenant isolation".
"""

from __future__ import annotations

import weakref

from sqlalchemy import Delete, Select, Table, Update, event
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.sql.selectable import (
    CTE,
    Alias,
    CompoundSelect,
    FromClause,
    Join,
    Subquery,
)
from sqlalchemy.sql.visitors import cloned_traverse

from app.tenancy import current, registry

__all__ = ["TenantFilterMissing", "install_tenant_filter"]

# Internal alias for the four statement shapes the hook handles. Keeps
# the helper signatures readable without truncating to ``Any``.
type _FilterableStmt = (
    Select[tuple[object, ...]] | CompoundSelect[tuple[object, ...]] | Update | Delete
)

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
    if not isinstance(stmt, Select | CompoundSelect | Update | Delete):
        return

    # Bail before walking: ``tenant_agnostic()`` is the single escape hatch
    # and must skip every code path — including the recursive subquery
    # rewriter that can otherwise fail closed on legitimate cross-tenant
    # reads.
    if current.is_tenant_agnostic():
        return

    # Top-level scoped FROMs (Update/Delete target, or a scoped table in
    # the outer Select's FROM list). The recursive pass below handles
    # scoped tables in WHERE-clause subqueries / CTEs / UNION legs.
    # CompoundSelect has no top-level FROM of its own — the legs do —
    # so its top-level target list is always empty.
    top_level_targets = (
        _collect_top_level_scoped_targets(stmt)
        if isinstance(stmt, Select | Update | Delete)
        else []
    )

    ctx = current.get_current()
    workspace_id = ctx.workspace_id if ctx is not None else None

    if isinstance(stmt, Select | CompoundSelect):
        # Single AST walk. Visits every inner :class:`Select` (including
        # the outer when ``stmt`` is itself a Select). When ``ctx`` is
        # set, the visitor mutates each scoped Select's
        # ``_where_criteria`` in place on the cloned tree; when ``ctx``
        # is :data:`None`, it only collects offender names so the
        # caller can fail closed. Either way the walker has to descend
        # the tree once to know whether the statement touches a scoped
        # table at all, so folding the discovery and mutation into one
        # pass is strictly cheaper than the previous two-pass shape.
        rewritten, inner_offenders = _walk_and_filter(stmt, workspace_id)
        if not top_level_targets and not inner_offenders:
            return
        if workspace_id is None:
            # Deterministic offender so error messages don't depend on
            # walker iteration order.
            offender = sorted(
                [_target_name(t) for t in top_level_targets] + inner_offenders
            )[0]
            raise TenantFilterMissing(table=offender)
        # Outer Select (when present) was filtered as part of the walk,
        # so no separate ``.where(...)`` is needed here.
        orm_execute_state.statement = rewritten
        return

    # Update / Delete: ``cloned_traverse`` clones the Update target
    # alias, which then no longer shares column identity with the
    # original ``stmt.table.c.*`` we use for the top-level filter —
    # running it unconditionally produces a malformed
    # ``UPDATE foo AS foo_1 ... FROM foo AS foo_1`` when the target is
    # :func:`aliased`. Discover offenders without mutating first; only
    # descend a second time if there's actually an inner Select to
    # rewrite.
    _, inner_offenders = _walk_and_filter(stmt, workspace_id=None)
    if not top_level_targets and not inner_offenders:
        return
    if workspace_id is None:
        offender = sorted(
            [_target_name(t) for t in top_level_targets] + inner_offenders
        )[0]
        raise TenantFilterMissing(table=offender)
    rewritten_ud: Update | Delete = stmt
    if inner_offenders:
        walked, _ = _walk_and_filter(stmt, workspace_id)
        assert isinstance(walked, Update | Delete)
        rewritten_ud = walked
    for target in top_level_targets:
        # ``target.c.workspace_id`` resolves to the aliased column
        # when ``target`` is an :class:`Alias`, and to the bare
        # column when ``target`` is a :class:`Table`. Using the
        # target's own column avoids the bug where filtering a
        # query built on ``aliased(...)`` would add a second
        # bare-table FROM element (cartesian product).
        rewritten_ud = rewritten_ud.where(target.c.workspace_id == workspace_id)
    orm_execute_state.statement = rewritten_ud


def _walk_and_filter(
    stmt: _FilterableStmt,
    workspace_id: str | None,
) -> tuple[_FilterableStmt, list[str]]:
    """Walk ``stmt``'s AST once, collect offenders, optionally inject filters.

    Single-pass replacement for the previous two-helper split. Visits
    every reachable :class:`Select` via :func:`cloned_traverse` (the
    only walker that reliably descends into ``Subquery.element`` and
    ``CompoundSelect.selects`` across SQLAlchemy minor versions —
    plain ``traverse`` double-visits inner selects under a Subquery).

    For each Select whose top-level FROM list contains a scoped table
    or alias:

    * the target's name is recorded in the returned list (used by the
      caller for deterministic :class:`TenantFilterMissing` messages
      and for the Update/Delete branch's "do I need to rewrite?"
      gate); and
    * if ``workspace_id`` is not :data:`None`, the cloned node's
      ``_where_criteria`` is extended with
      ``target.c.workspace_id == workspace_id`` for each scoped
      target. ``cloned_traverse`` discards visitor return values and
      expects in-place mutation, so we touch ``_where_criteria``
      directly rather than chaining ``.where()``.

    The outer node is included when ``stmt`` is itself a Select —
    callers therefore don't need a separate top-level ``.where(...)``
    for that shape. Update/Delete callers still apply their own
    top-level filter because the Update/Delete node isn't a Select
    and the visitor never fires on it.

    The original ``stmt`` is never mutated; the returned tree is a
    deep clone (important — ``Subquery`` objects are shared and
    cached, so mutating in place would leak predicates across
    queries). The clone is produced even when ``workspace_id`` is
    :data:`None`; the caller throws it away in that case.
    """
    found: list[str] = []

    def _visit_select(node: Select[tuple[object, ...]]) -> None:
        targets = _collect_top_level_scoped_targets(node)
        if not targets:
            return
        for t in targets:
            found.append(_target_name(t))
        if workspace_id is None:
            return
        added = tuple(t.c.workspace_id == workspace_id for t in targets)
        # ``_where_criteria`` is a tuple of clauses combined with AND
        # at compile time. Appending is the in-place equivalent of
        # ``.where(...)``; we cannot use ``.where()`` here because
        # cloned_traverse expects mutation, not a return value.
        node._where_criteria = node._where_criteria + added

    rewritten = cloned_traverse(stmt, {}, {"select": _visit_select})
    # ``cloned_traverse`` returns the same shape it received (mypy
    # widens to :class:`ExternallyTraversible`); the runtime check
    # narrows back to the four shapes the hook handles.
    assert isinstance(rewritten, Select | CompoundSelect | Update | Delete)
    return rewritten, found


def _target_name(target: Table | Alias) -> str:
    """Return the underlying table name for a walked FROM target."""
    if isinstance(target, Table):
        return target.name
    element = target.element
    if isinstance(element, Table):
        return element.name
    # Defensive fallback — walker only ever hands back Table or Alias-of-Table.
    return getattr(element, "name", "<unknown>")


def _collect_top_level_scoped_targets(
    stmt: Select[tuple[object, ...]] | Update | Delete,
) -> list[Table | Alias]:
    """Return scoped FROM targets at the top level of ``stmt``.

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
    side is filtered on **both** sides). Subqueries / CTEs that wrap a
    :class:`Select` are *skipped* here — the recursive rewriter will
    descend into their inner Select and inject the predicate there.
    Subqueries / CTEs wrapping a non-Select selectable (e.g. raw
    ``text()``) that hide a scoped table cannot be rewritten and raise
    :class:`TenantFilterMissing`.

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
    cross-workspace self-join slips through.

    Subquery / CTE nodes are *skipped* at this level: the caller's
    recursive rewriter (:func:`_rewrite_inner_selects`) descends into
    their inner :class:`Select` separately and injects the predicate
    there. The fail-closed path for opaque (non-Select) inner
    selectables lives on the Subquery branch below.

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
        # Subqueries / CTEs that wrap a Select are handled by the
        # recursive rewriter — the inner Select gets visited and
        # filtered separately. Subqueries that wrap something the
        # rewriter can't reach into (e.g. a raw ``text()``-based
        # selectable) and that hide a scoped table reference fail
        # closed: we have no AST to inject a predicate into without
        # reshaping the query, and silently passing through would
        # leak rows.
        inner = node.element
        if not isinstance(inner, Select):
            offender = _first_scoped_name(inner)
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
        # Alias wrapping a selectable (subquery-as-alias): if the
        # wrapped element is a Select it'll be handled by the
        # recursive rewriter. Otherwise fall back to a structural walk
        # so the fail-closed path still trips on opaque wrappers.
        if not isinstance(element, Select):
            _walk_from(element, collected, seen)
        return
    # Anything else (Values, etc.): walk one element deep if it has an
    # ``element`` attribute so odd wrappers around scoped tables are
    # still caught. Leave unrecognised shapes alone.
    if isinstance(node, FromClause):
        inner_attr = getattr(node, "element", None)
        if inner_attr is not None and inner_attr is not node:
            _walk_from(inner_attr, collected, seen)


def _first_scoped_name(node: object) -> str:
    """Recursively find the first scoped table name reachable from ``node``.

    Used only by the fail-closed branch of :func:`_walk_from` when an
    opaque wrapper (Subquery / CTE around a non-Select selectable, or a
    similar shape) hides a scoped reference the rewriter cannot reach.
    Returns ``""`` when nothing scoped is found.
    """
    if isinstance(node, Table):
        return node.name if registry.is_scoped(node.name) else ""
    if isinstance(node, Join):
        left = _first_scoped_name(node.left)
        if left:
            return left
        return _first_scoped_name(node.right)
    if isinstance(node, Subquery | CTE):
        return _first_scoped_name(node.element)
    inner_attr = getattr(node, "element", None)
    if inner_attr is not None and inner_attr is not node:
        return _first_scoped_name(inner_attr)
    return ""
