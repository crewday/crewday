"""Integration tests for :mod:`app.audit` against a real DB.

Covers the transaction-boundary contract (§01 "Key runtime
invariants" #3), the tenant-filter behaviour on ``audit_log``
reads (§01 "Tenant filter enforcement"), and the post-migration
schema shape (indexes, column nullability).

The sibling ``tests/unit/test_audit_writer.py`` covers field-copy
and diff/clock/ULID surface without the migration harness.

See ``docs/specs/02-domain-model.md`` §"audit_log" and
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.audit import write_audit
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import TenantFilterMissing, install_tenant_filter

pytestmark = pytest.mark.integration


_CTX = WorkspaceContext(
    workspace_id="01HWA00000000000000000WSPA",
    workspace_slug="workspace-a",
    actor_id="01HWA00000000000000000USRA",
    actor_kind="user",
    actor_grant_role="manager",
    actor_was_owner_member=True,
    audit_correlation_id="01HWA00000000000000000CRLA",
)


@pytest.fixture(scope="module")
def filtered_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory with the tenant filter installed.

    Scoped to the module so SQLAlchemy's per-sessionmaker event
    dispatch doesn't churn — building a fresh ``sessionmaker`` each
    test and re-installing the hook hits a known SQLAlchemy quirk
    where ``event.contains`` reports the listener but the new
    sessions' ``dispatch.do_orm_execute`` list comes back empty
    (observed after two prior sessions on the same engine chain).

    The top-level ``db_session`` fixture is filterless (it binds
    directly to a raw connection for SAVEPOINT isolation, bypassing
    the default sessionmaker). Tests that need to observe the
    ``TenantFilterMissing`` behaviour build their own factory here
    and install the hook explicitly.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_audit_log_registered() -> None:
    """Re-register ``audit_log`` as workspace-scoped before each test.

    ``app.adapters.db.audit`` registers the table at import time, but a
    sibling unit test (``tests/unit/test_tenancy_orm_filter.py``) calls
    :func:`registry._reset_for_tests` in an autouse fixture, which
    wipes the process-wide registry. Without this fixture the import-
    time registration would lose the race and our tenant-filter
    enforcement assertions would pass in isolation and silently drop
    the filter under the full suite.
    """
    registry.register("audit_log")


class TestMigrationShape:
    """The migration lands the ``audit_log`` table + both composite indexes."""

    def test_audit_log_table_exists(self, engine: Engine) -> None:
        assert "audit_log" in inspect(engine).get_table_names()

    def test_audit_log_columns_match_spec(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("audit_log")}
        expected = {
            "id",
            "workspace_id",
            "actor_id",
            "actor_kind",
            "actor_grant_role",
            "actor_was_owner_member",
            "entity_kind",
            "entity_id",
            "action",
            "diff",
            "correlation_id",
            "created_at",
        }
        assert set(cols) == expected
        # All columns NOT NULL — audit rows are complete by construction.
        for name, col in cols.items():
            assert col["nullable"] is False, f"{name} must be NOT NULL"
        pk = inspect(engine).get_pk_constraint("audit_log")
        assert pk["constrained_columns"] == ["id"]

    def test_composite_indexes_are_present(self, engine: Engine) -> None:
        """Both composite indexes the spec calls out must exist."""
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("audit_log")}
        assert "ix_audit_log_workspace_created" in indexes
        assert indexes["ix_audit_log_workspace_created"]["column_names"] == [
            "workspace_id",
            "created_at",
        ]
        assert "ix_audit_log_workspace_entity" in indexes
        assert indexes["ix_audit_log_workspace_entity"]["column_names"] == [
            "workspace_id",
            "entity_kind",
            "entity_id",
        ]


class TestTransactionBoundary:
    """``write_audit`` lands / rolls back with the caller's UoW."""

    def test_commit_persists_the_row(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """A write inside ``with session.begin():`` lands on commit."""
        token = set_current(_CTX)
        try:
            with filtered_factory() as writer, writer.begin():
                write_audit(
                    writer,
                    _CTX,
                    entity_kind="task",
                    entity_id="01HWATASK000000000000000T1",
                    action="created",
                    diff={"title": "new"},
                )

            # Reopen a fresh session and confirm the row is visible.
            with filtered_factory() as reader:
                rows = reader.scalars(select(AuditLog)).all()
                # Drop any rows seeded by a sibling test that also pinned
                # _CTX.workspace_id; the row we just wrote is the one
                # whose entity_id is the fixed value we passed in.
                ours = [r for r in rows if r.entity_id == "01HWATASK000000000000000T1"]
                assert len(ours) == 1
                assert ours[0].action == "created"
                assert ours[0].diff == {"title": "new"}
        finally:
            reset_current(token)

    def test_rollback_drops_the_row(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """A raise inside ``with session.begin():`` rolls back the row."""

        class _Boom(Exception):
            pass

        token = set_current(_CTX)
        try:
            writer = filtered_factory()
            try:
                with pytest.raises(_Boom), writer.begin():
                    write_audit(
                        writer,
                        _CTX,
                        entity_kind="task",
                        entity_id="01HWATASK000000000000000T2",
                        action="created",
                    )
                    raise _Boom
            finally:
                writer.close()

            # Fresh session — the rolled-back row is gone.
            with filtered_factory() as reader:
                rows = reader.scalars(
                    select(AuditLog).where(
                        AuditLog.entity_id == "01HWATASK000000000000000T2"
                    )
                ).all()
                assert rows == []
        finally:
            reset_current(token)

    def test_diff_none_round_trips_as_empty_dict(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """``diff=None`` persists as ``{}`` and reloads as ``{}``.

        The unit suite checks the in-memory attribute before flush;
        this test closes the loop by committing, reopening a fresh
        session, and confirming the JSON column round-trips the
        empty-dict payload (§02 "audit_log" NOT NULL contract).
        """
        token = set_current(_CTX)
        try:
            with filtered_factory() as writer, writer.begin():
                write_audit(
                    writer,
                    _CTX,
                    entity_kind="task",
                    entity_id="01HWATASK000000000000000T3",
                    action="deleted",
                    diff=None,
                )

            with filtered_factory() as reader:
                row = reader.scalars(
                    select(AuditLog).where(
                        AuditLog.entity_id == "01HWATASK000000000000000T3"
                    )
                ).one()
                assert row.diff == {}
                assert isinstance(row.diff, dict)
        finally:
            reset_current(token)


class TestTenantFilterEnforcement:
    """Reads against ``audit_log`` without a ctx raise :class:`TenantFilterMissing`."""

    def test_select_without_ctx_raises(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """Registered as scoped ⇒ SELECT without a ctx fails closed."""
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(AuditLog))
        assert exc.value.table == "audit_log"

    def test_select_with_ctx_only_returns_current_workspace(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """With a ctx active, the filter scopes reads to ``workspace_id``."""
        other_ctx = WorkspaceContext(
            workspace_id="01HWA00000000000000000WSPB",
            workspace_slug="workspace-b",
            actor_id="01HWA00000000000000000USRB",
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id="01HWA00000000000000000CRLB",
        )
        # Seed one row per workspace.
        token = set_current(_CTX)
        try:
            with filtered_factory() as writer, writer.begin():
                write_audit(
                    writer,
                    _CTX,
                    entity_kind="task",
                    entity_id="01HWATASK000000000000000WA",
                    action="created",
                )
        finally:
            reset_current(token)

        token = set_current(other_ctx)
        try:
            with filtered_factory() as writer, writer.begin():
                write_audit(
                    writer,
                    other_ctx,
                    entity_kind="task",
                    entity_id="01HWATASK000000000000000WB",
                    action="created",
                )
        finally:
            reset_current(token)

        # Reading from workspace A sees only the A-scoped row among the
        # two we just seeded (other tests may have added their own).
        token = set_current(_CTX)
        try:
            with filtered_factory() as reader:
                rows = reader.scalars(
                    select(AuditLog).where(
                        AuditLog.entity_id.in_(
                            [
                                "01HWATASK000000000000000WA",
                                "01HWATASK000000000000000WB",
                            ]
                        )
                    )
                ).all()
                assert [r.entity_id for r in rows] == ["01HWATASK000000000000000WA"]
        finally:
            reset_current(token)

        # Reading from workspace B sees only the B-scoped row.
        token = set_current(other_ctx)
        try:
            with filtered_factory() as reader:
                rows = reader.scalars(
                    select(AuditLog).where(
                        AuditLog.entity_id.in_(
                            [
                                "01HWATASK000000000000000WA",
                                "01HWATASK000000000000000WB",
                            ]
                        )
                    )
                ).all()
                assert [r.entity_id for r in rows] == ["01HWATASK000000000000000WB"]
        finally:
            reset_current(token)
