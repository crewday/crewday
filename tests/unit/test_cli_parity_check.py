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
        "x_cli": {"group": group, "verb": name, "mutates": method != "GET"},
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _schema(*operation_ids: str, include_x_cli: bool = True) -> dict[str, object]:
    paths: dict[str, object] = {}
    for index, operation_id in enumerate(operation_ids):
        operation: dict[str, object] = {
            "operationId": operation_id,
            "responses": {"200": {"description": "ok"}},
        }
        if include_x_cli:
            operation["x-cli"] = {"group": "demo", "verb": f"list-{index}"}
        paths[f"/w/{{slug}}/api/v1/demo/{index}"] = {
            "get": {
                **operation,
            }
        }
    return {"openapi": "3.1.0", "paths": paths}


def _admin_schema(
    operation_id: str,
    *,
    method: str = "get",
    include_x_cli: bool = True,
) -> dict[str, object]:
    operation: dict[str, object] = {
        "operationId": operation_id,
        "responses": {"200": {"description": "ok"}},
    }
    if include_x_cli:
        operation["x-cli"] = {
            "group": "demo",
            "verb": operation_id.rsplit(".", 1)[-1],
            "summary": f"{operation_id} summary",
            "mutates": method.lower() != "get",
        }
    return {
        "openapi": "3.1.0",
        "paths": {"/admin/api/v1/demo": {method: operation}},
    }


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


def test_report_requires_admin_operations_in_admin_surface(tmp_path: Path) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(surface, [])
    _write_json(surface_admin, [])
    _write_json(schema, _admin_schema("admin.demo.list"))

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.admin_missing_from_surface == ("admin.demo.list",)
    assert not report.ok


def test_report_requires_admin_x_cli_or_reviewed_exclusion(tmp_path: Path) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(surface, [])
    _write_json(surface_admin, [])
    _write_json(schema, _admin_schema("admin.demo.unannotated", include_x_cli=False))

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.admin_missing_x_cli == ("admin.demo.unannotated",)
    assert not report.ok


def test_report_accepts_reviewed_admin_exclusion(tmp_path: Path) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(surface, [])
    _write_json(surface_admin, [])
    exclusions.write_text(
        "exclusions:\n"
        "  - operation_id: admin.demo.secret\n"
        "    reason: interactive session only\n",
        encoding="utf-8",
    )
    _write_json(schema, _admin_schema("admin.demo.secret", include_x_cli=False))

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.admin_missing_x_cli == ()
    assert report.admin_missing_from_surface == ()
    assert report.ok


def test_report_requires_reviewed_exclusion_for_hidden_admin_operation(
    tmp_path: Path,
) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(surface, [])
    _write_json(surface_admin, [])
    _write_json(
        schema,
        {
            "openapi": "3.1.0",
            "paths": {
                "/admin/api/v1/agent/message": {
                    "post": {
                        "operationId": "admin.agent.message.create",
                        "responses": {"200": {"description": "ok"}},
                        "x-cli": {
                            "hidden": True,
                            "group": "agent",
                            "verb": "message-create",
                            "summary": "Message agent",
                            "mutates": True,
                        },
                    }
                }
            },
        },
    )

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.admin_hidden_without_reviewed_exclusion == (
        "admin.agent.message.create",
    )
    assert not report.ok


def test_report_requires_reviewed_exclusion_for_unavailable_admin_operation(
    tmp_path: Path,
) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    entry = _entry(
        operation_id="admin.demo.secret",
        group="demo",
        name="secret",
        path="/admin/api/v1/demo/secret",
        method="POST",
    )
    _write_json(surface, [])
    _write_json(surface_admin, [entry])
    _write_json(
        schema,
        {
            "openapi": "3.1.0",
            "paths": {
                "/admin/api/v1/demo/secret": {
                    "post": {
                        "operationId": "admin.demo.secret",
                        "responses": {"200": {"description": "ok"}},
                        "x-agent-forbidden": True,
                        "x-interactive-only": True,
                        "x-cli": {
                            "group": "demo",
                            "verb": "secret",
                            "summary": "Rotate a secret",
                            "mutates": True,
                        },
                    }
                }
            },
        },
    )

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.admin_unavailable_without_reviewed_exclusion == ("admin.demo.secret",)
    assert not report.ok


def test_report_documents_admin_default_mutation_confirmation(
    tmp_path: Path,
) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    entry = _entry(
        operation_id="admin.demo.create",
        group="demo",
        name="create",
        path="/admin/api/v1/demo",
        method="POST",
    )
    _write_json(surface, [])
    _write_json(surface_admin, [entry])
    _write_json(schema, _admin_schema("admin.demo.create", method="post"))

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.admin_mutation_confirmation_missing == ()
    assert report.ok


def test_report_requires_workspace_x_cli_or_reviewed_exclusion(
    tmp_path: Path,
) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(surface, [])
    _write_json(
        schema,
        {
            "openapi": "3.1.0",
            "paths": {
                "/w/{slug}/api/v1/not-agent": {
                    "get": {
                        "operationId": "demo.not_agent",
                        "responses": {"200": {"description": "ok"}},
                    }
                },
            },
        },
    )

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.workspace_missing_x_cli == ("demo.not_agent",)
    assert not report.ok


