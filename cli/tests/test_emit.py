"""Tests for the shared emit path: ``--jq`` and ``--no-color`` (§13).

These flags are threaded onto :class:`crewday._globals.CrewdayContext`
by the root group (cd-jecm3) and consumed here through the single
:mod:`crewday._emit` write path so both generated commands and the
hand-written overrides honour them uniformly.

Coverage:

* ``--no-color`` forces ANSI off in table output even on a (simulated)
  TTY — at the formatter level and end-to-end through a generated
  command.
* ``--jq`` filters JSON output; an invalid expression and a missing
  ``jq`` binary raise distinct, clear errors; no jq subprocess is
  spawned unless ``--jq`` is passed.
* The filter applies to both a generated command and an override.
"""

from __future__ import annotations

import json
import pathlib
import random
from collections.abc import Callable
from typing import Any

import click
import httpx
import pytest
from click.testing import CliRunner
from crewday import _emit, _output
from crewday._client import CrewdayClient
from crewday._globals import OUTPUT_CHOICES, CrewdayContext, OutputMode
from crewday._main import ConfigError, CrewdayError, ExitCode
from crewday._output import format_response
from crewday._overrides import tasks as tasks_override
from crewday._runtime import (
    load_surface,
    register_generated_commands,
)

# ---------------------------------------------------------------------------
# --no-color: formatter level
# ---------------------------------------------------------------------------


def test_no_color_forces_ansi_off_even_when_color_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``no_color=True`` beats the TTY/NO_COLOR heuristic in table mode."""
    # Simulate an interactive colour-capable stdout so the default path
    # would emit ANSI — the point of --no-color is to override that.
    monkeypatch.setattr(_output, "_use_color", lambda: True)

    coloured = format_response([{"id": "task-1"}], "table")
    plain = format_response([{"id": "task-1"}], "table", no_color=True)

    assert "\x1b[" in coloured
    assert "\x1b[" not in plain


# ---------------------------------------------------------------------------
# --jq: engine behaviour at the emit level
# ---------------------------------------------------------------------------


def _ctx(**overrides: Any) -> CrewdayContext:
    base: dict[str, Any] = {"profile": None, "workspace": "smoke", "output": "json"}
    base.update(overrides)
    return CrewdayContext(**base)


def test_emit_jq_filters_json_payload(capsys: pytest.CaptureFixture[str]) -> None:
    _emit.emit({"id": "task-9", "state": "done"}, ctx=_ctx(jq=".state"))

    out = capsys.readouterr().out
    assert json.loads(out) == "done"


def test_emit_without_jq_never_invokes_the_engine(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Opt-in only: no subprocess unless --jq is passed."""

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("jq must not run when --jq is absent")

    monkeypatch.setattr("crewday._emit.subprocess.run", explode)

    _emit.emit({"id": "task-9"}, ctx=_ctx())

    assert json.loads(capsys.readouterr().out) == {"id": "task-9"}


def test_emit_jq_invalid_expression_raises_clear_client_error() -> None:
    with pytest.raises(CrewdayError) as excinfo:
        _emit.emit({"id": "task-9"}, ctx=_ctx(jq="{"))

    err = excinfo.value
    assert isinstance(err, CrewdayError)
    assert not isinstance(err, ConfigError)
    assert err.exit_code == ExitCode.CLIENT_ERROR
    assert "invalid --jq expression" in err.message


def test_emit_jq_engine_unavailable_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing ``jq`` binary is the spec's 'engine unavailable' case."""
    monkeypatch.setattr("crewday._emit.shutil.which", lambda _name: None)

    with pytest.raises(ConfigError) as excinfo:
        _emit.emit({"id": "task-9"}, ctx=_ctx(jq=".id"))

    err = excinfo.value
    assert err.exit_code == ExitCode.CONFIG_ERROR
    assert "engine unavailable" in err.message


# ---------------------------------------------------------------------------
# Generated command: both flags end-to-end
# ---------------------------------------------------------------------------


def _no_sleep(_seconds: float) -> None:
    return None


def _factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[CrewdayContext], CrewdayClient]:
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


def _narrow(output: str) -> OutputMode:
    match output:
        case "json" | "yaml" | "table" | "ndjson":
            return output
        case _:  # pragma: no cover - guarded by click.Choice
            raise click.BadParameter(f"unexpected --output: {output!r}")


