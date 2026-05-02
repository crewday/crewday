"""Unit tests for :mod:`crewday._codegen`.

Covers the Beads ``cd-1cfg`` contract:

* The verb / group heuristic (``_derive_name`` / ``_derive_group``)
  maps every (method, path) shape from §13 to the documented verb.
* ``x-cli`` overrides win over the heuristic.
* Exclusions: exact ``operation_id`` match, ``fnmatch``-style
  ``path_pattern`` match, missing ``reason:`` raises, unknown ids
  are silently tolerated.
* Partitioning: ``/admin/api/v1/*`` → admin surface; everything else
  → workspace.
* Determinism: generating twice from the same schema yields
  byte-identical output.
* ``--check`` mode exits non-zero when the committed file diverges.
* End-to-end: the codegen reads ``docs/api/openapi.json`` and the
  committed surface files round-trip without drift (CI parity gate).
* Boundary: the codegen module imports nothing under ``app.*`` —
  reading the committed schema keeps it a pure transformer
  (cd-uky5).

Tests feed synthetic OpenAPI dicts into the pure helpers wherever
possible so the suite stays fast (no I/O).
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from crewday import _codegen
from crewday._codegen import (
    DEFAULT_EXCLUSIONS_PATH,
    DEFAULT_OPENAPI_PATH,
    DEFAULT_SURFACE_ADMIN_PATH,
    DEFAULT_SURFACE_PATH,
    CollisionError,
    Exclusion,
    ExclusionError,
    classify_surface,
    derive_group_name,
    generate_surfaces,
    is_excluded,
    load_committed_schema,
    load_exclusions,
    main,
)

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/admin/api/v1/workspaces", "admin"),
        ("/admin/api/v1", "admin"),
        ("/admin/api/v1/llm/providers/{id}", "admin"),
        ("/api/v1/auth/me", "workspace"),
        ("/w/{slug}/api/v1/tasks", "workspace"),
        ("/w/{slug}/events", "workspace"),
        ("/healthz", "workspace"),  # non-admin fallback
    ],
)
def test_classify_surface(path: str, expected: str) -> None:
    """``/admin/api/v1/*`` is the only admin surface today."""
    assert classify_surface(path) == expected


# ---------------------------------------------------------------------------
# Group / name heuristic
# ---------------------------------------------------------------------------


def test_derive_group_uses_last_tag() -> None:
    """Tags win over the path — ``tags[-1]`` is the §12 convention."""
    group, _ = derive_group_name(
        method="GET",
        path="/w/{slug}/api/v1/time/shifts",
        operation={"tags": ["time", "shifts"]},
    )
    assert group == "shifts"


def test_derive_group_falls_back_to_path() -> None:
    """Without tags we derive from the resource-root segment."""
    group, _ = derive_group_name(
        method="GET",
        path="/w/{slug}/api/v1/tasks",
        operation={},
    )
    assert group == "tasks"


def test_derive_group_bare_host_path() -> None:
    """Bare-host routes also resolve a resource root."""
    group, _ = derive_group_name(
        method="POST",
        path="/api/v1/invite/accept",
        operation={},
    )
    assert group == "invite"


def test_derive_group_x_cli_override_wins() -> None:
    """``x-cli.group`` is the single source of truth when present."""
    group, verb = derive_group_name(
        method="POST",
        path="/w/{slug}/api/v1/time/shifts/open",
        operation={
            "tags": ["time"],
            "x-cli": {"group": "time", "verb": "clock-in"},
        },
    )
    assert group == "time"
    assert verb == "clock-in"


def test_derive_group_x_cli_subgroup_space_preserved() -> None:
    """``x-cli.group`` with spaces survives verbatim — runtime splits."""
    group, verb = derive_group_name(
        method="GET",
        path="/admin/api/v1/usage/workspaces",
        operation={"x-cli": {"group": "usage workspaces", "verb": "list"}},
    )
    assert group == "usage workspaces"
    assert verb == "list"


@pytest.mark.parametrize(
    ("method", "path", "tags", "expected_name"),
    [
        # GET — list vs show.
        ("GET", "/w/{slug}/api/v1/tasks", ["tasks"], "list"),
        ("GET", "/w/{slug}/api/v1/tasks/{id}", ["tasks"], "show"),
        ("GET", "/w/{slug}/api/v1/assets/{id}/actions", ["assets"], "actions-list"),
        (
            "PATCH",
            "/w/{slug}/api/v1/assets/actions/{action_id}",
            ["assets"],
            "actions-update",
        ),
        # POST — create vs action vs sub-action. The action / create
        # split depends on whether the trailing non-param segment
        # matches the *derived group name*: ``/me/tokens`` with
        # tag ``tokens`` → create; ``/shifts/open`` with tag ``time``
        # → action (``open`` != ``time``).
        ("POST", "/api/v1/me/tokens", ["tokens"], "create"),
        ("POST", "/w/{slug}/api/v1/time/shifts/open", ["time"], "open"),
        ("POST", "/api/v1/invite/accept", ["invite"], "accept"),
        ("POST", "/w/{slug}/api/v1/tasks/{id}/close", ["tasks"], "close"),
        ("POST", "/w/{slug}/api/v1/tasks/{id}", ["tasks"], "create"),
        # PATCH / PUT / DELETE / HEAD.
        ("PATCH", "/w/{slug}/api/v1/tasks/{id}", ["tasks"], "update"),
        ("PUT", "/w/{slug}/api/v1/tasks/{id}", ["tasks"], "replace"),
        ("DELETE", "/w/{slug}/api/v1/tasks/{id}", ["tasks"], "delete"),
        ("DELETE", "/w/{slug}/api/v1/tasks", ["tasks"], "delete"),
        ("HEAD", "/w/{slug}/api/v1/tasks", ["tasks"], "head"),
        # Path-derived group (no tags): the last-segment-== group rule
        # still applies. ``/api/v1/tasks`` → group ``tasks`` from path
        # → POST returns ``create``.
        ("POST", "/api/v1/tasks", [], "create"),
        # Collection list without tags: still ``list``.
        ("GET", "/api/v1/tasks", [], "list"),
    ],
)
def test_derive_name_heuristic(
    method: str, path: str, tags: list[str], expected_name: str
) -> None:
    """Each (method, path) shape from §13 maps to the documented verb."""
    # The group is derived first (needed by the POST action/create
    # split); rebuild via ``derive_group_name`` so the test exercises
    # the real call path.
    op: dict[str, Any] = {"tags": tags} if tags else {}
    _, name = derive_group_name(
        method=method,
        path=path,
        operation=op,
    )
    assert name == expected_name


def test_x_cli_hidden_excludes_operation() -> None:
    """``x-cli.hidden: true`` removes the operation from the surface."""
    schema = {
        "paths": {
            "/api/v1/internal": {
                "post": {
                    "operationId": "internal.do",
                    "summary": "internal",
                    "x-cli": {"hidden": True, "group": "x", "verb": "y"},
                }
            }
        }
    }
    surfaces = generate_surfaces(schema=schema)
    assert surfaces["workspace"] == []
    assert surfaces["admin"] == []


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


def test_exclusion_matches_by_operation_id() -> None:
    exc = Exclusion(reason="browser-only", operation_id="auth.passkey.login_start")
    assert exc.matches(operation_id="auth.passkey.login_start", path="/foo")
    assert not exc.matches(operation_id="auth.passkey.login_finish", path="/foo")


def test_exclusion_matches_by_path_pattern_glob() -> None:
    """``fnmatch`` glob: ``*`` matches any slug placeholder body."""
    exc = Exclusion(reason="sse", path_pattern="/w/{slug}/events")
    assert exc.matches(operation_id="transport.events", path="/w/{slug}/events")
    assert not exc.matches(
        operation_id="transport.events", path="/w/{slug}/api/v1/events"
    )


def test_exclusion_path_pattern_with_star() -> None:
    exc = Exclusion(reason="blob", path_pattern="/api/v1/files/*/blob")
    assert exc.matches(operation_id=None, path="/api/v1/files/abc/blob")
    assert not exc.matches(operation_id=None, path="/api/v1/files/abc")


