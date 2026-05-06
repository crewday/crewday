"""Targeted Alembic autogenerate filters."""

from __future__ import annotations

from collections.abc import Sequence

from alembic.operations import ops
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.schema import Column

_STOCKTAKE_INDEX_TABLE = "inventory_stocktake"
_STOCKTAKE_INDEX_NAME = "ix_inventory_stocktake_workspace_property_started"
_STOCKTAKE_INDEX_COLUMNS = ("workspace_id", "property_id")
_STOCKTAKE_INDEX_EXPRESSION = "started_at desc"


def process_sqlite_expression_index_false_positives(
    migration_context: MigrationContext,
    _revision: object,
    directives: list[ops.MigrationScript],
) -> None:
    """Remove known SQLite expression-index reflection noise from autogenerate.

    SQLAlchemy 2.0.49 reflects SQLite expression indexes imperfectly:
    ``started_at DESC`` comes back as a plain ``started_at`` column, so
    Alembic sees a drop/add diff for the already-correct stocktake index.
    This hook removes only that exact paired false positive. A missing
    index, a metadata expression change, or any unrelated drift still
    reaches ``alembic check``.
    """
    if migration_context.dialect.name != "sqlite":
        return
    if not _sqlite_stocktake_started_index_matches_metadata(migration_context):
        return

    for directive in directives:
        for upgrade_ops in directive.upgrade_ops_list:
            filter_sqlite_expression_index_false_positives(upgrade_ops)
        for downgrade_ops in directive.downgrade_ops_list:
            filter_sqlite_expression_index_false_positives(downgrade_ops)


def filter_sqlite_expression_index_false_positives(container: ops.OpContainer) -> None:
    """Remove the stocktake expression-index drop/add pair from ``container``."""
    for operation in container.ops:
        if isinstance(operation, ops.ModifyTableOps):
            filter_sqlite_expression_index_false_positives(operation)

    false_positive_indexes = _known_false_positive_indexes(container.ops)
    if not false_positive_indexes:
        return

    container.ops = [
        operation
        for operation in container.ops
        if not _targets_known_false_positive(operation, false_positive_indexes)
    ]


def _known_false_positive_indexes(
    operations: Sequence[ops.MigrateOperation],
) -> set[tuple[str | None, str, str]]:
    drops = [
        operation
        for operation in operations
        if isinstance(operation, ops.DropIndexOp)
        and _targets_stocktake_started_index(operation)
    ]
    creates = [
        operation
        for operation in operations
        if isinstance(operation, ops.CreateIndexOp)
        and _is_stocktake_started_expression_create(operation)
    ]

    pairs: set[tuple[str | None, str, str]] = set()
    for drop in drops:
        drop_key = _index_key(drop)
        for create in creates:
            if _index_key(create) == drop_key:
                pairs.add(drop_key)
                break
    return pairs


def _targets_known_false_positive(
    operation: ops.MigrateOperation,
    false_positive_indexes: set[tuple[str | None, str, str]],
) -> bool:
    if isinstance(operation, ops.CreateIndexOp | ops.DropIndexOp):
        return _index_key(operation) in false_positive_indexes
    return False


def _index_key(
    operation: ops.CreateIndexOp | ops.DropIndexOp,
) -> tuple[str | None, str, str]:
    table_name = operation.table_name
    if table_name is None:
        table_name = ""
    return (operation.schema, table_name, operation.index_name)


def _targets_stocktake_started_index(
    operation: ops.CreateIndexOp | ops.DropIndexOp,
) -> bool:
    return (
        operation.table_name == _STOCKTAKE_INDEX_TABLE
        and operation.index_name == _STOCKTAKE_INDEX_NAME
    )


def _is_stocktake_started_expression_create(operation: ops.CreateIndexOp) -> bool:
    if not _targets_stocktake_started_index(operation):
        return False
    if operation.unique:
        return False
    if len(operation.columns) != 3:
        return False

    leading_columns = tuple(_column_name(column) for column in operation.columns[:2])
    if leading_columns != _STOCKTAKE_INDEX_COLUMNS:
        return False

    expression = operation.columns[2]
    if not isinstance(expression, TextClause):
        return False
    return expression.text.strip().lower() == _STOCKTAKE_INDEX_EXPRESSION


def _sqlite_stocktake_started_index_matches_metadata(
    migration_context: MigrationContext,
) -> bool:
    connection = getattr(migration_context, "connection", None)
    if connection is None:
        return False

    definition = connection.execute(
        text(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'index'
              AND tbl_name = :table_name
              AND name = :index_name
            """
        ),
        {
            "table_name": _STOCKTAKE_INDEX_TABLE,
            "index_name": _STOCKTAKE_INDEX_NAME,
        },
    ).scalar_one_or_none()
    if not isinstance(definition, str):
        return False

    normalized_definition = " ".join(
        definition.lower()
        .replace('"', "")
        .replace("`", "")
        .replace("[", "")
        .replace("]", "")
        .split()
    )
    return (
        f"on {_STOCKTAKE_INDEX_TABLE} "
        f"({_STOCKTAKE_INDEX_COLUMNS[0]}, {_STOCKTAKE_INDEX_COLUMNS[1]}, "
        f"{_STOCKTAKE_INDEX_EXPRESSION})" in normalized_definition
    )


def _column_name(column: object) -> str | None:
    if isinstance(column, str):
        return column
    if isinstance(column, Column):
        return column.name
    name = getattr(column, "name", None)
    if isinstance(name, str):
        return name
    return None