def test_report_requires_workspace_mutation_agent_classification(
    tmp_path: Path,
) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(
        surface, [_entry(operation_id="demo.create", name="create", method="POST")]
    )
    _write_json(
        schema,
        {
            "openapi": "3.1.0",
            "paths": {
                "/w/{slug}/api/v1/demo": {
                    "post": {
                        "operationId": "demo.create",
                        "responses": {"200": {"description": "ok"}},
                        "x-cli": {
                            "group": "demo",
                            "verb": "create",
                            "summary": "Create demo",
                            "mutates": True,
                        },
                    }
                },
            },
        },
    )

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.workspace_mutation_classification_invalid == ("demo.create",)
    assert not report.ok


def test_report_treats_non_get_workspace_methods_as_mutating(
    tmp_path: Path,
) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(
        surface, [_entry(operation_id="demo.create", name="create", method="POST")]
    )
    _write_json(
        schema,
        {
            "openapi": "3.1.0",
            "paths": {
                "/w/{slug}/api/v1/demo": {
                    "post": {
                        "operationId": "demo.create",
                        "responses": {"200": {"description": "ok"}},
                        "x-cli": {
                            "group": "demo",
                            "verb": "create",
                            "summary": "Create demo",
                            "mutates": False,
                        },
                    }
                },
            },
        },
    )

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.workspace_mutation_classification_invalid == ("demo.create",)
    assert not report.ok


def test_report_rejects_invalid_workspace_agent_confirmation(
    tmp_path: Path,
) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(
        surface, [_entry(operation_id="demo.create", name="create", method="POST")]
    )
    _write_json(
        schema,
        {
            "openapi": "3.1.0",
            "paths": {
                "/w/{slug}/api/v1/demo": {
                    "post": {
                        "operationId": "demo.create",
                        "responses": {"200": {"description": "ok"}},
                        "x-agent-confirm": False,
                        "x-cli": {
                            "group": "demo",
                            "verb": "create",
                            "summary": "Create demo",
                            "mutates": True,
                        },
                    }
                },
            },
        },
    )

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.workspace_mutation_classification_invalid == ("demo.create",)
    assert not report.ok


def test_report_accepts_workspace_mutation_with_single_confirmation(
    tmp_path: Path,
) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(
        surface, [_entry(operation_id="demo.create", name="create", method="POST")]
    )
    _write_json(
        schema,
        {
            "openapi": "3.1.0",
            "paths": {
                "/w/{slug}/api/v1/demo": {
                    "post": {
                        "operationId": "demo.create",
                        "responses": {"200": {"description": "ok"}},
                        "x-agent-confirm": {"summary": "Create demo?"},
                        "x-cli": {
                            "group": "demo",
                            "verb": "create",
                            "summary": "Create demo",
                            "mutates": True,
                        },
                    }
                },
            },
        },
    )

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.workspace_mutation_classification_invalid == ()
    assert report.ok


def test_report_requires_reviewed_exclusion_for_hidden_workspace_operation(
    tmp_path: Path,
) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(surface, [])
    _write_json(
        schema,
        {
            "openapi": "3.1.0",
            "paths": {
                "/w/{slug}/api/v1/browser-only": {
                    "get": {
                        "operationId": "demo.browser_only",
                        "responses": {"200": {"description": "ok"}},
                        "x-cli": {"group": "demo", "verb": "browser", "hidden": True},
                    }
                },
            },
        },
    )

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.workspace_hidden_without_reviewed_exclusion == ("demo.browser_only",)
    assert not report.ok


def test_report_accepts_reviewed_workspace_exclusion(tmp_path: Path) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(surface, [])
    exclusions.write_text(
        "exclusions:\n"
        "  - operation_id: demo.browser_only\n"
        "    reason: browser-only ceremony\n",
        encoding="utf-8",
    )
    _write_json(
        schema,
        {
            "openapi": "3.1.0",
            "paths": {
                "/w/{slug}/api/v1/browser-only": {
                    "get": {
                        "operationId": "demo.browser_only",
                        "responses": {"200": {"description": "ok"}},
                        "x-cli": {"group": "demo", "verb": "browser", "hidden": True},
                    }
                },
            },
        },
    )

    report = cli_parity_check.build_report(
        surface_path=surface,
        surface_admin_path=surface_admin,
        exclusions_path=exclusions,
        schema_path=schema,
    )

    assert report.missing_from_cli == ()
    assert report.workspace_missing_x_cli == ()
    assert report.workspace_hidden_without_reviewed_exclusion == ()
    assert report.workspace_mutation_classification_invalid == ()
    assert report.ok


def test_main_prints_report_when_codegen_check_fails(
    tmp_path: Path,
    capsys: Any,
) -> None:
    surface, surface_admin, exclusions, schema = _paths(tmp_path)
    _write_json(surface, [])
    _write_json(
        schema,
        {
            "openapi": "3.1.0",
            "paths": {
                "/w/{slug}/api/v1/not-agent": {
                    "get": {
                        "operationId": "demo.not_agent",
                        "responses": {"200": {"description": "ok"}},
                    }
                },
            },
        },
    )

    status = cli_parity_check.main(
        [
            "--surface",
            str(surface),
            "--surface-admin",
            str(surface_admin),
            "--exclusions",
            str(exclusions),
            "--schema",
            str(schema),
        ]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert "crewday codegen: committed surface out of date" in captured.err
    assert "Workspace operations missing valid x-cli metadata" in captured.err
    assert "demo.not_agent" in captured.err


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
