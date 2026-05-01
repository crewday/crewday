"""Tests for :mod:`app.tenancy.orm_filter`.

Exercise the :class:`~sqlalchemy.orm.SessionEvents.do_orm_execute` hook
against an in-memory SQLite engine. The hook must:

* raise :class:`TenantFilterMissing` when a scoped-table query runs
  without a :class:`WorkspaceContext` and outside
  :func:`tenant_agnostic`,
* inject ``table.workspace_id = :workspace_id`` on Select / Update /
  Delete when a context is active,
* skip injection inside :func:`tenant_agnostic`,
* leave non-scoped tables alone,
* only filter the scoped side of a join.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import (
    CursorResult,
    Engine,
    ForeignKey,
    Integer,
    String,
    delete,
    event,
    select,
    update,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    aliased,
    mapped_column,
    sessionmaker,
)

from app.adapters.db.session import make_engine
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import (
    reset_current,
    set_current,
    tenant_agnostic,
)
from app.tenancy.orm_filter import TenantFilterMissing, install_tenant_filter


class _Base(DeclarativeBase):
    """Test-local declarative base — isolated from app metadata."""


class _Foo(_Base):
    """Workspace-scoped model; ``foo`` is registered with the tenancy registry."""

    __tablename__ = "foo"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)


class _Bar(_Base):
    """Non-scoped model; ``bar`` is deliberately NOT registered."""

    __tablename__ = "bar"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False)


class _FooWithBar(_Base):
    """Scoped model with an FK to non-scoped ``bar`` — used for join tests."""

    __tablename__ = "foo_with_bar"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    bar_id: Mapped[str] = mapped_column(ForeignKey("bar.id"), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


_CTX_A = WorkspaceContext(
    workspace_id="01HWA00000000000000000WSPA",
    workspace_slug="workspace-a",
    actor_id="01HWA00000000000000000USRA",
    actor_kind="user",
    actor_grant_role="manager",
    actor_was_owner_member=True,
    audit_correlation_id="01HWA00000000000000000CRLA",
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    _Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def _register_scoped_tables() -> Iterator[None]:
    """Register scoped tables for each test; clear after so cases are isolated."""
    registry._reset_for_tests()
    registry.register("foo")
    registry.register("foo_with_bar")
    try:
        yield
    finally:
        registry._reset_for_tests()


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Guarantee each test starts with no active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture
def sql_capture(engine: Engine) -> Iterator[list[str]]:
    """Capture the rendered SQL strings sent to the DB cursor."""
    captured: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        captured.append(statement)

    yield captured

    event.remove(engine, "before_cursor_execute", _capture)


# -- Select ---------------------------------------------------------------


def test_select_scoped_without_ctx_raises(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, pytest.raises(TenantFilterMissing) as exc:
        session.execute(select(_Foo))
    assert exc.value.table == "foo"


def test_select_scoped_with_ctx_injects_filter(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            session.execute(select(_Foo))
    finally:
        reset_current(token)

    assert sql_capture, "expected at least one cursor execution"
    # Collapse whitespace so the assertion is stable across SQLAlchemy
    # minor version tweaks to its compiled-SQL formatting.
    flattened = " ".join(sql_capture[-1].split())
    assert "foo.workspace_id = ?" in flattened
    assert "FROM foo" in flattened


def test_select_scoped_inside_tenant_agnostic_skips_filter(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    # justification: cross-tenant scan in a unit test
    with session_factory() as session, tenant_agnostic():
        session.execute(select(_Foo))

    assert sql_capture, "expected at least one cursor execution"
    flattened = " ".join(sql_capture[-1].split())
    # ``workspace_id`` appears in the SELECT list naturally — the assertion
    # is that no ``WHERE ... workspace_id = ?`` was appended.
    assert "WHERE" not in flattened.upper()


def test_select_non_scoped_is_untouched(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    # No ctx, no agnostic, but ``bar`` isn't scoped so the hook must not
    # raise and must not add any workspace filter.
    with session_factory() as session:
        session.execute(select(_Bar))

    flattened = " ".join(sql_capture[-1].split())
    assert "workspace_id" not in flattened
    assert "WHERE" not in flattened.upper()


def test_join_scoped_and_non_scoped_filters_scoped_only(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    import re

    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            session.execute(select(_FooWithBar).join(_Bar))
    finally:
        reset_current(token)

    flattened = " ".join(sql_capture[-1].split())
    # Scoped side gets the filter.
    assert "foo_with_bar.workspace_id = ?" in flattened
    # Non-scoped side does not. Word-boundary regex guards against the
    # ``foo_with_bar.workspace_id`` substring also matching ``bar.``.
    assert re.search(r"(?<!_)\bbar\.workspace_id", flattened) is None


def test_join_two_scoped_tables_filters_both(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    # Register both scoped tables for this case.
    registry.register("foo")
    registry.register("foo_with_bar")
    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            session.execute(
                select(_Foo, _FooWithBar).join_from(
                    _Foo, _FooWithBar, _Foo.id == _FooWithBar.id
                )
            )
    finally:
        reset_current(token)

    flattened = " ".join(sql_capture[-1].split())
    assert "foo.workspace_id = ?" in flattened
    assert "foo_with_bar.workspace_id = ?" in flattened


# -- Update ---------------------------------------------------------------


def test_update_scoped_without_ctx_raises(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, pytest.raises(TenantFilterMissing) as exc:
        session.execute(update(_Foo).values(name="x"))
    assert exc.value.table == "foo"


def test_update_scoped_with_ctx_injects_filter(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            result = session.execute(update(_Foo).values(name="x"))
            assert isinstance(result, CursorResult)
    finally:
        reset_current(token)

    flattened = " ".join(sql_capture[-1].split())
    assert "foo.workspace_id = ?" in flattened or "workspace_id = ?" in flattened
    # Sanity: the UPDATE verb is actually what's on the wire.
    assert "UPDATE" in flattened.upper()


def test_update_scoped_inside_tenant_agnostic_skips_filter(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    # justification: deployment-admin bulk rename in a unit test
    with session_factory() as session, tenant_agnostic():
        session.execute(update(_Foo).values(name="x"))

    flattened = " ".join(sql_capture[-1].split())
    # No WHERE clause means no tenant filter got injected. A bulk UPDATE
    # without WHERE is intentional here under agnostic scope.
    assert "WHERE" not in flattened.upper()


# -- Delete ---------------------------------------------------------------


def test_delete_scoped_without_ctx_raises(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session, pytest.raises(TenantFilterMissing) as exc:
        session.execute(delete(_Foo))
    assert exc.value.table == "foo"


def test_delete_scoped_with_ctx_injects_filter(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            session.execute(delete(_Foo))
    finally:
        reset_current(token)

    flattened = " ".join(sql_capture[-1].split())
    assert "workspace_id = ?" in flattened
    assert "DELETE" in flattened.upper()


def test_delete_scoped_inside_tenant_agnostic_skips_filter(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    # justification: deployment-admin purge in a unit test
    with session_factory() as session, tenant_agnostic():
        session.execute(delete(_Foo))

    flattened = " ".join(sql_capture[-1].split())
    assert "WHERE" not in flattened.upper()


# -- Context isolation ----------------------------------------------------


def test_filter_scopes_to_current_workspace(
    session_factory: sessionmaker[Session],
    engine: Engine,
) -> None:
    """Two rows in two workspaces — each ctx reads only its own."""
    other_ctx = WorkspaceContext(
        workspace_id="01HWA00000000000000000WSPB",
        workspace_slug="workspace-b",
        actor_id="01HWA00000000000000000USRB",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id="01HWA00000000000000000CRLB",
    )
    # Seed data across two workspaces via an agnostic raw insert — the
    # inserts bypass the filter because INSERT is out of scope for the
    # rewriter (caller sets workspace_id manually).
    # justification: test seeds rows in two workspaces
    with session_factory() as session, tenant_agnostic():
        session.add(
            _Foo(
                id="01HWA00000000000000000ROW1",
                workspace_id=_CTX_A.workspace_id,
                name="a-row",
            )
        )
        session.add(
            _Foo(
                id="01HWA00000000000000000ROW2",
                workspace_id=other_ctx.workspace_id,
                name="b-row",
            )
        )
        session.commit()

    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            rows = session.scalars(select(_Foo)).all()
            assert [r.name for r in rows] == ["a-row"]
    finally:
        reset_current(token)

    token = set_current(other_ctx)
    try:
        with session_factory() as session:
            rows = session.scalars(select(_Foo)).all()
            assert [r.name for r in rows] == ["b-row"]
    finally:
        reset_current(token)


def test_nested_tenant_agnostic_restores_outer(
    session_factory: sessionmaker[Session],
) -> None:
    """After a ``with tenant_agnostic()`` block exits, the filter is live again."""
    with session_factory() as session:
        # Filter active: no ctx -> raises.
        with pytest.raises(TenantFilterMissing):
            session.execute(select(_Foo))
        # Agnostic scope: no raise.
        # justification: test nested agnostic reverts to filter
        with tenant_agnostic():
            session.execute(select(_Foo))
        # Back outside agnostic — must raise again.
        with pytest.raises(TenantFilterMissing):
            session.execute(select(_Foo))


# -- Subquery fail-closed -------------------------------------------------


def test_subquery_with_scoped_table_fails_closed_without_ctx(
    session_factory: sessionmaker[Session],
) -> None:
    """Scoped table hidden inside a subquery raises without a ctx.

    The rewriter cannot reach into an opaque :class:`Subquery` to inject
    the filter; it fails closed so no unfiltered query escapes. Callers
    who genuinely need a cross-tenant subquery wrap the block in
    :func:`tenant_agnostic`.
    """
    sub = select(_Foo).subquery()
    with session_factory() as session, pytest.raises(TenantFilterMissing) as exc:
        session.execute(select(sub))
    assert exc.value.table == "foo"


def test_install_tenant_filter_is_idempotent(
    engine: Engine,
    sql_capture: list[str],
) -> None:
    """Double-installing the listener must not double the ``WHERE`` clause."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    install_tenant_filter(factory)
    install_tenant_filter(factory)

    token = set_current(_CTX_A)
    try:
        with factory() as session:
            session.execute(select(_Foo))
    finally:
        reset_current(token)

    flattened = " ".join(sql_capture[-1].split())
    # Exactly one filter, not three. Stripping spaces lets us count the
    # substring without caring about surrounding whitespace variations.
    assert flattened.count("foo.workspace_id = ?") == 1


