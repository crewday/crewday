"""Auto-inject ``AND workspace_id = :current_workspace_id`` on every query.

SQLAlchemy ``do_orm_execute`` hook. For each ORM ``execute()`` that ships a
``Select``, ``Update``, or ``Delete`` against a workspace-scoped table, this
module rewrites the statement to pin ``workspace_id`` to the active
:class:`~app.tenancy.context.WorkspaceContext`. Developers write queries
normally; the filter is never threaded by hand.

Two flavours of scoped table are supported (see
:mod:`app.tenancy.registry`):

* **Plain workspace-column tables** — registered via
  :func:`~app.tenancy.registry.register`. The hook injects a literal
  ``<table>.workspace_id = :ctx_workspace`` predicate.
* **Scope-through-join tables** — registered via
  :func:`~app.tenancy.registry.register_scope_through_join`. These
  tables (``unit`` / ``area`` / ``property_closure``) reach the
  workspace boundary through a junction (``property_workspace``).
  The hook either verifies the caller already joined the junction
  with the right ``workspace_id`` predicate or auto-injects an
  ``IN (SELECT <remote> FROM <via> WHERE workspace_id = :ctx)``
  filter so naive callers stay safe by default.

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
* ``Update`` / ``Delete`` against a **scope-through-join** table fail
  closed: the Update/Delete branch only knows how to chain a local
  ``.where(target.c.workspace_id == ctx)`` and grafting a junction
  join onto a bulk DML changes the statement shape in ways the
  rewriter cannot prove safe across SQLAlchemy minor versions.
  Callers thread the predicate by hand or wrap the block in
  ``tenant_agnostic()``.

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
``docs/specs/15-security-privacy.md`` §"Row-level security".
"""

from __future__ import annotations

import weakref

from sqlalchemy import Delete, Select, Table, Update, event, select
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, ColumnElement
from sqlalchemy.sql.operators import eq
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
from app.tenancy.registry import ScopeThroughJoin

__all__ = ["TenantFilterMissing", "install_tenant_filter"]

# Internal alias for the four statement shapes the hook handles. Keeps
# the helper signatures readable without truncating to ``Any``.
type _FilterableStmt = (
    Select[tuple[object, ...]] | CompoundSelect[tuple[object, ...]] | Update | Delete
)

# A scoped FROM target paired with its scoping spec. ``spec=None`` means
# the table carries a literal ``workspace_id`` column; a non-None spec
# means the table reaches the boundary through ``spec.via_table``.
type _ScopedTarget = tuple[Table | Alias, ScopeThroughJoin | None]

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

    **Production wiring installs the hook on a Session subclass, not
    a sessionmaker.** :class:`app.adapters.db.session.FilteredSession`
    receives this call exactly once at module import; the production
    ``sessionmaker(class_=FilteredSession, ...)`` then inherits the
    listener via the class dispatch. This immunises production
    against the cd-nf8p heisenbug, where building multiple
    sessionmakers against the same engine could leave a fresh
    session's ``do_orm_execute`` list empty even when
    :func:`sqlalchemy.event.contains` reported the listener as
    attached. Tests that build their own per-fixture sessionmaker
    can still pass it here — the function works on either target —
    but new code paths should prefer the class-level install.

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
                [_target_name(t) for t, _ in top_level_targets] + inner_offenders
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

    # Scope-through-join tables can't be safely UPDATE/DELETE'd through
    # this hook — see module docstring. Fail closed regardless of
    # whether ``ctx`` is set, matching the "no AST to inject into"
    # behaviour for opaque subqueries: callers either thread the
    # predicate by hand or wrap in ``tenant_agnostic``.
    join_scoped_top = [target for target, spec in top_level_targets if spec is not None]
    if join_scoped_top:
        offender = sorted(_target_name(t) for t in join_scoped_top)[0]
        raise TenantFilterMissing(table=offender)

    if workspace_id is None:
        offender = sorted(
            [_target_name(t) for t, _ in top_level_targets] + inner_offenders
        )[0]
        raise TenantFilterMissing(table=offender)
    rewritten_ud: Update | Delete = stmt
    if inner_offenders:
        walked, _ = _walk_and_filter(stmt, workspace_id)
        assert isinstance(walked, Update | Delete)
        rewritten_ud = walked
    for target, _spec in top_level_targets:
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

    For each Select whose top-level FROM list contains a scoped target
    (plain or scope-through-join):

    * the target's name is recorded in the returned list (used by the
      caller for deterministic :class:`TenantFilterMissing` messages
      and for the Update/Delete branch's "do I need to rewrite?"
      gate); and
    * if ``workspace_id`` is not :data:`None`, the cloned node's
      ``_where_criteria`` is extended with the right predicate per
      target. Plain targets get ``target.c.workspace_id ==
      workspace_id``; scope-through-join targets get either nothing
      (when the caller already joined the junction with a matching
      workspace predicate) or
      ``target.<via_local_column> IN (SELECT <via_remote_column>
      FROM <via_table> WHERE workspace_id = :ctx_workspace)``.
      ``cloned_traverse`` discards visitor return values and expects
      in-place mutation, so we touch ``_where_criteria`` directly
      rather than chaining ``.where()``.

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
        for t, _ in targets:
            found.append(_target_name(t))
        if workspace_id is None:
            return
        added: list[ColumnElement[bool]] = []
        for target, spec in targets:
            if spec is None:
                added.append(target.c.workspace_id == workspace_id)
                continue
            if _select_has_matching_via_join(node, target, spec, workspace_id):
                # Caller already routed through the junction with a
                # matching ``workspace_id`` predicate; injecting the
                # ``IN (SELECT ...)`` filter on top would be a duplicate
                # constraint and risk a planner regression on Postgres.
                continue
            added.append(_join_scope_predicate(target, spec, workspace_id))
        # ``_where_criteria`` is a tuple of clauses combined with AND
        # at compile time. Appending is the in-place equivalent of
        # ``.where(...)``; we cannot use ``.where()`` here because
        # cloned_traverse expects mutation, not a return value.
        node._where_criteria = node._where_criteria + tuple(added)

    rewritten = cloned_traverse(stmt, {}, {"select": _visit_select})
    # ``cloned_traverse`` returns the same shape it received (mypy
    # widens to :class:`ExternallyTraversible`); the runtime check
    # narrows back to the four shapes the hook handles.
    assert isinstance(rewritten, Select | CompoundSelect | Update | Delete)
    return rewritten, found


