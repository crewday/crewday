"""Unit tests for :mod:`crewday._runtime`.

Every scenario uses :class:`httpx.MockTransport` — the same pattern as
:mod:`cli.tests.test_client` — to wire a fake HTTP layer under the
generated Click commands. The real :class:`crewday._client.CrewdayClient`
is exercised end-to-end (request building, retries, idempotency-key
forwarding); the runtime is responsible for *constructing* commands
and *invoking* the client correctly per descriptor entry, which is
exactly what the assertions verify.

Coverage maps to the §13 §"Runtime command construction",
§"Parameter mapping", §"Pagination", §"Output", §"Idempotency-Key"
spec sections.
"""

from __future__ import annotations

import json
import pathlib
import random
from collections.abc import Callable
from typing import Any

import click
import httpx
from click.testing import CliRunner
from crewday._client import CrewdayClient
from crewday._globals import CrewdayContext
from crewday._main import ExitCode, _register_surface_commands_once, root
from crewday._runtime import (
    DEFAULT_SURFACE_ADMIN_PATH,
    DEFAULT_SURFACE_PATH,
    SurfaceEntry,
    load_surface,
    register_generated_commands,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _no_sleep(_seconds: float) -> None:
    """Stub for retries — never wait real wall time in tests."""
    return None


def _client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[CrewdayContext], CrewdayClient]:
    """Return a ``ClientFactory`` that builds a mocked :class:`CrewdayClient`.

    The factory closes over ``handler`` so each test pins its own HTTP
    surface. The returned client is wired with a deterministic RNG and
    a no-op sleep so retry timing doesn't slow the suite down.
    """

    def factory(ctx: CrewdayContext) -> CrewdayClient:
        return CrewdayClient(
            base_url="https://api.test.local",
            token="test-token",
            workspace=ctx.workspace,
            transport=httpx.MockTransport(handler),
            rng=random.Random(0),
            sleep=_no_sleep,
        )

    return factory


def _build_root(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    workspace_path: pathlib.Path = DEFAULT_SURFACE_PATH,
    admin_path: pathlib.Path = DEFAULT_SURFACE_ADMIN_PATH,
) -> click.Group:
    """Build a fresh root + register generated commands wired to ``handler``.

    Uses :func:`click.group` (the decorator factory) so the global-flag
    callback is wired the same way :data:`crewday._main.root` does it
    — programmatically constructing a :class:`click.Group` and trying
    to attach a ``@callback()`` afterwards trips Click 8.2's stricter
    typing (``Group.callback`` is a plain attribute, not a decorator).
    """

    from crewday._globals import OUTPUT_CHOICES, OutputMode

    @click.group(name="test-crewday")
    @click.option("--workspace", default="smoke")
    @click.option("--output", type=click.Choice(OUTPUT_CHOICES), default="json")
    @click.pass_context
    def test_root(ctx: click.Context, workspace: str, output: str) -> None:
        # ``click.Choice`` returns one of the four literals verbatim;
        # narrow explicitly so the dataclass field stays strictly typed.
        narrowed: OutputMode
        match output:
            case "json":
                narrowed = "json"
            case "yaml":
                narrowed = "yaml"
            case "table":
                narrowed = "table"
            case "ndjson":
                narrowed = "ndjson"
            case _:
                raise click.BadParameter(f"unexpected --output: {output!r}")
        ctx.obj = CrewdayContext(
            profile=None,
            workspace=workspace,
            output=narrowed,
        )

    register_generated_commands(
        test_root,
        client_factory=_client_factory(handler),
        workspace_path=workspace_path,
        admin_path=admin_path,
    )
    return test_root