def test_load_exclusions_missing_file(tmp_path: Path) -> None:
    """Missing file is tolerated (empty list)."""
    assert load_exclusions(tmp_path / "does-not-exist.yaml") == []


def test_load_exclusions_empty_list(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("exclusions: []\n")
    assert load_exclusions(path) == []


def test_load_exclusions_rejects_missing_reason(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("exclusions:\n  - operation_id: foo.bar\n")
    with pytest.raises(ExclusionError, match="missing or empty 'reason'"):
        load_exclusions(path)


def test_load_exclusions_rejects_empty_reason(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("exclusions:\n  - operation_id: foo.bar\n    reason: '   '\n")
    with pytest.raises(ExclusionError, match="missing or empty 'reason'"):
        load_exclusions(path)


def test_load_exclusions_rejects_both_op_and_pattern(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "exclusions:\n"
        "  - operation_id: foo.bar\n"
        "    path_pattern: /baz\n"
        "    reason: conflict\n"
    )
    with pytest.raises(ExclusionError, match="exactly one"):
        load_exclusions(path)


def test_load_exclusions_rejects_neither_op_nor_pattern(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("exclusions:\n  - reason: orphan\n")
    with pytest.raises(ExclusionError, match="exactly one"):
        load_exclusions(path)


def test_load_exclusions_rejects_non_list(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("exclusions: not-a-list\n")
    with pytest.raises(ExclusionError, match="must be a list"):
        load_exclusions(path)


def test_load_exclusions_rejects_top_level_list(tmp_path: Path) -> None:
    """Top level must be a mapping with 'exclusions' key."""
    path = tmp_path / "bad.yaml"
    path.write_text("- operation_id: foo\n  reason: bar\n")
    with pytest.raises(ExclusionError, match="mapping"):
        load_exclusions(path)


def test_load_exclusions_tolerates_unknown_keys(tmp_path: Path) -> None:
    """Extra keys are ignored — forward-compat for future fields."""
    path = tmp_path / "ok.yaml"
    path.write_text(
        "exclusions:\n"
        "  - operation_id: foo.bar\n"
        "    reason: retired\n"
        "    notes: ignore me\n"
    )
    items = load_exclusions(path)
    assert items == [Exclusion(reason="retired", operation_id="foo.bar")]


def test_is_excluded_tolerates_unknown_ids() -> None:
    """Spec §13: unknown operation ids must not raise.

    The canonical exclusions list includes ids that predate the
    routes being mounted; those must round-trip as "not matched yet"
    rather than killing the codegen.
    """
    exclusions = [Exclusion(reason="planned", operation_id="not-yet-mounted")]
    assert not is_excluded(
        operation_id="auth.logout", path="/api/v1/auth/logout", exclusions=exclusions
    )
    # And the exclusion still matches when that id does appear later.
    assert is_excluded(
        operation_id="not-yet-mounted",
        path="/api/v1/future",
        exclusions=exclusions,
    )


def test_checked_in_exclusions_are_loadable() -> None:
    """The canonical ``_exclusions.yaml`` parses and has reasons everywhere."""
    items = load_exclusions(DEFAULT_EXCLUSIONS_PATH)
    assert items, "committed exclusions file must not be empty"
    for item in items:
        assert item.reason.strip()
        assert (item.operation_id is None) != (item.path_pattern is None)


# ---------------------------------------------------------------------------
# Surface partitioning + generation
# ---------------------------------------------------------------------------


def _synthetic_schema() -> dict[str, Any]:
    """Return a small OpenAPI dict covering every code path.

    Shape-only — enough to exercise the heuristic, admin/workspace
    partitioning, exclusions, body/response refs, and x-cli overrides.
    """
    return {
        "paths": {
            "/w/{slug}/api/v1/tasks": {
                "get": {
                    "operationId": "tasks.list",
                    "summary": "List tasks",
                    "tags": ["tasks"],
                    "parameters": [
                        {
                            "name": "slug",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "state",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "X-Request-Id",
                            "in": "header",
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/TaskList"}
                                }
                            }
                        }
                    },
                },
                "post": {
                    "operationId": "tasks.create",
                    "tags": ["tasks"],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/TaskCreate"}
                            }
                        },
                        "required": True,
                    },
                    "responses": {
                        "201": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Task"}
                                }
                            }
                        }
                    },
                },
            },
            "/w/{slug}/api/v1/tasks/{id}/close": {
                "post": {
                    "operationId": "tasks.close",
                    "tags": ["tasks"],
                    "responses": {"204": {}},
                }
            },
            "/admin/api/v1/workspaces": {
                "get": {
                    "operationId": "workspaces.list",
                    "tags": ["admin"],
                    "responses": {"200": {}},
                }
            },
            "/w/{slug}/events": {
                "get": {
                    "operationId": "transport.events",
                    "tags": ["transport"],
                    "responses": {"200": {}},
                }
            },
            "/api/v1/auth/passkey/login/start": {
                "post": {
                    "operationId": "auth.passkey.login_start",
                    "tags": ["auth"],
                    "responses": {"200": {}},
                }
            },
        }
    }


