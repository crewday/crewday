"""Tests for Alembic autogenerate noise filters."""

from __future__ import annotations

import pytest
from alembic.operations import ops
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SAWarning

from migrations.autogenerate_filters import (
    filter_sqlite_expression_index_false_positives,
    process_sqlite_expression_index_false_positives,
    sqlite_role_grant_workspace_active_index_matches_metadata,
    suppress_sqlite_role_grant_expression_index_warnings_if_safe,
)


def test_filter_removes_only_stocktake_expression_index_pair() -> None:
    upgrade_ops = ops.UpgradeOps(
        ops=[
            ops.DropIndexOp(
                "ix_inventory_stocktake_workspace_property_started",
                "inventory_stocktake",
            ),
            ops.CreateIndexOp(
                "ix_inventory_stocktake_workspace_property_started",
                "inventory_stocktake",
                ["workspace_id", "property_id", text("started_at DESC")],
            ),
            ops.CreateIndexOp(
                "ix_unrelated",
                "inventory_stocktake",
                ["workspace_id"],
            ),
        ]
    )

    filter_sqlite_expression_index_false_positives(upgrade_ops)

    assert len(upgrade_ops.ops) == 1
    assert isinstance(upgrade_ops.ops[0], ops.CreateIndexOp)
    assert upgrade_ops.ops[0].index_name == "ix_unrelated"


def test_filter_keeps_missing_stocktake_index_add() -> None:
    upgrade_ops = ops.UpgradeOps(
        ops=[
            ops.CreateIndexOp(
                "ix_inventory_stocktake_workspace_property_started",
                "inventory_stocktake",
                ["workspace_id", "property_id", text("started_at DESC")],
            )
        ]
    )

    filter_sqlite_expression_index_false_positives(upgrade_ops)

    assert len(upgrade_ops.ops) == 1


def test_filter_keeps_stocktake_index_metadata_expression_change() -> None:
    upgrade_ops = ops.UpgradeOps(
        ops=[
            ops.DropIndexOp(
                "ix_inventory_stocktake_workspace_property_started",
                "inventory_stocktake",
            ),
            ops.CreateIndexOp(
                "ix_inventory_stocktake_workspace_property_started",
                "inventory_stocktake",
                ["workspace_id", "property_id", text("started_at ASC")],
            ),
        ]
    )

    filter_sqlite_expression_index_false_positives(upgrade_ops)

    assert len(upgrade_ops.ops) == 2


def test_process_filter_removes_pair_only_when_live_sqlite_index_matches() -> None:
    directives = [_stocktake_started_index_migration_script()]

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE inventory_stocktake (
                    workspace_id TEXT NOT NULL,
                    property_id TEXT NOT NULL,
                    started_at TEXT NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX ix_inventory_stocktake_workspace_property_started
                ON inventory_stocktake (workspace_id, property_id, started_at DESC)
                """
            )
        )
        migration_context = MigrationContext.configure(conn)
        process_sqlite_expression_index_false_positives(
            migration_context, None, directives
        )

    assert directives[0].upgrade_ops.ops == []


def test_process_filter_keeps_pair_when_live_sqlite_index_expression_drifted() -> None:
    directives = [_stocktake_started_index_migration_script()]

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE inventory_stocktake (
                    workspace_id TEXT NOT NULL,
                    property_id TEXT NOT NULL,
                    started_at TEXT NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX ix_inventory_stocktake_workspace_property_started
                ON inventory_stocktake (workspace_id, property_id, started_at ASC)
                """
            )
        )
        migration_context = MigrationContext.configure(conn)
        process_sqlite_expression_index_false_positives(
            migration_context, None, directives
        )

    assert len(directives[0].upgrade_ops.ops) == 2