def _write_surface(
    tmp_path: pathlib.Path,
    *,
    workspace: list[dict[str, Any]] | None = None,
    admin: list[dict[str, Any]] | None = None,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Write synthetic surface files; clear the loader cache."""
    workspace_path = tmp_path / "_surface.json"
    admin_path = tmp_path / "_surface_admin.json"
    workspace_path.write_text(json.dumps(workspace or []), encoding="utf-8")
    admin_path.write_text(json.dumps(admin or []), encoding="utf-8")
    load_surface.cache_clear()
    return workspace_path, admin_path


def _entry(
    *,
    name: str = "demo",
    group: str = "demo",
    method: str = "GET",
    path: str = "/api/v1/demo",
    operation_id: str = "demo.op",
    idempotent: bool = True,
    path_params: list[dict[str, Any]] | None = None,
    query_params: list[dict[str, Any]] | None = None,
    body_schema_ref: str | None = None,
    response_schema_ref: str | None = None,
    x_cli: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one synthetic surface entry — exactly the codegen shape."""
    return {
        "name": name,
        "group": group,
        "operation_id": operation_id,
        "summary": f"summary for {operation_id}",
        "http": {"method": method, "path": path},
        "idempotent": idempotent,
        "path_params": path_params or [],
        "query_params": query_params or [],
        "body_schema_ref": body_schema_ref,
        "response_schema_ref": response_schema_ref,
        "x_cli": x_cli,
        "x_agent_confirm": None,
    }


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def test_loader_merges_admin_and_workspace(tmp_path: pathlib.Path) -> None:
    """Both descriptor files are loaded and merged into one tuple."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[_entry(name="ws-only", group="ws", operation_id="ws.op")],
        admin=[_entry(name="admin-only", group="admin", operation_id="admin.op")],
    )

    entries = load_surface(workspace_path=workspace_path, admin_path=admin_path)
    operation_ids = {e.operation_id for e in entries}
    assert operation_ids == {"ws.op", "admin.op"}

    # Cache hit: a second call with the same arguments returns the same
    # tuple instance (functools.cache holds the result).
    again = load_surface(workspace_path=workspace_path, admin_path=admin_path)
    assert again is entries


def test_loader_empty_admin_surface_ok(tmp_path: pathlib.Path) -> None:
    """``_surface_admin.json == []`` is legal — admin tree is empty today."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[_entry(name="ws-only", group="ws", operation_id="ws.op")],
        admin=[],
    )
    entries = load_surface(workspace_path=workspace_path, admin_path=admin_path)
    assert len(entries) == 1
    assert entries[0].operation_id == "ws.op"


def test_loader_typed_surface_entry_fields(tmp_path: pathlib.Path) -> None:
    """The typed dataclass surfaces every JSON field as the right type."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="passkey-revoke",
                group="auth",
                method="DELETE",
                path="/w/{slug}/api/v1/auth/passkey/{credential_id}",
                operation_id="auth.passkey.revoke",
                idempotent=True,
                path_params=[
                    {
                        "name": "credential_id",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                x_cli={"group": "auth", "verb": "passkey-revoke", "mutates": True},
            )
        ],
    )
    entries = load_surface(workspace_path=workspace_path, admin_path=admin_path)
    assert isinstance(entries[0], SurfaceEntry)
    assert entries[0].cli_group == "auth"
    assert entries[0].cli_verb == "passkey-revoke"
    assert entries[0].http.method == "DELETE"
    assert entries[0].path_params[0].name == "credential_id"


# ---------------------------------------------------------------------------
# Path / query / body wiring
# ---------------------------------------------------------------------------


def test_path_params_become_click_options(tmp_path: pathlib.Path) -> None:
    """Required path params land as required Click options with type-coercion."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="show",
                group="demo",
                method="GET",
                path="/api/v1/demo/{str_id}/{int_id}/{flag}",
                operation_id="demo.show",
                path_params=[
                    {"name": "str_id", "required": True, "schema": {"type": "string"}},
                    {"name": "int_id", "required": True, "schema": {"type": "integer"}},
                    {"name": "flag", "required": True, "schema": {"type": "boolean"}},
                ],
            )
        ],
    )

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()

    # All three path params required: missing one trips Click's own
    # ``MissingParameter`` (exit 2).
    missing = runner.invoke(test_root, ["demo", "show", "--str-id", "abc"])
    assert missing.exit_code == 2

    # All three supplied with the right types — int and bool coerce.
    ok = runner.invoke(
        test_root,
        ["demo", "show", "--str-id", "abc", "--int-id", "42", "--flag", "true"],
    )
    assert ok.exit_code == 0, ok.output
    assert seen["url"].endswith("/api/v1/demo/abc/42/True")