def test_partitioning_workspace_vs_admin() -> None:
    schema = _synthetic_schema()
    surfaces = generate_surfaces(schema=schema)
    admin_paths = {e["http"]["path"] for e in surfaces["admin"]}
    workspace_paths = {e["http"]["path"] for e in surfaces["workspace"]}
    assert admin_paths == {"/admin/api/v1/workspaces"}
    assert "/w/{slug}/api/v1/tasks" in workspace_paths
    # No overlap.
    assert not admin_paths & workspace_paths


def test_exclusions_filter_applies_across_surfaces() -> None:
    schema = _synthetic_schema()
    exclusions = [
        Exclusion(
            reason="browser-only",
            operation_id="auth.passkey.login_start",
        ),
        Exclusion(reason="sse", path_pattern="/w/{slug}/events"),
    ]
    surfaces = generate_surfaces(schema=schema, exclusions=exclusions)
    op_ids = {e["operation_id"] for e in surfaces["workspace"]}
    assert "auth.passkey.login_start" not in op_ids
    assert "transport.events" not in op_ids


def test_entry_shape_is_exhaustive() -> None:
    """Every descriptor entry has the documented keys, nothing more."""
    schema = _synthetic_schema()
    surfaces = generate_surfaces(schema=schema)
    entry = next(e for e in surfaces["workspace"] if e["operation_id"] == "tasks.list")
    assert set(entry.keys()) == {
        "group",
        "name",
        "operation_id",
        "summary",
        "http",
        "path_params",
        "query_params",
        "body_schema_ref",
        "response_schema_ref",
        "idempotent",
        "x_cli",
        "x_agent_confirm",
    }
    assert entry["idempotent"] is True  # GET
    assert entry["response_schema_ref"] == "#/components/schemas/TaskList"
    # Header params are dropped; query is kept.
    query_names = [p["name"] for p in entry["query_params"]]
    assert query_names == ["state"]
    path_names = [p["name"] for p in entry["path_params"]]
    assert path_names == ["slug"]