def _join_scope_predicate(
    target: Table | Alias,
    spec: ScopeThroughJoin,
    workspace_id: str,
) -> ColumnElement[bool]:
    """Build the auto-injected ``IN (SELECT ...)`` predicate.

    Compiles to ``target.<local> IN (SELECT <remote> FROM <via>
    WHERE workspace_id = :ctx)``. Resolves the junction
    :class:`~sqlalchemy.Table` lazily through the scoped target's
    :attr:`Table.metadata`; both tables are typically declared on
    the same :class:`~sqlalchemy.MetaData` (the application's
    :class:`~sqlalchemy.orm.DeclarativeBase`). Falls back to a
    fail-closed :class:`TenantFilterMissing` when the junction
    can't be resolved — a misregistration shouldn't silently
    produce an unfiltered query.
    """
    via = _resolve_via_table(target, spec)
    local_col = target.c[spec.via_local_column]
    remote_col = via.c[spec.via_remote_column]
    workspace_col = via.c[spec.workspace_column]
    sub = select(remote_col).where(workspace_col == workspace_id).scalar_subquery()
    return local_col.in_(sub)


def _resolve_via_table(
    target: Table | Alias,
    spec: ScopeThroughJoin,
) -> Table:
    """Look up the junction :class:`Table` from the target's metadata."""
    base: Table
    if isinstance(target, Table):
        base = target
    else:
        element = target.element
        if not isinstance(element, Table):
            raise TenantFilterMissing(table=spec.via_table)
        base = element
    via = base.metadata.tables.get(spec.via_table)
    if via is None:
        raise TenantFilterMissing(table=spec.via_table)
    return via