def test_query_params_optional_with_default(tmp_path: pathlib.Path) -> None:
    """An optional query param with ``schema.default`` defaults at the CLI."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="list",
                group="demo",
                method="GET",
                path="/api/v1/demo",
                operation_id="demo.list",
                query_params=[
                    {
                        "name": "limit",
                        "required": False,
                        "schema": {"type": "integer", "default": 50},
                    }
                ],
            )
        ],
    )

    seen: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # ``QueryParams`` returns the last value per key as a plain str
        # under ``__getitem__``; the dict-cast walks the multi-dict and
        # yields one (key, str) entry. We assert the materialised dict
        # matches the expected single-value flag set.
        seen["params"] = {k: request.url.params[k] for k in request.url.params}
        return httpx.Response(200, json={"ok": True})

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()

    result = runner.invoke(test_root, ["demo", "list"])
    assert result.exit_code == 0, result.output
    assert seen["params"] == {"limit": "50"}


def test_query_array_param_uses_multiple_true(tmp_path: pathlib.Path) -> None:
    """Array-typed query params accept ``--tag a --tag b``."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="list",
                group="demo",
                method="GET",
                path="/api/v1/demo",
                operation_id="demo.list",
                query_params=[
                    {
                        "name": "tag",
                        "required": False,
                        "schema": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    }
                ],
            )
        ],
    )

    seen: dict[str, list[tuple[str, str]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # ``request.url.params`` is a multi-dict; capture as a list so we
        # see both repetitions.
        seen["params"] = list(request.url.params.multi_items())
        return httpx.Response(200, json={"ok": True})

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()

    result = runner.invoke(test_root, ["demo", "list", "--tag", "a", "--tag", "b"])
    assert result.exit_code == 0, result.output
    assert seen["params"] == [("tag", "a"), ("tag", "b")]


def test_field_repeat_builds_body_dict(tmp_path: pathlib.Path) -> None:
    """``--field a=1 --field b=2`` produces ``{"a": "1", "b": "2"}``."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="create",
                group="demo",
                method="POST",
                path="/api/v1/demo",
                operation_id="demo.create",
                idempotent=False,
                body_schema_ref="#/components/schemas/Demo",
            )
        ],
    )

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "x"})

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()

    result = runner.invoke(
        test_root,
        ["demo", "create", "--field", "a=1", "--field", "b=2"],
    )
    assert result.exit_code == 0, result.output
    assert captured["body"] == {"a": "1", "b": "2"}


def test_field_last_write_wins(tmp_path: pathlib.Path) -> None:
    """Repeating the same key keeps the last value."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="create",
                group="demo",
                method="POST",
                path="/api/v1/demo",
                operation_id="demo.create",
                idempotent=False,
                body_schema_ref="#/components/schemas/Demo",
            )
        ],
    )

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "x"})

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()

    result = runner.invoke(
        test_root,
        ["demo", "create", "--field", "a=1", "--field", "a=2"],
    )
    assert result.exit_code == 0, result.output
    assert captured["body"] == {"a": "2"}


