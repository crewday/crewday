"""The single CLI stdout emit path: ``--jq`` filtering + ``--no-color``.

Every command that prints a decoded payload — the codegen-driven
commands in :mod:`crewday._runtime` and the hand-written composites in
:mod:`crewday._overrides` — funnels through :func:`emit` (or
:func:`emit_ndjson` for streamed pagination). Centralising the write
site is what lets the two §13 "Global flags" ``--jq`` and ``--no-color``
apply *once*, uniformly, instead of being re-wired per command. Before
this module the overrides hand-rolled ``click.echo(json.dumps(...))``,
which no global flag could reach.

jq engine decision
------------------
``--jq`` shells out to the ``jq`` **binary** via :mod:`subprocess`
rather than adding a Python jq binding (``pyjq`` / ``jq``). Rationale:

* No jq library is a CLI dependency today, so the binary path adds
  **zero new dependencies**.
* A missing binary is exactly the spec's "engine unavailable" case; we
  surface it as a :class:`~crewday._main.ConfigError` (exit 5), the same
  slot used for other environment/config failures.
* An invalid jq expression is user input, surfaced as a
  :class:`~crewday._main.CrewdayError` (exit 1, client error). The two
  error paths are deliberately given **distinct exit codes** so an agent
  can tell "install jq" apart from "fix your filter".

jq is opt-in only: no subprocess is spawned unless ``ctx.jq`` is set.
jq consumes and produces JSON, so ``--jq`` filters the JSON rendering of
the payload regardless of ``-o`` (table/yaml formatting is bypassed when
a filter is present, per §13 "``--jq`` jq-filter the JSON output").
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from typing import Any, Final

import click

from crewday._client import ApiError
from crewday._globals import CrewdayContext
from crewday._main import ConfigError, CrewdayError
from crewday._output import format_api_error, format_response

__all__ = [
    "emit",
    "emit_api_error",
    "emit_ndjson",
]


_JQ_BINARY: Final[str] = "jq"


def emit(
    payload: object,
    *,
    ctx: CrewdayContext,
    schema_hint: Mapping[str, Any] | None = None,
) -> None:
    """Write ``payload`` to stdout honouring ``--jq`` and ``--no-color``."""
    if ctx.jq is not None:
        rendered = _run_jq(ctx.jq, format_response(payload, "json"))
        if rendered:
            click.echo(rendered)
        return
    rendered = format_response(
        payload,
        ctx.output,
        schema_hint=schema_hint,
        no_color=ctx.no_color,
    )
    if rendered:
        click.echo(rendered)


def emit_ndjson(rows: Iterator[Mapping[str, Any]], *, ctx: CrewdayContext) -> None:
    """Stream ``rows`` to stdout as NDJSON, one JSON object per line.

    Under ``--jq`` each row is filtered through the expression before it
    is written so a paginated stream stays a stream (no buffering the
    whole result set to hand jq one document).
    """
    for row in rows:
        line = format_response(row, "ndjson")
        if ctx.jq is not None:
            filtered = _run_jq(ctx.jq, line)
            if not filtered:
                continue
            line = filtered
        sys.stdout.write(line)
        sys.stdout.write("\n")
        sys.stdout.flush()


def emit_api_error(error: ApiError, *, ctx: CrewdayContext) -> None:
    """Write a structured API error to stderr and exit with its code.

    Errors are not run through ``--jq``: the filter targets the JSON
    *data* on stdout, while RFC 7807 errors go to stderr (§13 "Output").
    ``--no-color`` still applies to the human-readable table form.
    """
    click.echo(format_api_error(error, ctx.output, no_color=ctx.no_color), err=True)
    raise click.exceptions.Exit(error.exit_code)


def _run_jq(expr: str, json_text: str) -> str:
    """Filter ``json_text`` through ``jq expr`` and return jq's stdout.

    Raises :class:`ConfigError` (exit 5) when the ``jq`` binary is not on
    ``PATH`` — the "engine unavailable" case — and :class:`CrewdayError`
    (exit 1) when jq rejects the expression or fails at runtime.
    """
    executable = shutil.which(_JQ_BINARY)
    if executable is None:
        raise ConfigError(
            "--jq needs the 'jq' binary on PATH, but it was not found "
            "(jq engine unavailable). Install jq or drop --jq."
        )
    try:
        completed = subprocess.run(
            [executable, expr],
            input=json_text,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        # The binary resolved but could not be executed (permissions,
        # exec format). Treat as engine-unavailable, not a bad filter.
        raise ConfigError(f"--jq could not run the 'jq' binary: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"jq exited {completed.returncode}"
        raise CrewdayError(f"invalid --jq expression {expr!r}: {detail}")
    return completed.stdout.rstrip("\n")