def test_body_schema_ref_from_requestbody() -> None:
    schema = _synthetic_schema()
    surfaces = generate_surfaces(schema=schema)
    entry = next(
        e for e in surfaces["workspace"] if e["operation_id"] == "tasks.create"
    )
    assert entry["body_schema_ref"] == "#/components/schemas/TaskCreate"
    assert entry["idempotent"] is False  # POST


def test_response_picks_lowest_2xx() -> None:
    """A 201 response payload is preferred over other non-2xx shapes."""
    schema = _synthetic_schema()
    surfaces = generate_surfaces(schema=schema)
    entry = next(
        e for e in surfaces["workspace"] if e["operation_id"] == "tasks.create"
    )
    assert entry["response_schema_ref"] == "#/components/schemas/Task"


def test_idempotent_flag_per_method() -> None:
    """GET/HEAD/PUT/DELETE are idempotent; POST/PATCH are not."""
    schema = {
        "paths": {
            f"/api/v1/probe/{method.lower()}": {
                method.lower(): {
                    "operationId": f"probe.{method.lower()}",
                    "responses": {"200": {}},
                }
            }
            for method in ("GET", "HEAD", "PUT", "DELETE", "POST", "PATCH")
        }
    }
    surfaces = generate_surfaces(schema=schema)
    per_method = {e["http"]["method"]: e["idempotent"] for e in surfaces["workspace"]}
    assert per_method == {
        "GET": True,
        "HEAD": True,
        "PUT": True,
        "DELETE": True,
        "POST": False,
        "PATCH": False,
    }


