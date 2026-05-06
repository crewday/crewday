"""Targeted Alembic autogenerate filters."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from alembic.operations import ops
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SAWarning
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.schema import Column

_STOCKTAKE_INDEX_TABLE = "inventory_stocktake"
_STOCKTAKE_INDEX_NAME = "ix_inventory_stocktake_workspace_property_started"
_STOCKTAKE_INDEX_COLUMNS = ("workspace_id", "property_id")
_STOCKTAKE_INDEX_EXPRESSION = "started_at desc"
_ROLE_GRANT_INDEX_TABLE = "role_grant"
_ROLE_GRANT_INDEX_NAME = "uq_role_grant_workspace_user_role_scope_active"
_ROLE_GRANT_INDEX_COLUMNS = ("workspace_id", "user_id", "grant_role")
_ROLE_GRANT_INDEX_EXPRESSION = "coalesce(scope_property_id, '')"
_ROLE_GRANT_INDEX_PREDICATE = "where scope_kind = 'workspace' and revoked_at is null"
_ROLE_GRANT_INDEX_DEFINITION = (
    f"create unique index {_ROLE_GRANT_INDEX_NAME} "
    f"on {_ROLE_GRANT_INDEX_TABLE} "
    f"({_ROLE_GRANT_INDEX_COLUMNS[0]}, {_ROLE_GRANT_INDEX_COLUMNS[1]}, "
    f"{_ROLE_GRANT_INDEX_COLUMNS[2]}, {_ROLE_GRANT_INDEX_EXPRESSION}) "
    f"{_ROLE_GRANT_INDEX_PREDICATE}"
)


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


@contextmanager
def suppress_sqlite_role_grant_expression_index_warnings_if_safe(
    connection: Connection,
) -> Iterator[None]:
    """Suppress SQLite role-grant expression-index warnings after a live check."""
    if not sqlite_role_grant_workspace_active_index_matches_metadata(connection):
        raise RuntimeError(
            "SQLite Alembic check cannot compare expression index "
            f"{_ROLE_GRANT_INDEX_NAME!r}; live sqlite_master SQL does not match "
            "the expected unique COALESCE(scope_property_id, '') partial index."
        )

    import warnings

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=(
                ".*Skipped unsupported reflection of expression-based index "
                f"{_ROLE_GRANT_INDEX_NAME}.*"
            ),
            category=SAWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message=(
                ".*autogenerate skipping metadata-specified expression-based index "
                f"'{_ROLE_GRANT_INDEX_NAME}'.*"
            ),
            category=UserWarning,
        )
        yield


def sqlite_role_grant_workspace_active_index_matches_metadata(
    connection: Connection,
) -> bool:
    """Return whether SQLite's role-grant expression index matches metadata."""
    normalized_definition = _sqlite_index_definition(
        connection,
        table_name=_ROLE_GRANT_INDEX_TABLE,
        index_name=_ROLE_GRANT_INDEX_NAME,
    )
    if normalized_definition is None:
        return False

    return normalized_definition == _ROLE_GRANT_INDEX_DEFINITION


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

    normalized_definition = _sqlite_index_definition(
        connection,
        table_name=_STOCKTAKE_INDEX_TABLE,
        index_name=_STOCKTAKE_INDEX_NAME,
    )
    if normalized_definition is None:
        return False

    return (
        f"on {_STOCKTAKE_INDEX_TABLE} "
        f"({_STOCKTAKE_INDEX_COLUMNS[0]}, {_STOCKTAKE_INDEX_COLUMNS[1]}, "
        f"{_STOCKTAKE_INDEX_EXPRESSION})" in normalized_definition
    )


def _sqlite_index_definition(
    connection: Connection,
    *,
    table_name: str,
    index_name: str,
) -> str | None:
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
            "table_name": table_name,
            "index_name": index_name,
        },
    ).scalar_one_or_none()
    if not isinstance(definition, str):
        return None

    normalized = " ".join(
        definition.lower()
        .replace('"', "")
        .replace("`", "")
        .replace("[", "")
        .replace("]", "")
        .split()
    )
    return normalized.replace("( ", "(").replace(" )", ")")


def _column_name(column: object) -> str | None:
    if isinstance(column, str):
        return column
    if isinstance(column, Column):
        return column.name
    name = getattr(column, "name", None)
    if isinstance(name, str):
        return name
    return None