def _select_has_matching_via_join(
    node: Select[tuple[object, ...]],
    target: Table | Alias,
    spec: ScopeThroughJoin,
    workspace_id: str,
) -> bool:
    """Return ``True`` iff ``node`` already binds the junction safely.

    The caller is opting into manual scope-through-join handling. We
    accept their query if **all** of:

    1. ``node`` **inner-**joins ``spec.via_table`` (or an alias of it)
       to ``target.<via_local_column>`` via
       ``target.<via_local_column> == via.<via_remote_column>``. Outer
       joins are rejected — a ``LEFT OUTER JOIN`` with the workspace
       predicate in the ON clause does not enforce the boundary
       (rows with no matching junction row come through as NULLs);
    2. ``node._where_criteria`` (or the join's own ON clause) carries
       a ``via.<workspace_column> == <bind>`` predicate where ``<bind>``
       is a :class:`BindParameter` whose value equals the active
       ``workspace_id``.

    A static literal that names a *different* workspace must NOT bypass
    the rewriter — unlike the plain workspace-column path, where the
    rewriter still appends ``AND target.workspace_id = :ctx`` on top of
    the caller's predicate (so a malicious literal returns zero rows),
    the scope-through-join path *replaces* its own injection when the
    detector matches. Trusting the bind value blindly would let a
    caller write ``where(PW.workspace_id == "OTHER_WS")`` and read
    rows that belong to ``"OTHER_WS"`` instead of the active context.
    Validating the bind closes that hole; non-bind expressions (other
    columns, function calls, expressions) fall back to auto-injection.
    """
    via_aliases = list(_iter_via_table_aliases(node, spec.via_table))
    if not via_aliases:
        return False

    target_local = target.c[spec.via_local_column]
    matched_via: list[FromClause] = []
    for via in via_aliases:
        remote = via.c[spec.via_remote_column]
        if _select_has_inner_join_equality(node, target_local, remote):
            matched_via.append(via)

    if not matched_via:
        return False

    # The matched via must also carry a ``workspace_id == :ctx``
    # predicate somewhere reachable from this Select's WHERE clause or
    # the inner-join's own ON clause. The bind value must equal the
    # active ``workspace_id``; a literal naming any other workspace
    # falls back to auto-injection.
    for via in matched_via:
        workspace_col = via.c[spec.workspace_column]
        if _select_pins_workspace_to(node, workspace_col, workspace_id):
            return True
    return False


def _iter_via_table_aliases(
    node: Select[tuple[object, ...]],
    via_table_name: str,
) -> list[FromClause]:
    """Return every :class:`Table` / :class:`Alias` of ``via_table_name`` in ``node``.

    Walks the same pre-compile FROM sources we use elsewhere
    (``_raw_columns`` / ``_from_obj`` / ``_setup_joins``) plus the
    legs of any :class:`Join` reachable from those sources. Aliases
    are returned wholesale so the caller can match the join predicate
    against the alias's own columns.
    """
    found: list[FromClause] = []
    seen: set[int] = set()

    def _consider(node_: object) -> None:
        if isinstance(node_, Table):
            if node_.name == via_table_name and id(node_) not in seen:
                seen.add(id(node_))
                found.append(node_)
            return
        if isinstance(node_, Alias):
            element = node_.element
            if (
                isinstance(element, Table)
                and element.name == via_table_name
                and id(node_) not in seen
            ):
                seen.add(id(node_))
                found.append(node_)
            return
        if isinstance(node_, Join):
            _consider(node_.left)
            _consider(node_.right)
            return

    for source in _iter_select_from_sources(node):
        _consider(source)
    return found


def _select_has_inner_join_equality(
    node: Select[tuple[object, ...]],
    left_col: ColumnElement[object],
    right_col: ColumnElement[object],
) -> bool:
    """Return ``True`` iff ``node`` binds the columns through an **inner** join.

    Searches:

    * the ON clauses of every ``_setup_joins`` entry whose ``isouter``
      flag is :data:`False`,
    * every :class:`Join` reachable from ``_from_obj`` whose own
      ``isouter`` flag is :data:`False` (a join nested inside a LEFT
      OUTER JOIN is treated as inner-join only when the outer wrapper
      is not in the path between them; the recursive walker checks at
      each level),
    * the WHERE clause itself (an old-style cartesian + filter
      ``select(A, B).where(A.x == B.y)`` is semantically inner).

    Outer joins are excluded because a workspace predicate on the
    outer side of a ``LEFT OUTER JOIN`` does not enforce the boundary —
    rows with no matching junction row still come through. Equality is
    matched lineage-aware (annotation wrappers tunnel through).
    """
    targets = (left_col, right_col)
    for setup_join in node._setup_joins:
        onclause = setup_join[1]
        opts = setup_join[3] if len(setup_join) >= 4 else None
        is_outer = bool(opts.get("isouter")) if isinstance(opts, dict) else False
        if (
            not is_outer
            and onclause is not None
            and _binary_eq_binds(onclause, *targets)
        ):
            return True
    for from_obj in node._from_obj:
        if _inner_join_onclause_binds(from_obj, *targets):
            return True
    return any(_binary_eq_binds(clause, *targets) for clause in node._where_criteria)