def test_deterministic_double_generation() -> None:
    """Same schema twice → byte-identical serialisation."""
    schema = _synthetic_schema()
    first = generate_surfaces(schema=schema)
    second = generate_surfaces(schema=schema)
    assert first == second
    assert _codegen._serialise(first["workspace"]) == _codegen._serialise(
        second["workspace"]
    )
    assert _codegen._serialise(first["admin"]) == _codegen._serialise(second["admin"])


def test_sort_order_is_group_name_method_path() -> None:
    """The sort key is documented + stable across runs."""
    schema = {
        "paths": {
            "/api/v1/z": {
                "get": {"operationId": "z.list", "tags": ["z"], "responses": {}}
            },
            "/api/v1/a/{id}": {
                "get": {"operationId": "a.show", "tags": ["a"], "responses": {}},
                "delete": {"operationId": "a.delete", "tags": ["a"], "responses": {}},
            },
            "/api/v1/a": {
                "get": {"operationId": "a.list", "tags": ["a"], "responses": {}},
            },
        }
    }
    surfaces = generate_surfaces(schema=schema)
    keys = [
        (e["group"], e["name"], e["http"]["method"], e["http"]["path"])
        for e in surfaces["workspace"]
    ]
    assert keys == sorted(keys)
    # Concretely:
    assert keys == [
        ("a", "delete", "DELETE", "/api/v1/a/{id}"),
        ("a", "list", "GET", "/api/v1/a"),
        ("a", "show", "GET", "/api/v1/a/{id}"),
        ("z", "list", "GET", "/api/v1/z"),
    ]


def test_options_method_is_skipped() -> None:
    """CORS preflight methods never become CLI verbs."""
    schema = {
        "paths": {
            "/api/v1/resource": {
                "options": {
                    "operationId": "resource.options",
                    "responses": {"200": {}},
                }
            }
        }
    }
    surfaces = generate_surfaces(schema=schema)
    assert surfaces["workspace"] == []


def test_generate_surfaces_raises_on_collision() -> None:
    """Two ops that resolve to the same (group, name) raise cleanly.

    Reproduces the cd-bm77 finding: bare-host ``POST /me/tokens`` and
    workspace-scoped ``POST /w/{slug}/api/v1/auth/tokens`` both
    resolve to ``(tokens, create)`` under the naked heuristic. The
    codegen must refuse rather than silently emit a duplicate, and
    the error message must name the clashing operation ids so an API
    author can fix it with an ``x-cli`` override.
    """
    schema = {
        "paths": {
            "/api/v1/me/tokens": {
                "post": {
                    "operationId": "auth.me.tokens.mint",
                    "tags": ["tokens"],
                    "responses": {"201": {}},
                }
            },
            "/w/{slug}/api/v1/auth/tokens": {
                "post": {
                    "operationId": "auth.tokens.mint",
                    "tags": ["tokens"],
                    "responses": {"201": {}},
                }
            },
        }
    }
    with pytest.raises(CollisionError) as excinfo:
        generate_surfaces(schema=schema)
    message = str(excinfo.value)
    assert "'tokens'" in message
    assert "'create'" in message
    # Error names both operation ids so the fix is mechanical.
    assert "auth.me.tokens.mint" in message
    assert "auth.tokens.mint" in message


def test_generate_surfaces_collision_allows_cross_surface() -> None:
    """A (group, name) pair can repeat across admin vs workspace.

    Spec §13 partitions the CLI root into ``crewday ...`` (workspace)
    and ``crewday deploy ...`` (admin). The two trees are independent
    Click roots, so ``tokens list`` under workspace does not collide
    with ``tokens list`` under admin.
    """
    schema = {
        "paths": {
            "/w/{slug}/api/v1/auth/tokens": {
                "get": {
                    "operationId": "workspace.tokens.list",
                    "tags": ["tokens"],
                    "responses": {"200": {}},
                }
            },
            "/admin/api/v1/tokens": {
                "get": {
                    "operationId": "admin.tokens.list",
                    "tags": ["tokens"],
                    "responses": {"200": {}},
                }
            },
        }
    }
    surfaces = generate_surfaces(schema=schema)
    assert [e["operation_id"] for e in surfaces["workspace"]] == [
        "workspace.tokens.list"
    ]
    assert [e["operation_id"] for e in surfaces["admin"]] == ["admin.tokens.list"]


