from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts import cli_parity_check


def _entry(
    *,
    operation_id: str,
    group: str = "demo",
    name: str = "list",
    path: str = "/w/{slug}/api/v1/demo",
    method: str = "GET",
) -> dict[str, Any]:
    return {
        "body_schema_ref": None,
        "group": group,
        "http": {"method": method, "path": path},
        "idempotent": method in {"GET", "HEAD", "PUT", "DELETE"},
        "name": name,
        "operation_id": operation_id,
        "path_params": [],
        "query_params": [],
        "response_schema_ref": None,
        "summary": f"{operation_id} summary",
        "x_agent_confirm": None,
        "x_cli": None,
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _schema(*operation_ids: str) -> dict[str, object]:
    paths: dict[str, object] = {}
    for index, operation_id in enumerate(operation_ids):
        paths[f"/w/{{slug}}/api/v1/demo/{index}"] = {
            "get": {
                "operationId": operation_id,
                "responses": {"200": {"description": "ok"}},
            }
        }
    return {"openapi": "3.1.0", "paths": paths}


def _paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    surface = tmp_path / "_surface.json"
    surface_admin = tmp_path / "_surface_admin.json"
    exclusions = tmp_path / "_exclusions.yaml"
    schema = tmp_path / "openapi.json"
    _write_json(surface_admin, [])
    exclusions.write_text("exclusions: []\n", encoding="utf-8")
    return surface, surface_admin, exclusions, schema


def test_report_accepts_matching_surface_and_openapi(tmp_path: Path) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(surface, [_entry(operation_id="demo.list")])
    _write_json(schema, _schema("demo.list"))

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.ok


def test_report_names_openapi_operations_missing_from_surface(tmp_path: Path) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(surface, [_entry(operation_id="demo.list")])
    _write_json(schema, _schema("demo.list", "demo.create"))

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.missing_from_cli == ("demo.create",)
    assert not report.ok


def test_report_names_surface_operations_removed_from_openapi(tmp_path: Path) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(
        surface,
        [
            _entry(operation_id="demo.list"),
            _entry(operation_id="demo.stale", name="stale"),
        ],
    )
    _write_json(schema, _schema("demo.list"))

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.removed_from_openapi == ("demo.stale",)
    assert not report.ok


def test_override_covers_composite_operations(tmp_path: Path) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(surface, [])
    _write_json(schema, _schema("complete_task", "upload_task_evidence"))

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.missing_from_cli == ()