def test_field_and_body_file_conflict_errors(tmp_path: pathlib.Path) -> None:
    """``--field`` + ``--body-file`` → non-zero exit with a clear message."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="create",
                group="demo",
                method="POST",
                path="/api/v1/demo",
                operation_id="demo.create",
                idempotent=False,
                body_schema_ref="#/components/schemas/Demo",
            )
        ],
    )

    body_path = tmp_path / "body.json"
    body_path.write_text(json.dumps({"x": 1}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("HTTP layer should not be reached on a usage error")

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()

    result = runner.invoke(
        test_root,
        [
            "demo",
            "create",
            "--field",
            "a=1",
            "--body-file",
            str(body_path),
        ],
    )
    assert result.exit_code != 0
    combined = (result.output or "") + (result.stderr or "")
    assert "mutually exclusive" in combined.lower()


def test_body_file_loads_json(tmp_path: pathlib.Path) -> None:
    """``--body-file path.json`` parses the file and sends as JSON body."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="create",
                group="demo",
                method="POST",
                path="/api/v1/demo",
                operation_id="demo.create",
                idempotent=False,
                body_schema_ref="#/components/schemas/Demo",
            )
        ],
    )

    body_path = tmp_path / "body.json"
    body_payload = {"name": "alice", "tags": ["a", "b"], "count": 3}
    body_path.write_text(json.dumps(body_payload), encoding="utf-8")

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()

    result = runner.invoke(
        test_root,
        ["demo", "create", "--body-file", str(body_path)],
    )
    assert result.exit_code == 0, result.output
    assert captured["body"] == body_payload


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotency_key_auto_attached_on_post(tmp_path: pathlib.Path) -> None:
    """A POST without an explicit key auto-attaches a ULID."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="create",
                group="demo",
                method="POST",
                path="/api/v1/demo",
                operation_id="demo.create",
                idempotent=False,
            )
        ],
    )

    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"ok": True})

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()

    result = runner.invoke(test_root, ["demo", "create"])
    assert result.exit_code == 0, result.output
    key = captured["key"]
    assert key is not None and len(key) == 26  # ULID canonical length


def test_idempotency_key_override_honoured(tmp_path: pathlib.Path) -> None:
    """An explicit ``--idempotency-key`` overrides the auto-generated value."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="create",
                group="demo",
                method="POST",
                path="/api/v1/demo",
                operation_id="demo.create",
                idempotent=False,
            )
        ],
    )

    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"ok": True})

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()

    result = runner.invoke(
        test_root,
        ["demo", "create", "--idempotency-key", "fixed-key-foo"],
    )
    assert result.exit_code == 0, result.output
    assert captured["key"] == "fixed-key-foo"


def test_idempotency_key_not_attached_on_delete(tmp_path: pathlib.Path) -> None:
    """Spec §12 only lists POST as cached; DELETE must not auto-attach."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="revoke",
                group="demo",
                method="DELETE",
                path="/api/v1/demo/{id}",
                operation_id="demo.revoke",
                idempotent=True,
                path_params=[
                    {"name": "id", "required": True, "schema": {"type": "string"}}
                ],
            )
        ],
    )

    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"ok": True})

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()

    result = runner.invoke(test_root, ["demo", "revoke", "--id", "abc"])
    assert result.exit_code == 0, result.output
    assert captured["key"] is None


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _paginated_entry() -> dict[str, Any]:
    """Return a synthetic paginated list entry mirroring §12 envelope."""
    return _entry(
        name="list",
        group="demo",
        method="GET",
        path="/api/v1/demo",
        operation_id="demo.list",
        query_params=[
            {
                "name": "cursor",
                "required": False,
                "schema": {"type": "string"},
            },
            {
                "name": "limit",
                "required": False,
                "schema": {"type": "integer", "default": 50},
            },
        ],
    )


def _paginated_handler() -> Callable[[httpx.Request], httpx.Response]:
    """Three-page handler walked by ``cursor=p2`` and ``cursor=p3``."""
    pages: dict[str | None, dict[str, Any]] = {
        None: {
            "data": [{"id": 1}, {"id": 2}],
            "next_cursor": "p2",
            "has_more": True,
        },
        "p2": {"data": [{"id": 3}], "next_cursor": "p3", "has_more": True},
        "p3": {
            "data": [{"id": 4}, {"id": 5}],
            "next_cursor": None,
            "has_more": False,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        return httpx.Response(200, json=pages[cursor])

    return handler


def test_all_flag_follows_cursor(tmp_path: pathlib.Path) -> None:
    """``--all`` walks every page via ``client.iterate`` and aggregates."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[_paginated_entry()],
    )
    test_root = _build_root(
        _paginated_handler(),
        workspace_path=workspace_path,
        admin_path=admin_path,
    )
    runner = CliRunner()

    result = runner.invoke(test_root, ["demo", "list", "--all"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}]


