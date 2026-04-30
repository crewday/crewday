"""Unit tests for :mod:`crewday._output`."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from crewday._client import ApiError
from crewday._output import format_api_error, format_response


def test_json_output_is_deterministic_and_preserves_key_order() -> None:
    payload = {"id": "task-1", "created_at": datetime(2026, 4, 30, tzinfo=UTC)}

    rendered = format_response(payload, "json")

    assert rendered == (
        '{\n  "id": "task-1",\n  "created_at": "2026-04-30T00:00:00+00:00"\n}'
    )


def test_yaml_output_is_deterministic() -> None:
    payload = {"id": "task-1", "state": "open"}

    rendered = format_response(payload, "yaml")

    assert rendered == "id: task-1\nstate: open"


def test_yaml_none_omits_document_end_marker() -> None:
    assert format_response(None, "yaml") == "null"


def test_ndjson_outputs_one_json_object_per_line() -> None:
    payload = [{"id": "a"}, {"id": "b"}]

    rendered = format_response(payload, "ndjson")

    assert rendered.splitlines() == ['{"id": "a"}', '{"id": "b"}']


def test_ndjson_unwraps_cursor_envelope_data() -> None:
    payload = {
        "data": [{"id": "a"}, {"id": "b"}],
        "next_cursor": None,
        "has_more": False,
    }

    rendered = format_response(payload, "ndjson")

    assert rendered.splitlines() == ['{"id": "a"}', '{"id": "b"}']


def test_ndjson_preserves_non_envelope_data_field() -> None:
    payload = {"id": "event-1", "data": [{"kind": "note"}]}

    rendered = format_response(payload, "ndjson")

    assert rendered == '{"id": "event-1", "data": [{"kind": "note"}]}'


def test_table_uses_schema_hint_columns() -> None:
    payload = [{"id": "task-1", "state": "open", "hidden": "x"}]
    hint = {
        "x-cli-columns": [
            {"key": "state", "label": "State"},
            {"key": "id", "label": "Task"},
        ]
    }

    rendered = format_response(payload, "table", schema_hint=hint)

    assert "State" in rendered
    assert "Task" in rendered
    assert "open" in rendered
    assert "task-1" in rendered
    assert "hidden" not in rendered


def test_table_falls_back_to_top_level_scalar_columns() -> None:
    payload = [{"id": "task-1", "state": "open", "meta": {"nested": True}}]

    rendered = format_response(payload, "table")

    assert "Id" in rendered
    assert "State" in rendered
    assert "task-1" in rendered
    assert "nested" not in rendered


def test_table_preserves_non_envelope_data_field() -> None:
    payload = {"id": "event-1", "data": [{"kind": "note"}]}

    rendered = format_response(payload, "table")

    assert "event-1" in rendered
    assert "note" not in rendered


def test_table_respects_no_color_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    rendered = format_response([{"id": "task-1"}], "table")

    assert "\x1b[" not in rendered


def test_json_api_error_uses_structured_payload() -> None:
    error = ApiError(
        status=429,
        code="rate_limited",
        message="retry later",
        details={"retry_after_seconds": 60},
    )

    rendered = format_api_error(error, "json")

    assert json.loads(rendered) == {
        "status": 429,
        "code": "rate_limited",
        "message": "retry later",
        "details": {"retry_after_seconds": 60},
    }


def test_table_api_error_is_human_readable_without_json_envelope() -> None:
    error = ApiError(
        status=404,
        code="not_found",
        message="no such task",
    )

    rendered = format_api_error(error, "table")

    assert "404" in rendered
    assert "not_found" in rendered
    assert "no such task" in rendered