def _select_pins_workspace_to(
    node: Select[tuple[object, ...]],
    workspace_col: ColumnElement[object],
    workspace_id: str,
) -> bool:
    """Return ``True`` iff a ``workspace_col == :workspace_id`` predicate is reachable.

    Same search domain as :func:`_select_has_inner_join_equality` —
    WHERE clauses + non-outer setup_join ON clauses + non-outer
    :class:`Join` ON clauses. The ``workspace_id`` value must appear as
    a :class:`BindParameter` (or a callable bind that returns it);
    plain literals naming a different workspace, expressions, and
    column-to-column equalities all return :data:`False` so the
    rewriter falls back to auto-injection. This is a strict mirror of
    the plain workspace-column path's behaviour: there, the rewriter
    *appends* its own predicate so a malicious literal returns zero
    rows; here, the rewriter *skips* its predicate when the detector
    matches, so we cannot trust an unverified literal.
    """
    for clause in node._where_criteria:
        if _binary_eq_to_bind_value(clause, workspace_col, workspace_id):
            return True
    for setup_join in node._setup_joins:
        onclause = setup_join[1]
        opts = setup_join[3] if len(setup_join) >= 4 else None
        is_outer = bool(opts.get("isouter")) if isinstance(opts, dict) else False
        if (
            not is_outer
            and onclause is not None
            and _binary_eq_to_bind_value(onclause, workspace_col, workspace_id)
        ):
            return True
    for from_obj in node._from_obj:
        if _inner_join_onclause_eq_to_bind_value(from_obj, workspace_col, workspace_id):
            return True
    return False


def _binary_eq_binds(
    clause: object,
    a: ColumnElement[object],
    b: ColumnElement[object],
) -> bool:
    """Return ``True`` iff ``clause`` matches ``a == b`` (in any AND'd subclause)."""
    if isinstance(clause, BinaryExpression) and clause.operator is eq:
        sides = (clause.left, clause.right)
        if any(_columns_match(a, s) for s in sides) and any(
            _columns_match(b, s) for s in sides
        ):
            return True
    # Walk AND'd compound clauses — SQLAlchemy folds
    # ``where(a, b)`` into a :class:`BooleanClauseList` for us, but
    # ``where(and_(a, b))`` lands as a single composite element here.
    sub_clauses = getattr(clause, "clauses", None)
    if sub_clauses is not None:
        return any(_binary_eq_binds(c, a, b) for c in sub_clauses)
    return False


def _binary_eq_to_bind_value(
    clause: object,
    col: ColumnElement[object],
    expected_value: str,
) -> bool:
    """``True`` iff any AND'd subclause is ``col == <bind=expected_value>``.

    Strict bind-value match — the rewriter skips its own predicate when
    the detector matches, so we cannot accept an unverified literal.
    Returns :data:`False` for column-to-column equalities, expressions,
    function calls, and binds whose value (or callable result) does
    not equal ``expected_value``.
    """
    if isinstance(clause, BinaryExpression) and clause.operator is eq:
        for col_side, val_side in (
            (clause.left, clause.right),
            (clause.right, clause.left),
        ):
            if _columns_match(col_side, col) and _bind_equals(val_side, expected_value):
                return True
    sub_clauses = getattr(clause, "clauses", None)
    if sub_clauses is not None:
        return any(
            _binary_eq_to_bind_value(c, col, expected_value) for c in sub_clauses
        )
    return False


def _bind_equals(node: object, expected_value: str) -> bool:
    """``True`` iff ``node`` is a bind resolving to ``expected_value``.

    SQLAlchemy stores the literal directly on ``BindParameter.value``;

    a deferred bind exposes a callable on ``.callable`` that yields the
    value at render time. Anything else (a column, an expression, a
    function call) returns :data:`False` so the detector falls back to
    auto-injection — defence-in-depth dominates the rare cost of an
    extra ``IN (SELECT …)`` predicate.
    """
    if not isinstance(node, BindParameter):
        return False
    callable_ = getattr(node, "callable", None)
    if callable_ is not None:
        try:
            value = callable_()
        except Exception:
            return False
        return bool(value == expected_value)
    return bool(node.value == expected_value)