def test_all_flag_streams_ndjson_when_output_ndjson(
    tmp_path: pathlib.Path,
) -> None:
    """Under ``-o ndjson``, ``--all`` emits one JSON object per line."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[_paginated_entry()],
    )
    test_root = _build_root(
        _paginated_handler(),
        workspace_path=workspace_path,
        admin_path=admin_path,
    )
    runner = CliRunner()

    result = runner.invoke(
        test_root,
        ["--output", "ndjson", "demo", "list", "--all"],
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines]
    assert parsed == [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}]


def test_empty_ndjson_response_emits_no_blank_line(tmp_path: pathlib.Path) -> None:
    """An empty list under ``-o ndjson`` produces zero records."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[_entry(name="list", group="demo", path="/api/v1/demo")],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()

    result = runner.invoke(test_root, ["--output", "ndjson", "demo", "list"])

    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_no_all_flag_returns_single_page(tmp_path: pathlib.Path) -> None:
    """Without ``--all`` the list verb returns the server's first page verbatim.

    The ``next_cursor`` field stays in the envelope so an agent can
    walk subsequent pages by passing ``--cursor`` (well, by setting
    the cursor query — currently elided from the option set; this is
    the documented escape via ``--all`` for now).
    """
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[_paginated_entry()],
    )
    test_root = _build_root(
        _paginated_handler(),
        workspace_path=workspace_path,
        admin_path=admin_path,
    )
    runner = CliRunner()

    result = runner.invoke(test_root, ["demo", "list"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"] == [{"id": 1}, {"id": 2}]
    assert payload["next_cursor"] == "p2"
    assert payload["has_more"] is True


def test_generated_command_honours_yaml_output(tmp_path: pathlib.Path) -> None:
    """Normal responses route through the active output formatter."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[_entry(name="show", group="demo", path="/api/v1/demo")],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "task-1", "state": "open"})

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()

    result = runner.invoke(test_root, ["--output", "yaml", "demo", "show"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "id: task-1\nstate: open"


def test_generated_command_passes_x_cli_columns_to_table_formatter(
    tmp_path: pathlib.Path,
) -> None:
    """Table output uses ``x-cli-columns`` from the surface entry."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="list",
                group="demo",
                path="/api/v1/demo",
                x_cli={
                    "group": "demo",
                    "verb": "list",
                    "x-cli-columns": [
                        {"key": "state", "label": "State"},
                        {"key": "id", "label": "Task"},
                    ],
                },
            )
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "task-1", "state": "open", "hidden": "x"}]},
        )

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()

    result = runner.invoke(test_root, ["--output", "table", "demo", "list"])

    assert result.exit_code == 0, result.output
    assert "State" in result.output
    assert "Task" in result.output
    assert "open" in result.output
    assert "task-1" in result.output
    assert "hidden" not in result.output


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