def test_install_tenant_filter_every_fresh_sessionmaker_filters(
    engine: Engine,
) -> None:
    """Regression for cd-3yhd: every fresh :class:`sessionmaker` that passes
    through :func:`install_tenant_filter` must have the listener attached.

    Before the fix, idempotency was checked via
    :func:`sqlalchemy.event.contains`, which keys on ``id(target)``.
    When one ``sessionmaker`` was garbage-collected and a new one was
    allocated at the same address (common in per-test fixtures, cd-3yhd
    flake), the check returned a stale ``True`` and ``install`` silently
    no-op'd — the fresh :class:`Session` executed queries unfiltered.

    We simulate fixture turnovers and assert the listener is present
    on a :class:`Session` spawned from **every** factory — direct
    proof that the guard doesn't false-positive across back-to-back
    allocations. 10 iterations gives >99% address-reuse detection
    probability; more is waste.
    """
    import gc

    from app.tenancy.orm_filter import _do_orm_execute

    for _ in range(10):
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        install_tenant_filter(factory)
        with factory() as session:
            listeners = list(session.dispatch.do_orm_execute)

            assert _do_orm_execute in listeners, (
                "install_tenant_filter must attach the listener to every "
                "freshly-created sessionmaker, including those that land on "
                "a previously-used memory address"
            )
        del factory, session
        gc.collect()