def _write_schema(path: Path, schema: dict[str, Any]) -> Path:
    """Write ``schema`` as JSON at ``path`` and return ``path``.

    Tests pass the resulting path through ``--openapi`` so the codegen
    reads it instead of the committed ``docs/api/openapi.json``.
    """
    path.write_text(json.dumps(schema), encoding="utf-8")
    return path


def test_main_exits_two_on_collision(tmp_path: Path) -> None:
    """``main`` renders CollisionError as a clean exit-2 without traceback."""
    schema_path = _write_schema(
        tmp_path / "openapi.json",
        {
            "paths": {
                "/api/v1/me/tokens": {
                    "post": {
                        "operationId": "auth.me.tokens.mint",
                        "tags": ["tokens"],
                        "responses": {"201": {}},
                    }
                },
                "/w/{slug}/api/v1/auth/tokens": {
                    "post": {
                        "operationId": "auth.tokens.mint",
                        "tags": ["tokens"],
                        "responses": {"201": {}},
                    }
                },
            }
        },
    )
    surface = tmp_path / "_surface.json"
    surface_admin = tmp_path / "_surface_admin.json"
    excl = tmp_path / "excl.yaml"
    excl.write_text("exclusions: []\n")

    exit_code = main(
        [
            "--surface",
            str(surface),
            "--surface-admin",
            str(surface_admin),
            "--exclusions",
            str(excl),
            "--openapi",
            str(schema_path),
        ]
    )
    assert exit_code == 2
    # Nothing written on failure.
    assert not surface.exists()
    assert not surface_admin.exists()


def test_x_agent_confirm_copied_verbatim() -> None:
    schema = {
        "paths": {
            "/api/v1/expenses/{id}/approve": {
                "post": {
                    "operationId": "expenses.approve",
                    "tags": ["expenses"],
                    "responses": {"200": {}},
                    "x-agent-confirm": {
                        "summary": "Approve expense {id}?",
                        "risk": "medium",
                    },
                }
            }
        }
    }
    surfaces = generate_surfaces(schema=schema)
    entry = surfaces["workspace"][0]
    assert entry["x_agent_confirm"] == {
        "summary": "Approve expense {id}?",
        "risk": "medium",
    }


# ---------------------------------------------------------------------------
# CLI modes
# ---------------------------------------------------------------------------


def test_main_writes_files(tmp_path: Path) -> None:
    """``python -m crewday._codegen`` writes both surface files."""
    schema_path = _write_schema(tmp_path / "openapi.json", _synthetic_schema())
    surface = tmp_path / "_surface.json"
    surface_admin = tmp_path / "_surface_admin.json"
    excl = tmp_path / "excl.yaml"
    excl.write_text("exclusions: []\n")

    exit_code = main(
        [
            "--surface",
            str(surface),
            "--surface-admin",
            str(surface_admin),
            "--exclusions",
            str(excl),
            "--openapi",
            str(schema_path),
        ]
    )
    assert exit_code == 0
    assert surface.is_file()
    assert surface_admin.is_file()

    workspace = json.loads(surface.read_text())
    admin = json.loads(surface_admin.read_text())
    assert len(workspace) >= 1
    # `/admin/api/v1/workspaces` → admin surface.
    assert [e["operation_id"] for e in admin] == ["workspaces.list"]


def test_main_check_mode_detects_drift(tmp_path: Path) -> None:
    """``--check`` exits 1 when the committed file is stale."""
    schema_path = _write_schema(tmp_path / "openapi.json", _synthetic_schema())
    surface = tmp_path / "_surface.json"
    surface_admin = tmp_path / "_surface_admin.json"
    surface.write_text("[]\n")
    surface_admin.write_text("[]\n")
    excl = tmp_path / "excl.yaml"
    excl.write_text("exclusions: []\n")

    exit_code = main(
        [
            "--check",
            "--surface",
            str(surface),
            "--surface-admin",
            str(surface_admin),
            "--exclusions",
            str(excl),
            "--openapi",
            str(schema_path),
        ]
    )
    assert exit_code == 1