def test_role_grant_index_guard_matches_expected_live_sqlite_sql() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_role_grant_table(conn)
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX uq_role_grant_workspace_user_role_scope_active
                ON role_grant (
                    workspace_id,
                    user_id,
                    grant_role,
                    COALESCE(scope_property_id, '')
                )
                WHERE scope_kind = 'workspace' AND revoked_at IS NULL
                """
            )
        )

        assert sqlite_role_grant_workspace_active_index_matches_metadata(conn) is True


def test_role_grant_index_guard_rejects_missing_live_sqlite_index() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_role_grant_table(conn)

        assert sqlite_role_grant_workspace_active_index_matches_metadata(conn) is False


def test_role_grant_index_guard_rejects_expression_drift() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_role_grant_table(conn)
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX uq_role_grant_workspace_user_role_scope_active
                ON role_grant (
                    workspace_id,
                    user_id,
                    grant_role,
                    scope_property_id
                )
                WHERE scope_kind = 'workspace' AND revoked_at IS NULL
                """
            )
        )

        assert sqlite_role_grant_workspace_active_index_matches_metadata(conn) is False


def test_role_grant_index_guard_rejects_predicate_drift() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_role_grant_table(conn)
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX uq_role_grant_workspace_user_role_scope_active
                ON role_grant (
                    workspace_id,
                    user_id,
                    grant_role,
                    COALESCE(scope_property_id, '')
                )
                WHERE scope_kind = 'workspace'
                """
            )
        )

        assert sqlite_role_grant_workspace_active_index_matches_metadata(conn) is False


def test_role_grant_index_guard_rejects_additive_predicate_drift() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_role_grant_table(conn)
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX uq_role_grant_workspace_user_role_scope_active
                ON role_grant (
                    workspace_id,
                    user_id,
                    grant_role,
                    COALESCE(scope_property_id, '')
                )
                WHERE scope_kind = 'workspace'
                  AND revoked_at IS NULL
                  AND scope_property_id IS NULL
                """
            )
        )

        assert sqlite_role_grant_workspace_active_index_matches_metadata(conn) is False


def test_role_grant_warning_suppression_rejects_unsafe_live_index() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_role_grant_table(conn)

        with (
            pytest.raises(RuntimeError, match="live sqlite_master SQL"),
            suppress_sqlite_role_grant_expression_index_warnings_if_safe(conn),
        ):
            pass


def test_role_grant_warning_suppression_is_guarded_and_targeted() -> None:
    import warnings

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _create_role_grant_table(conn)
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX uq_role_grant_workspace_user_role_scope_active
                ON role_grant (
                    workspace_id,
                    user_id,
                    grant_role,
                    COALESCE(scope_property_id, '')
                )
                WHERE scope_kind = 'workspace' AND revoked_at IS NULL
                """
            )
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with suppress_sqlite_role_grant_expression_index_warnings_if_safe(conn):
                warnings.warn(
                    "Skipped unsupported reflection of expression-based index "
                    "uq_role_grant_workspace_user_role_scope_active",
                    SAWarning,
                    stacklevel=1,
                )
                warnings.warn(
                    "autogenerate skipping metadata-specified expression-based index "
                    "'uq_role_grant_workspace_user_role_scope_active'; dialect "
                    "'sqlite' under SQLAlchemy 2.0.49 can't reflect these indexes "
                    "so they can't be compared",
                    UserWarning,
                    stacklevel=1,
                )
                warnings.warn(
                    "autogenerate skipping metadata-specified expression-based index "
                    "'uq_unrelated'",
                    UserWarning,
                    stacklevel=1,
                )

    assert len(caught) == 1
    assert "uq_unrelated" in str(caught[0].message)


def _stocktake_started_index_migration_script() -> ops.MigrationScript:
    return ops.MigrationScript(
        None,
        ops.UpgradeOps(
            ops=[
                ops.DropIndexOp(
                    "ix_inventory_stocktake_workspace_property_started",
                    "inventory_stocktake",
                ),
                ops.CreateIndexOp(
                    "ix_inventory_stocktake_workspace_property_started",
                    "inventory_stocktake",
                    ["workspace_id", "property_id", text("started_at DESC")],
                ),
            ]
        ),
        ops.DowngradeOps(ops=[]),
    )


def _create_role_grant_table(conn: Connection) -> None:
    conn.execute(
        text(
            """
            CREATE TABLE role_grant (
                workspace_id TEXT,
                user_id TEXT NOT NULL,
                grant_role TEXT NOT NULL,
                scope_property_id TEXT,
                scope_kind TEXT NOT NULL,
                revoked_at TEXT
            )
            """
        )
    )
