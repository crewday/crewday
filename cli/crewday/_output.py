"""Output formatting for generated CLI commands.

Generated commands hand their decoded API response to this module and
choose one of the four §13 modes: JSON, YAML, table, or NDJSON. The
formatter is deliberately side-effect-free; runtime code owns stdout /
stderr and streaming.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from io import StringIO
from typing import Any, Final

import yaml
from rich import box
from rich.console import Console
from rich.table import Table

from crewday._client import ApiError
from crewday._globals import OutputMode

__all__ = [
    "format_api_error",
    "format_response",
]


_COLUMN_HINT_KEY: Final[str] = "x-cli-columns"
_MAX_CELL_WIDTH: Final[int] = 72


def format_response(
    value: object,
    mode: OutputMode,
    schema_hint: Mapping[str, Any] | None = None,
) -> str:
    """Return ``value`` formatted for the requested CLI output mode."""
    if mode == "json":
        return json.dumps(value, indent=2, sort_keys=False, default=_json_default)
    if mode == "yaml":
        return _format_yaml(value)
    if mode == "ndjson":
        return "\n".join(
            json.dumps(row, sort_keys=False, default=_json_default)
            for row in _rows_for_ndjson(value)
        )
    return _format_table(value, schema_hint=schema_hint)


def format_api_error(error: ApiError, mode: OutputMode) -> str:
    """Return an API error formatted for stderr in the active mode."""
    payload = {
        "status": error.status,
        "code": error.code,
        "message": error.message,
        "details": error.details,
    }
    if mode in ("json", "yaml", "ndjson"):
        return format_response(payload, mode)

    table = Table(
        box=box.ASCII,
        show_header=True,
        header_style=_header_style(),
        expand=False,
    )
    table.add_column("Status", no_wrap=True)
    table.add_column("Code", no_wrap=True)
    table.add_column("Message")
    table.add_row(str(error.status), error.code, error.message)
    return _render_table(table)


def _format_yaml(value: object) -> str:
    rendered = yaml.safe_dump(
        _yaml_safe(value),
        sort_keys=False,
        allow_unicode=True,
    ).rstrip()
    lines = rendered.splitlines()
    if lines and lines[-1] == "...":
        lines = lines[:-1]
    return "\n".join(lines)


def _json_default(value: object) -> str:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _yaml_safe(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(k): _yaml_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_yaml_safe(v) for v in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _rows_for_ndjson(value: object) -> Iterable[object]:
    if isinstance(value, Mapping):
        data = value.get("data")
        data_rows = _sequence_rows(data)
        if data_rows is not None and _is_cursor_envelope(value):
            return data_rows
        return (value,)
    rows = _sequence_rows(value)
    if rows is not None:
        return rows
    return (value,)


def _format_table(
    value: object,
    *,
    schema_hint: Mapping[str, Any] | None,
) -> str:
    rows = _rows_for_table(value)
    if not rows:
        return ""

    columns = _columns_for_rows(rows, schema_hint=schema_hint)
    if not columns:
        return format_response(value, "json")

    table = Table(
        box=box.ASCII,
        show_header=True,
        header_style=_header_style(),
        expand=True,
    )
    for _key, label in columns:
        table.add_column(label, overflow="fold", max_width=_MAX_CELL_WIDTH)
    for row in rows:
        table.add_row(*[_cell_text(row.get(key)) for key, _label in columns])
    return _render_table(table)


def _rows_for_table(value: object) -> list[Mapping[str, object]]:
    if isinstance(value, Mapping):
        data = value.get("data")
        data_rows = _mapping_rows(data)
        if data_rows is not None and _is_table_envelope(value):
            return data_rows
        return [_string_key_mapping(value)]
    rows = _mapping_rows(value)
    if rows is not None:
        return rows
    return [{"value": value}]


def _is_cursor_envelope(value: Mapping[object, object]) -> bool:
    return "data" in value and (
        "has_more" in value or "next_cursor" in value or "total" in value
    )


def _is_table_envelope(value: Mapping[object, object]) -> bool:
    return _is_cursor_envelope(value) or set(value) == {"data"}


def _columns_for_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    schema_hint: Mapping[str, Any] | None,
) -> list[tuple[str, str]]:
    hinted = _hinted_columns(schema_hint)
    if hinted:
        return hinted

    for row in rows:
        scalar_columns = [
            (str(key), _label_for(str(key)))
            for key, value in row.items()
            if _is_scalar(value)
        ]
        if scalar_columns:
            return scalar_columns
    return []


def _hinted_columns(schema_hint: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    if schema_hint is None:
        return []

    raw = schema_hint.get(_COLUMN_HINT_KEY)
    if raw is None and isinstance(schema_hint.get("schema"), Mapping):
        raw = schema_hint["schema"].get(_COLUMN_HINT_KEY)
    if raw is None:
        return []

    columns: list[tuple[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item:
                columns.append((item, _label_for(item)))
            elif isinstance(item, Mapping):
                key = item.get("key") or item.get("name")
                if not isinstance(key, str) or not key:
                    continue
                label = item.get("label")
                columns.append(
                    (key, label if isinstance(label, str) else _label_for(key))
                )
    return columns


def _label_for(key: str) -> str:
    return key.replace("_", " ").strip().title()


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, int | float | str):
        return str(value)
    if isinstance(value, Enum):
        return str(value.value)
    return json.dumps(value, sort_keys=False, default=_json_default)


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(
        value,
        str | int | float | bool | datetime | date | Decimal | Enum,
    )


def _sequence_rows(value: object) -> list[object] | None:
    if not isinstance(value, list | tuple):
        return None
    return list(value)


def _mapping_rows(value: object) -> list[Mapping[str, object]] | None:
    if not isinstance(value, list | tuple):
        return None

    rows: list[Mapping[str, object]] = []
    for row in value:
        if not isinstance(row, Mapping):
            return None
        rows.append(_string_key_mapping(row))
    return rows


def _string_key_mapping(row: Mapping[object, object]) -> Mapping[str, object]:
    return {str(key): value for key, value in row.items()}


def _render_table(table: Table) -> str:
    buffer = StringIO()
    use_color = _use_color()
    console = Console(
        file=buffer,
        width=shutil.get_terminal_size(fallback=(100, 24)).columns,
        force_terminal=use_color,
        color_system="standard" if use_color else None,
        no_color=not use_color,
        legacy_windows=False,
    )
    console.print(table)
    return buffer.getvalue().rstrip()


def _header_style() -> str:
    return "bold" if _use_color() else ""


def _use_color() -> bool:
    return "NO_COLOR" not in os.environ and sys.stdout.isatty()