def _flag_root(
    handler: Callable[[httpx.Request], httpx.Response],
    tmp_path: pathlib.Path,
) -> click.Group:
    """A root that threads --jq / --no-color, plus one generated GET verb."""
    workspace_path = tmp_path / "_surface.json"
    admin_path = tmp_path / "_surface_admin.json"
    entry = {
        "name": "list",
        "group": "widgets",
        "operation_id": "widgets.list",
        "summary": "list widgets",
        "http": {"method": "GET", "path": "/w/{slug}/api/v1/widgets"},
        "idempotent": False,
        "path_params": [],
        "query_params": [],
        "body_schema_ref": None,
        "response_schema_ref": None,
        "x_cli": None,
        "x_agent_confirm": None,
        "x_agent_links": None,
        "agent_link_routes": {},
    }
    workspace_path.write_text(json.dumps([entry]), encoding="utf-8")
    admin_path.write_text(json.dumps([]), encoding="utf-8")
    load_surface.cache_clear()

    @click.group(name="test-crewday")
    @click.option("--workspace", default="smoke")
    @click.option("--output", type=click.Choice(OUTPUT_CHOICES), default="json")
    @click.option("--jq", "jq_filter", default=None)
    @click.option("--no-color", is_flag=True, default=False)
    @click.pass_context
    def test_root(
        ctx: click.Context,
        workspace: str,
        output: str,
        jq_filter: str | None,
        no_color: bool,
    ) -> None:
        ctx.obj = CrewdayContext(
            profile=None,
            workspace=workspace,
            output=_narrow(output),
            jq=jq_filter,
            no_color=no_color,
        )

    register_generated_commands(
        test_root,
        client_factory=_factory(handler),
        workspace_path=workspace_path,
        admin_path=admin_path,
    )
    return test_root


def _widgets_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json=[{"id": "widget-1", "state": "open"}, {"id": "widget-2", "state": "shut"}],
    )


def test_generated_command_jq_filters_output(tmp_path: pathlib.Path) -> None:
    root = _flag_root(_widgets_handler, tmp_path)

    result = CliRunner().invoke(root, ["--jq", ".[].id", "widgets", "list"])

    assert result.exit_code == 0, result.output
    assert result.output.split() == ['"widget-1"', '"widget-2"']


def test_generated_command_no_color_suppresses_table_ansi(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the colour heuristic on so the only thing turning colour off
    # is --no-color itself.
    monkeypatch.setattr(_output, "_use_color", lambda: True)
    root = _flag_root(_widgets_handler, tmp_path)
    runner = CliRunner()

    # ``color=True`` keeps click from stripping ANSI on the non-TTY test
    # sink, so what remains is exactly what --no-color controls upstream.
    coloured = runner.invoke(root, ["--output", "table", "widgets", "list"], color=True)
    plain = runner.invoke(
        root, ["--no-color", "--output", "table", "widgets", "list"], color=True
    )

    assert coloured.exit_code == 0, coloured.output
    assert plain.exit_code == 0, plain.output
    assert "\x1b[" in coloured.output
    assert "\x1b[" not in plain.output


# ---------------------------------------------------------------------------
# Override: --jq flows through the same shared path
# ---------------------------------------------------------------------------


def test_override_tasks_complete_jq_filters_output(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tasks complete`` (an override) now emits through the shared path."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": "task-1", "state": "pending"})
        return httpx.Response(200, json={"id": "task-1", "state": "done"})

    def factory(_ctx: CrewdayContext) -> CrewdayClient:
        return CrewdayClient(
            base_url="https://api.test.local",
            token="test-token",
            workspace="smoke",
            transport=httpx.MockTransport(handler),
            rng=random.Random(0),
            sleep=_no_sleep,
        )

    monkeypatch.setattr(tasks_override, "_client_factory_for", factory)

    result = CliRunner().invoke(
        tasks_override.complete,
        ["task-1"],
        obj=_ctx(jq=".state"),
    )

    assert result.exit_code == 0, result.output
    # The human summary line still prints; the JSON body is jq-filtered.
    assert "Task task-1" in result.output
    assert '"done"' in result.output
    # The raw unfiltered object must not appear.
    assert '"id": "task-1"' not in result.output