def _columns_match(a: object, b: object) -> bool:
    """Lineage-aware column identity check for the join detector.

    SQLAlchemy decorates ORM-built joins with :class:`AnnotatedColumn`
    wrappers that don't compare ``is``-equal to the bare
    :class:`~sqlalchemy.Column` we hand around the rewriter, so plain
    identity gives false negatives on the most common spelling
    (``select(Foo).join(Bar, Foo.x == Bar.y)``). ``shares_lineage``
    is the supported way to ask "do these two column elements
    ultimately refer to the same underlying :class:`Column`?" — it
    tunnels through annotations, aliases, and label proxies.

    Falls back to ``is`` for non-ColumnElement nodes (e.g. when a
    visitor hands us a literal :class:`BindParameter` on the right
    side of an equality).
    """
    if isinstance(a, ColumnElement) and isinstance(b, ColumnElement):
        return a.shares_lineage(b)
    return a is b


def _inner_join_onclause_binds(
    node: object,
    a: ColumnElement[object],
    b: ColumnElement[object],
) -> bool:
    """Recursively check inner-:class:`Join` ON clauses under ``node`` for ``a == b``.

    Outer-join nodes are short-circuited — the workspace boundary they
    advertise in their own ON clause does not protect rows on the
    outer side. Walks left/right children regardless so a nested inner
    join under an outer join can still match (the outer wrapper
    excludes that path's own ON clause from consideration but doesn't
    poison its children).
    """
    if isinstance(node, Join):
        if not node.isouter and _binary_eq_binds(node.onclause, a, b):
            return True
        return _inner_join_onclause_binds(
            node.left, a, b
        ) or _inner_join_onclause_binds(node.right, a, b)
    return False


def _inner_join_onclause_eq_to_bind_value(
    node: object,
    col: ColumnElement[object],
    expected_value: str,
) -> bool:
    """Recursively check inner-:class:`Join` ON clauses for ``col == :expected_value``.

    Same outer-join short-circuit as :func:`_inner_join_onclause_binds`.
    """
    if isinstance(node, Join):
        if not node.isouter and _binary_eq_to_bind_value(
            node.onclause, col, expected_value
        ):
            return True
        return _inner_join_onclause_eq_to_bind_value(
            node.left, col, expected_value
        ) or _inner_join_onclause_eq_to_bind_value(node.right, col, expected_value)
    return False


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
) -> list[_ScopedTarget]:
    """Return scoped FROM targets at the top level of ``stmt``.

    Targets are ``(Table | Alias, ScopeThroughJoin | None)`` pairs;
    a ``None`` spec means the table carries a literal ``workspace_id``
    column, while a non-None spec means the table reaches the
    workspace boundary through a junction (and the caller injects the
    appropriate ``IN (SELECT ...)`` predicate against the junction).

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
        if isinstance(target, Table):
            spec = registry.get_scope_through_join(target.name)
            if registry.is_scoped(target.name):
                return [(target, spec)]
        if isinstance(target, Alias):
            element = target.element
            if isinstance(element, Table) and registry.is_scoped(element.name):
                spec = registry.get_scope_through_join(element.name)
                return [(target, spec)]
        return []

    # stmt is a Select. Gather the top-level FROM sources without
    # triggering a compile. ``_raw_columns`` + ``_from_obj`` +
    # ``_setup_joins`` is what the ORM itself feeds to the compiler;
    # walking them directly is stable across test orderings because
    # the cached annotations on ``Select.get_final_froms()`` are what
    # vary (cd-3yhd).
    collected: list[_ScopedTarget] = []
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
    collected: list[_ScopedTarget],
    seen: set[int],
) -> None:
    """Depth-walk a FROM element and append every scoped target to ``collected``.

    ``seen`` deduplicates by **object identity** (``id(node)``), not by
    ``Table.name``: a self-join with an ``aliased(Model)`` right side
    shares the underlying ``Table.name`` with the left but is a distinct
    :class:`Alias` object — both sides need a workspace filter, else a
    cross-workspace self-join slips through.

    Subquery / CTE nodes are *skipped* at this level: the caller's
    recursive rewriter (:func:`_walk_and_filter`) descends into
    their inner :class:`Select` separately and injects the predicate
    there. The fail-closed path for opaque (non-Select) inner
    selectables lives on the Subquery branch below.

    Still handles :class:`Join` in case a consumer ever hands us a
    pre-joined FROM element, even though :func:`_iter_select_from_sources`
    returns already-unjoined sources today.
    """
    if isinstance(node, Table):
        if registry.is_scoped(node.name) and id(node) not in seen:
            spec = registry.get_scope_through_join(node.name)
            collected.append((node, spec))
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
                spec = registry.get_scope_through_join(element.name)
                collected.append((node, spec))
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