def test_api_error_formats_as_json_for_script_output(tmp_path: pathlib.Path) -> None:
    """``ApiError`` uses the structured formatter under ``-o json``."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="show",
                group="demo",
                method="GET",
                path="/api/v1/demo/{id}",
                operation_id="demo.show",
                path_params=[
                    {"name": "id", "required": True, "schema": {"type": "string"}}
                ],
            )
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "type": "https://crewday.dev/errors/not_found",
                "title": "Not Found",
                "detail": "no such demo",
            },
        )

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()

    result = runner.invoke(test_root, ["demo", "show", "--id", "missing"])

    assert result.exit_code == ExitCode.CLIENT_ERROR
    rendered = result.stderr or result.output
    assert json.loads(rendered) == {
        "status": 404,
        "code": "not_found",
        "message": "no such demo",
        "details": {
            "type": "https://crewday.dev/errors/not_found",
            "title": "Not Found",
            "detail": "no such demo",
        },
    }


# ---------------------------------------------------------------------------
# Workspace slug substitution
# ---------------------------------------------------------------------------


def test_workspace_slug_substituted_into_path(tmp_path: pathlib.Path) -> None:
    """The ``{slug}`` placeholder is filled from ``--workspace``."""
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="list",
                group="demo",
                method="GET",
                path="/w/{slug}/api/v1/demo",
                operation_id="demo.list",
            )
        ],
    )

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()
    result = runner.invoke(test_root, ["--workspace", "acme", "demo", "list"])
    assert result.exit_code == 0, result.output
    assert "/w/acme/api/v1/demo" in seen["url"]


def test_missing_workspace_on_slug_path_raises_config_error(
    tmp_path: pathlib.Path,
) -> None:
    """A ``/w/{slug}/...`` verb with no workspace lands on §13 exit code 5.

    :class:`ConfigError` is :class:`crewday._main.CrewdayError` with
    ``exit_code=5``; Click's :class:`ClickException` plumbing routes
    that to the process exit. The handler is a tripwire — the CLI must
    error before any HTTP attempt.
    """
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="list",
                group="demo",
                method="GET",
                path="/w/{slug}/api/v1/demo",
                operation_id="demo.list",
            )
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("HTTP layer should not be reached without --workspace")

    # ``_build_root`` defaults ``--workspace`` to ``"smoke"``; pass the
    # empty string explicitly to simulate the unset case (Click's
    # ``str`` option accepts an empty string verbatim, which the
    # runtime's ``not workspace`` check treats as missing).
    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()
    result = runner.invoke(test_root, ["--workspace", "", "demo", "list"])
    # ConfigError carries exit_code = 5 (CONFIG_ERROR slot per §13).
    assert result.exit_code == 5, result.output
    combined = (result.output or "") + (result.stderr or "")
    assert "no workspace" in combined.lower()


def test_path_param_values_are_url_encoded(tmp_path: pathlib.Path) -> None:
    """A path-param value with reserved characters is percent-encoded.

    The next agent who adds a free-text path param shouldn't be able
    to silently route to a different endpoint by passing ``/`` or
    ``..`` in the value. URL-encoding closes that escape hatch in one
    place — the resolver — rather than leaving every call site to
    remember.
    """
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="show",
                group="demo",
                method="GET",
                path="/api/v1/demo/{id}",
                operation_id="demo.show",
                path_params=[
                    {"name": "id", "required": True, "schema": {"type": "string"}}
                ],
            )
        ],
    )

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()
    result = runner.invoke(test_root, ["demo", "show", "--id", "foo/bar baz"])
    assert result.exit_code == 0, result.output
    # ``/`` → %2F, space → %20. The path stays under /api/v1/demo/<id>
    # rather than escaping into /api/v1/demo/foo/bar baz/...
    assert "/api/v1/demo/foo%2Fbar%20baz" in seen["url"]


def test_patch_body_wiring_mirrors_post(tmp_path: pathlib.Path) -> None:
    """PATCH inherits ``--field`` / ``--body-file`` from ``_BODY_METHODS``.

    Guards against a future regression that drops PATCH from the body
    methods set — the runtime would silently ignore ``--field`` on
    PATCH and the user would see a confusing ``no such option`` error
    rather than the documented body-construction path.
    """
    workspace_path, admin_path = _write_surface(
        tmp_path,
        workspace=[
            _entry(
                name="update",
                group="demo",
                method="PATCH",
                path="/api/v1/demo/{id}",
                operation_id="demo.update",
                idempotent=False,
                body_schema_ref="#/components/schemas/Demo",
                path_params=[
                    {"name": "id", "required": True, "schema": {"type": "string"}}
                ],
            )
        ],
    )

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["method"] = request.method
        # PATCH must NOT auto-attach Idempotency-Key per §12 (POST only).
        captured["idempotency_key"] = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"ok": True})

    test_root = _build_root(
        handler, workspace_path=workspace_path, admin_path=admin_path
    )
    runner = CliRunner()

    result = runner.invoke(
        test_root,
        ["demo", "update", "--id", "abc", "--field", "name=alice"],
    )
    assert result.exit_code == 0, result.output
    assert captured["method"] == "PATCH"
    assert captured["body"] == {"name": "alice"}
    assert captured["idempotency_key"] is None


# ---------------------------------------------------------------------------
# End-to-end: real descriptor + real Click root
# ---------------------------------------------------------------------------


def test_root_help_shows_generated_groups() -> None:
    """``crewday --help`` lists the codegen groups (e.g. ``auth``)."""
    # Mount the production descriptors on the real root group via the
    # same path :func:`crewday._main.main` uses at startup.
    _register_surface_commands_once()
    runner = CliRunner()
    result = runner.invoke(root, ["--help"])
    assert result.exit_code == 0, result.output
    # ``auth`` is present in the committed _surface.json.
    assert "auth" in result.output