def test_subquery_with_scoped_table_agnostic_escape(
    session_factory: sessionmaker[Session],
) -> None:
    """Inside :func:`tenant_agnostic`, the subquery fail-closed path is bypassed."""
    sub = select(_Foo).subquery()
    # justification: cross-tenant analytics read, unit test
    with session_factory() as session, tenant_agnostic():
        session.execute(select(sub))


# -- Aliased scoped tables -----------------------------------------------


def test_select_aliased_scoped_filters_on_alias_not_base(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    """``select(aliased(Foo))`` filters on the alias, not the base table.

    If the rewriter unwrapped the alias to the bare ``Table`` it would add
    the bare ``foo`` as a second FROM element (cartesian product) and
    leave the aliased side of the caller's query unfiltered. The hook
    must inject ``foo_1.workspace_id`` — the alias's own column.
    """
    alias = aliased(_Foo)
    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            session.execute(select(alias))
    finally:
        reset_current(token)

    flattened = " ".join(sql_capture[-1].split())
    # Alias's own column is filtered.
    assert "foo_1.workspace_id = ?" in flattened
    # Exactly one FROM element (no cartesian with bare ``foo``). SQLite
    # renders the alias as ``FROM foo AS foo_1`` without adding a second
    # ``, foo`` — if the walker wrongly unwrapped, we'd see ``, foo``
    # after the alias and a stray join.
    assert ", foo " not in flattened.lower()
    assert "from foo as foo_1 " in flattened.lower()


def test_self_join_with_alias_filters_both_sides(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    """A self-join with ``aliased(Foo)`` must inject a filter on BOTH sides.

    Dedup by ``Table.name`` would skip the alias (same ``foo`` name) and
    leave the aliased side unfiltered — a cross-workspace self-join
    would then slip through. Dedup by object identity fixes this.
    """
    alias = aliased(_Foo)
    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            session.execute(
                select(_Foo, alias).join_from(_Foo, alias, _Foo.id == alias.id)
            )
    finally:
        reset_current(token)

    flattened = " ".join(sql_capture[-1].split())
    assert "foo.workspace_id = ?" in flattened
    assert "foo_1.workspace_id = ?" in flattened


def test_update_on_alias_of_scoped_table_filters(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    """UPDATE targeting an alias of a scoped table still injects the filter.

    Rare in practice — most UPDATEs hit the bare mapper — but the
    fail-open path here would be a silent cross-tenant write.
    """
    alias = aliased(_Foo)
    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            session.execute(update(alias).values(name="x"))
    finally:
        reset_current(token)

    flattened = " ".join(sql_capture[-1].split())
    assert "workspace_id = ?" in flattened
    assert "UPDATE" in flattened.upper()


# -- WHERE-clause subquery: recursively filtered (cd-fdac) ----------------


def test_where_clause_subquery_scoped_table_is_filtered(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    """Scoped table in a WHERE-clause subquery gets the inner filter injected.

    ``select(Bar).where(Bar.id.in_(select(Foo.id)))`` is non-scoped at
    the outer level (``bar`` isn't registered) but the inner select
    reaches scoped ``foo``. The recursive rewriter (cd-fdac) walks
    every :class:`Select` in the AST and injects
    ``foo.workspace_id = ?`` against the inner select; the outer
    ``bar`` must stay untouched.
    """
    import re

    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            session.execute(select(_Bar).where(_Bar.id.in_(select(_Foo.id))))
    finally:
        reset_current(token)

    flattened = " ".join(sql_capture[-1].split())
    # Inner scoped reference is filtered.
    assert "foo.workspace_id = ?" in flattened
    # Outer non-scoped FROM is left alone — word-boundary regex so the
    # ``foo.workspace_id`` substring above doesn't satisfy ``bar.``.
    assert re.search(r"(?<!_)\bbar\.workspace_id", flattened) is None


def test_where_clause_subquery_without_ctx_raises_with_inner_offender(
    session_factory: sessionmaker[Session],
) -> None:
    """Without a ctx, the inner scoped reference fails closed by name.

    The rewriter doesn't get to run when no :class:`WorkspaceContext`
    is active; the discovery walk still has to find the inner
    ``foo`` and surface it on :class:`TenantFilterMissing`, otherwise
    the developer sees no signal that an unfiltered query was about
    to leak.
    """
    with session_factory() as session, pytest.raises(TenantFilterMissing) as exc:
        session.execute(select(_Bar).where(_Bar.id.in_(select(_Foo.id))))
    assert exc.value.table == "foo"


def test_correlated_exists_subquery_filters_inner_scoped(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    """A correlated EXISTS over a scoped table gets the inner filter.

    Mirrors ``Bar.foos.any(Foo.id == 'x')``: outer is non-scoped,
    correlated inner select reads scoped ``foo``. The inner select's
    own ``_from_obj`` is what the rewriter walks, so the predicate
    lands inside the EXISTS — the outer ``bar`` stays bare.
    """
    import re

    from sqlalchemy import exists

    inner = select(_Foo.id).where(_Foo.id == _Bar.id)
    stmt = select(_Bar).where(exists(inner))

    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            session.execute(stmt)
    finally:
        reset_current(token)

    flattened = " ".join(sql_capture[-1].split())
    assert "foo.workspace_id = ?" in flattened
    assert re.search(r"(?<!_)\bbar\.workspace_id", flattened) is None


def test_union_all_legs_each_get_filter(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    """Both legs of a UNION ALL get the workspace filter.

    The rewriter descends into :class:`CompoundSelect` and visits each
    inner :class:`Select`; the resulting SQL must carry the predicate
    twice (once per leg).
    """
    from sqlalchemy import union_all

    stmt = union_all(
        select(_Foo).where(_Foo.id == "a"),
        select(_Foo).where(_Foo.id == "b"),
    )

    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            session.execute(stmt)
    finally:
        reset_current(token)

    flattened = " ".join(sql_capture[-1].split())
    # Each leg has its own ``foo.workspace_id = ?`` predicate.
    assert flattened.count("foo.workspace_id = ?") == 2


def test_subquery_in_where_inside_tenant_agnostic_skips_filter(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    """``tenant_agnostic()`` still bypasses the recursive rewriter.

    The escape hatch must short-circuit before walking; otherwise a
    legitimate cross-tenant subquery read would have a workspace
    filter injected anyway and silently return zero rows.
    """
    # justification: cross-tenant analytics in a unit test
    with session_factory() as session, tenant_agnostic():
        session.execute(select(_Bar).where(_Bar.id.in_(select(_Foo.id))))

    flattened = " ".join(sql_capture[-1].split())
    assert "workspace_id = ?" not in flattened


def test_select_of_subquery_with_ctx_filters_inner(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    """``select(scoped.subquery())`` with a ctx now filters the inner select.

    Pre-cd-fdac, the rewriter failed closed on a Subquery in the
    top-level FROM regardless of context — even with a workspace
    active, the legitimate read raised. Now the recursive walker
    descends into the subquery's inner :class:`Select` and injects
    the predicate there. The outer Subquery wrapper is left bare,
    which is correct: filtering the inner already constrains the
    rows the wrapper exposes.
    """
    sub = select(_Foo).subquery()
    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            session.execute(select(sub))
    finally:
        reset_current(token)

    flattened = " ".join(sql_capture[-1].split())
    assert "foo.workspace_id = ?" in flattened


def test_update_non_scoped_with_scoped_subquery_filters_inner(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    """``update(Bar).where(Bar.id.in_(select(_Foo.id)))`` filters the inner select.

    Outer ``bar`` is non-scoped so no top-level filter applies, but
    the inner select reads scoped ``foo`` and must carry
    ``foo.workspace_id = ?``. Without recursion the inner subquery
    would silently return cross-workspace rows and the bulk UPDATE
    would target ``bar`` rows that match other workspaces.
    """
    import re

    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            session.execute(
                update(_Bar).where(_Bar.id.in_(select(_Foo.id))).values(label="x")
            )
    finally:
        reset_current(token)

    flattened = " ".join(sql_capture[-1].split())
    assert "UPDATE" in flattened.upper()
    assert "foo.workspace_id = ?" in flattened
    # Outer non-scoped UPDATE target must stay bare; the scoped
    # predicate belongs only to the inner select.
    assert re.search(r"(?<!_)\bbar\.workspace_id", flattened) is None


def test_aliased_scoped_table_in_where_subquery_filters_on_alias(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    """An ``aliased(Foo)`` inside a WHERE subquery filters on the alias's column.

    The cd-3yhd invariant — never inject ``foo.workspace_id`` against
    a bare table when the caller wrote an alias — extends to the new
    recursive path. If the inner-select visitor unwrapped to the bare
    :class:`~sqlalchemy.Table`, SQLite would either render an extra
    ``, foo`` FROM element (cartesian product) or filter the wrong
    column entirely; the alias's own column is what must show up.
    """
    foo_alias = aliased(_Foo)
    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            session.execute(select(_Bar).where(_Bar.id.in_(select(foo_alias.id))))
    finally:
        reset_current(token)

    flattened = " ".join(sql_capture[-1].split()).lower()
    # Alias column gets the predicate, not the bare table column.
    assert "foo_1.workspace_id = ?" in flattened
    assert "where foo.workspace_id" not in flattened
    # Inner select renders as ``FROM foo AS foo_1`` — no stray bare
    # ``foo`` FROM element joined alongside.
    assert "from foo as foo_1" in flattened


def test_repeated_execution_of_same_statement_does_not_double_inject(
    session_factory: sessionmaker[Session],
    sql_capture: list[str],
) -> None:
    """Re-executing the same :class:`Select` keeps exactly one workspace filter.

    The rewriter clones via :func:`cloned_traverse` rather than
    mutating ``stmt`` in place, so the original statement object never
    accumulates predicates. A regression here would surface as two
    (or more) ``foo.workspace_id = ?`` clauses on the second run —
    visible in the captured SQL even if the query still returned the
    correct rows.
    """
    stmt = select(_Foo)
    token = set_current(_CTX_A)
    try:
        with session_factory() as session:
            session.execute(stmt)
            session.execute(stmt)
    finally:
        reset_current(token)

    assert len(sql_capture) >= 2
    for rendered in sql_capture[-2:]:
        flattened = " ".join(rendered.split())
        assert flattened.count("foo.workspace_id = ?") == 1


def test_offender_name_is_deterministic_across_multiple_inner_scopes(
    session_factory: sessionmaker[Session],
) -> None:
    """When several scoped tables are reachable via subqueries, the offender
    is the alphabetically-first name.

    The cd-3yhd invariant ("error messages don't depend on walker
    iteration order") still applies once offenders can come from
    inner selects. With both ``foo`` and ``foo_with_bar`` reachable
    via separate WHERE-clause subqueries, the rewriter must pick
    ``foo`` regardless of the order the visitor encounters them.
    """
    stmt = (
        select(_Bar)
        .where(_Bar.id.in_(select(_FooWithBar.id)))
        .where(_Bar.id.in_(select(_Foo.id)))
    )
    with session_factory() as session, pytest.raises(TenantFilterMissing) as exc:
        session.execute(stmt)
    assert exc.value.table == "foo"


# -- Class-level install (cd-nf8p): listener survives sessionmaker churn --


def test_filtered_session_class_install_survives_sessionmaker_churn(
    engine: Engine,
    sql_capture: list[str],
) -> None:
    """Regression for cd-nf8p: installing the tenant filter on the
    :class:`FilteredSession` class — not per-sessionmaker — must keep
    the listener alive through any number of fresh sessionmakers
    built on the same engine.

    The pre-fix path installed on each new ``sessionmaker`` and could
    silently leave a fresh session's ``do_orm_execute`` list empty
    even when :func:`sqlalchemy.event.contains` reported True. By
    pinning the listener on a :class:`Session` subclass, the
    Session-class dispatch carries it regardless of which factory
    built the session.

    This test deliberately does **not** call
    :func:`install_tenant_filter` on the per-iteration factories;
    success therefore proves the class-level install is what's
    catching the query.
    """
    from app.adapters.db.session import FilteredSession
    from app.tenancy.orm_filter import _do_orm_execute

    factories: list[sessionmaker[FilteredSession]] = [
        sessionmaker(bind=engine, expire_on_commit=False, class_=FilteredSession)
        for _ in range(5)
    ]

    # Every spawned session must have the listener attached via the
    # class dispatch — not the per-factory dispatch.
    for factory in factories:
        with factory() as session:
            listeners = list(session.dispatch.do_orm_execute)
            assert _do_orm_execute in listeners, (
                "FilteredSession class-level install must surface the "
                "tenant-filter listener on every session, regardless "
                "of which sessionmaker spawned it"
            )

    # With an active context, every session compiles SQL that carries
    # the workspace_id predicate. Checking the **most recent** capture
    # entry per iteration confirms each session's listener fired.
    token = set_current(_CTX_A)
    try:
        for factory in factories:
            before = len(sql_capture)
            with factory() as session:
                session.execute(select(_Foo))
            assert len(sql_capture) > before, (
                "expected the session to issue at least one cursor execution"
            )
            flattened = " ".join(sql_capture[-1].split())
            assert "foo.workspace_id = ?" in flattened
    finally:
        reset_current(token)

    # Without a context, every session must still fail closed.
    for factory in factories:
        with factory() as session, pytest.raises(TenantFilterMissing):
            session.execute(select(_Foo))
