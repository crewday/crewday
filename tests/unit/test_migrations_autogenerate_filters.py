"""Tests for Alembic autogenerate noise filters."""

from __future__ import annotations

from alembic.operations import ops
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text

from migrations.autogenerate_filters import (
    filter_sqlite_expression_index_false_positives,
    process_sqlite_expression_index_false_positives,
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