def test_main_check_mode_passes_when_in_sync(tmp_path: Path) -> None:
    """``--check`` exits 0 when committed == fresh."""
    schema_path = _write_schema(tmp_path / "openapi.json", _synthetic_schema())
    surface = tmp_path / "_surface.json"
    surface_admin = tmp_path / "_surface_admin.json"
    excl = tmp_path / "excl.yaml"
    excl.write_text("exclusions: []\n")

    base_argv = [
        "--surface",
        str(surface),
        "--surface-admin",
        str(surface_admin),
        "--exclusions",
        str(excl),
        "--openapi",
        str(schema_path),
    ]
    # Seed with a fresh write.
    main(base_argv)
    # Then --check from the same schema should succeed.
    exit_code = main(["--check", *base_argv])
    assert exit_code == 0


def test_main_dry_run_does_not_touch_disk(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--dry-run`` prints JSON to stdout and writes nothing."""
    schema_path = _write_schema(tmp_path / "openapi.json", _synthetic_schema())
    surface = tmp_path / "_surface.json"
    surface_admin = tmp_path / "_surface_admin.json"
    excl = tmp_path / "excl.yaml"
    excl.write_text("exclusions: []\n")

    exit_code = main(
        [
            "--dry-run",
            "--surface",
            str(surface),
            "--surface-admin",
            str(surface_admin),
            "--exclusions",
            str(excl),
            "--openapi",
            str(schema_path),
        ]
    )
    assert exit_code == 0
    assert not surface.exists()
    assert not surface_admin.exists()

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert set(payload.keys()) == {"_surface.json", "_surface_admin.json"}


def test_main_write_is_idempotent(tmp_path: Path) -> None:
    """A re-run over an in-sync file does not rewrite the mtime.

    Prevents spurious diffs when an agent runs the codegen for a
    sanity check; important because the committed files are part of
    the CI parity gate.
    """
    schema_path = _write_schema(tmp_path / "openapi.json", _synthetic_schema())
    surface = tmp_path / "_surface.json"
    surface_admin = tmp_path / "_surface_admin.json"
    excl = tmp_path / "excl.yaml"
    excl.write_text("exclusions: []\n")

    argv = [
        "--surface",
        str(surface),
        "--surface-admin",
        str(surface_admin),
        "--exclusions",
        str(excl),
        "--openapi",
        str(schema_path),
    ]
    main(argv)
    mtime = surface.stat().st_mtime_ns

    # Second run: contents identical → no rewrite.
    main(argv)
    assert surface.stat().st_mtime_ns == mtime


def test_load_committed_schema_missing_file_raises(tmp_path: Path) -> None:
    """Missing schema file gets a pointer to ``make openapi``."""
    with pytest.raises(FileNotFoundError, match="make openapi"):
        load_committed_schema(tmp_path / "nope.json")


def test_load_committed_schema_rejects_non_object(tmp_path: Path) -> None:
    """A JSON array at the top level fails fast with a clear message."""
    path = tmp_path / "bad.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="must be a JSON object"):
        load_committed_schema(path)


def test_default_openapi_path_points_at_committed_schema() -> None:
    """``DEFAULT_OPENAPI_PATH`` resolves to the canonical artefact."""
    # Don't load the file (it's large) — just confirm the layout
    # ``<repo>/docs/api/openapi.json`` matches what ``make openapi``
    # writes from ``scripts/regen_openapi.py``.
    assert DEFAULT_OPENAPI_PATH.name == "openapi.json"
    assert DEFAULT_OPENAPI_PATH.parent.name == "api"
    assert DEFAULT_OPENAPI_PATH.parent.parent.name == "docs"
    assert DEFAULT_OPENAPI_PATH.is_file(), (
        "docs/api/openapi.json missing; run 'make openapi' to regenerate"
    )


# ---------------------------------------------------------------------------
# End-to-end parity gate
# ---------------------------------------------------------------------------


def test_committed_schema_matches_committed_surfaces() -> None:
    """The committed surface files match a fresh codegen run.

    Equivalent to ``python -m crewday._codegen --check`` in CI. Reads
    ``docs/api/openapi.json`` (kept fresh by ``make openapi-check``)
    so the codegen stays a transform-only build step (cd-uky5). If
    this fails, either:

    * someone added an endpoint without running the codegen — run
      ``make openapi`` then ``uv run python -m crewday._codegen`` and
      commit both; or
    * the exclusions file is out of sync — update it with a reason.
    """
    schema = load_committed_schema()
    exclusions = load_exclusions(DEFAULT_EXCLUSIONS_PATH)
    surfaces = generate_surfaces(schema=schema, exclusions=exclusions)

    committed_workspace = json.loads(DEFAULT_SURFACE_PATH.read_text())
    committed_admin = json.loads(DEFAULT_SURFACE_ADMIN_PATH.read_text())

    assert surfaces["workspace"] == committed_workspace, (
        "committed _surface.json out of sync — run 'uv run python -m crewday._codegen'"
    )
    assert surfaces["admin"] == committed_admin, (
        "committed _surface_admin.json out of sync — run "
        "'uv run python -m crewday._codegen'"
    )


def test_committed_schema_has_no_duplicate_group_name_pairs() -> None:
    """Each ``(group, name)`` pair on every surface is unique.

    The runtime (cd-lato) registers at most one Click command per
    ``(group, verb)`` pair; a duplicate means one command would
    silently shadow the other. ``generate_surfaces`` raises
    :class:`CollisionError` on a clash, so reaching this point means
    the committed surfaces are collision-free. The assertion is a
    belt-and-braces guard against a future refactor that moves the
    check elsewhere.
    """
    schema = load_committed_schema()
    exclusions = load_exclusions(DEFAULT_EXCLUSIONS_PATH)
    surfaces = generate_surfaces(schema=schema, exclusions=exclusions)

    for surface_kind, entries in surfaces.items():
        pairs = [(e["group"], e["name"]) for e in entries]
        assert len(pairs) == len(set(pairs)), (
            f"duplicate (group, name) pairs on the {surface_kind} surface: {pairs}"
        )


# ---------------------------------------------------------------------------
# Boundary contract: codegen never reaches into ``app.*``
# ---------------------------------------------------------------------------


def test_codegen_module_does_not_import_app() -> None:
    """The codegen module statically imports nothing under ``app.*``.

    The whole point of cd-uky5 is to make the codegen a pure
    transformer over ``docs/api/openapi.json``. A static AST sweep is
    cheaper and more precise than waiting for ``uv run lint-imports``
    to flag a regression: it pins the contract directly inside the
    test suite a future refactor would already be running.
    """
    source = inspect.getsource(_codegen)
    tree = ast.parse(source)
    forbidden_roots = {"app"}
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in forbidden_roots:
                    bad.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            root = node.module.split(".")[0]
            if root in forbidden_roots:
                bad.append(node.module)
    assert bad == [], (
        "crewday._codegen must not import app.* — it is a transform-only "
        f"build step over docs/api/openapi.json (cd-uky5). Found: {bad}"
    )


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def test_serialise_ends_with_newline() -> None:
    out = _codegen._serialise([])
    assert out.endswith("\n")
    # Still valid JSON (empty list).
    assert json.loads(out) == []


def test_serialise_sort_keys_is_stable() -> None:
    """Keys are sorted — ``idempotent`` precedes ``path_params`` etc."""
    out = _codegen._serialise(
        [
            {"zeta": 1, "alpha": 2, "mu": 3},
        ]
    )
    payload = json.loads(out)
    # json.dumps(sort_keys=True) emits keys in alphabetical order.
    # The dict we parse back does not preserve that ordering, so we
    # check the raw string itself for the ``alpha`` key coming first.
    assert out.index("alpha") < out.index("mu") < out.index("zeta")
    assert payload == [{"zeta": 1, "alpha": 2, "mu": 3}]


# ---------------------------------------------------------------------------
# YAML shape of the committed exclusions
# ---------------------------------------------------------------------------


def test_committed_exclusions_yaml_is_well_formed() -> None:
    """Raw YAML loads and carries at least the canonical spec entries."""
    raw = yaml.safe_load(DEFAULT_EXCLUSIONS_PATH.read_text())
    assert isinstance(raw, dict)
    assert isinstance(raw.get("exclusions"), list)
    op_ids = {
        entry.get("operation_id")
        for entry in raw["exclusions"]
        if "operation_id" in entry
    }
    # Spec §13 "Exclusions" canonical list members:
    assert "auth.passkey.login_start" in op_ids
    assert "auth.passkey.login_finish" in op_ids
    assert "files.blob" in op_ids
    assert "healthz" in op_ids
    assert "readyz" in op_ids
    assert "version.get" in op_ids
