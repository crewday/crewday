"""CI gate for keeping the generated CLI surface in sync with OpenAPI."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import click
from crewday import _codegen
from crewday._client import CrewdayClient
from crewday._globals import CrewdayContext
from crewday._overrides import register_overrides
from crewday._runtime import (
    DEFAULT_SURFACE_ADMIN_PATH,
    DEFAULT_SURFACE_PATH,
    ClientFactory,
    SurfaceEntry,
    _build_command,
    load_surface,
    register_generated_commands,
)

_CLI_METHODS: Final[frozenset[str]] = frozenset(
    {"get", "post", "put", "patch", "delete", "head"}
)
_OPERATION_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class CommandSignature:
    """Stable projection of one Click command's public CLI shape."""

    path: tuple[str, str]
    params: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpenApiOperation:
    """Operation coordinates needed by the parity report."""

    operation_id: str
    path: str
    method: str


@dataclass(frozen=True, slots=True)
class ParityReport:
    """Result of the checks that do not mutate the worktree."""

    help_tree_missing: tuple[str, ...]
    help_tree_extra: tuple[str, ...]
    help_tree_changed: tuple[str, ...]
    missing_from_cli: tuple[str, ...]
    removed_from_openapi: tuple[str, ...]
    invalid_operation_ids: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (
            self.help_tree_missing
            or self.help_tree_extra
            or self.help_tree_changed
            or self.missing_from_cli
            or self.removed_from_openapi
            or self.invalid_operation_ids
        )


def _param_signature(param: click.Parameter) -> str:
    required = "required" if param.required else "optional"
    multiple = "multiple" if getattr(param, "multiple", False) else "single"
    opts = "/".join(param.opts + param.secondary_opts)
    return f"{param.param_type_name}:{param.name}:{opts}:{required}:{multiple}"


def _command_signature(
    path: tuple[str, str],
    command: click.Command,
) -> CommandSignature:
    return CommandSignature(
        path=path,
        params=tuple(_param_signature(param) for param in command.params),
    )


def _command_tree(root: click.Group) -> dict[tuple[str, str], CommandSignature]:
    tree: dict[tuple[str, str], CommandSignature] = {}

    def walk(group: click.Group, group_path: tuple[str, ...]) -> None:
        for name, command in sorted(group.commands.items()):
            if isinstance(command, click.Group):
                walk(command, (*group_path, name))
                continue
            path = (" ".join(group_path), name)
            tree[path] = _command_signature(path, command)

    for group_name, group in sorted(root.commands.items()):
        if not isinstance(group, click.Group):
            continue
        walk(group, (group_name,))
    return tree


def _override_metadata(root: click.Group) -> tuple[set[tuple[str, str]], set[str]]:
    keys: set[tuple[str, str]] = set()
    covered: set[str] = set()

    def walk(group: click.Group) -> None:
        for command in group.commands.values():
            if isinstance(command, click.Group):
                walk(command)
                continue
            raw = getattr(command, "_cli_override", None)
            if raw is None:
                continue
            group_name, verb, covers = raw
            if isinstance(group_name, str) and isinstance(verb, str):
                keys.add((group_name, verb))
            if isinstance(covers, tuple):
                covered.update(item for item in covers if isinstance(item, str))

    walk(root)
    return keys, covered


def _noop_client_factory(ctx: CrewdayContext) -> CrewdayClient:
    raise RuntimeError("cli parity check only inspects command shape")


def _surface_command_tree(
    entries: Sequence[SurfaceEntry],
) -> dict[tuple[str, str], CommandSignature]:
    factory: ClientFactory = _noop_client_factory
    tree: dict[tuple[str, str], CommandSignature] = {}
    for entry in entries:
        command = _build_command(entry, client_factory=factory)
        path = (entry.cli_group, entry.cli_verb)
        tree[path] = _command_signature(path, command)
    return tree


def _resolved_command_tree(
    *,
    surface_path: Path,
    surface_admin_path: Path,
) -> tuple[dict[tuple[str, str], CommandSignature], set[tuple[str, str]], set[str]]:
    root = click.Group(name="crewday")
    register_generated_commands(
        root,
        client_factory=_noop_client_factory,
        workspace_path=surface_path,
        admin_path=surface_admin_path,
    )
    register_overrides(root)
    override_keys, override_covered = _override_metadata(root)
    return _command_tree(root), override_keys, override_covered


def _format_command_key(path: tuple[str, str]) -> str:
    return " ".join(path)


def _diff_help_tree(
    *,
    surface_tree: Mapping[tuple[str, str], CommandSignature],
    resolved_tree: Mapping[tuple[str, str], CommandSignature],
    override_keys: set[tuple[str, str]],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    expected_keys = set(surface_tree)
    actual_keys = set(resolved_tree)
    missing = tuple(
        _format_command_key(path)
        for path in sorted(expected_keys - actual_keys - override_keys)
    )
    extra = tuple(
        _format_command_key(path)
        for path in sorted(actual_keys - expected_keys - override_keys)
    )
    changed = tuple(
        _format_command_key(path)
        for path in sorted((expected_keys & actual_keys) - override_keys)
        if surface_tree[path] != resolved_tree[path]
    )
    return missing, extra, changed


def _load_schema(path: Path | None) -> Mapping[str, Any]:
    if path is None:
        loaded = _codegen.load_live_schema()
        if not isinstance(loaded, Mapping):
            raise TypeError("live OpenAPI schema must be a mapping")
        return loaded
    with path.open("rb") as fh:
        loaded = json.load(fh)
    if not isinstance(loaded, Mapping):
        raise TypeError(f"{path} must contain a JSON object")
    return loaded


def _iter_openapi_operations(schema: Mapping[str, Any]) -> Iterable[OpenApiOperation]:
    paths = schema.get("paths")
    if not isinstance(paths, Mapping):
        return
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, Mapping):
            continue
        for method, operation in path_item.items():
            if (
                not isinstance(method, str)
                or method.lower() not in _CLI_METHODS
                or not isinstance(operation, Mapping)
            ):
                continue
            x_cli = operation.get("x-cli")
            if isinstance(x_cli, Mapping) and x_cli.get("hidden") is True:
                continue
            operation_id = operation.get("operationId")
            if isinstance(operation_id, str) and operation_id:
                yield OpenApiOperation(
                    operation_id=operation_id,
                    path=path,
                    method=method.upper(),
                )


def _excluded_operation_ids(
    operations: Sequence[OpenApiOperation],
    exclusions: Sequence[_codegen.Exclusion],
) -> set[str]:
    excluded: set[str] = set()
    for operation in operations:
        if any(
            exclusion.matches(
                operation_id=operation.operation_id,
                path=operation.path,
            )
            for exclusion in exclusions
        ):
            excluded.add(operation.operation_id)
    return excluded


def _operation_ids_from_surface(entries: Sequence[SurfaceEntry]) -> set[str]:
    return {entry.operation_id for entry in entries if entry.operation_id is not None}


def _invalid_operation_ids(operations: Sequence[OpenApiOperation]) -> tuple[str, ...]:
    return tuple(
        sorted(
            operation.operation_id
            for operation in operations
            if _OPERATION_ID_RE.fullmatch(operation.operation_id) is None
        )
    )


def build_report(
    *,
    surface_path: Path = DEFAULT_SURFACE_PATH,
    surface_admin_path: Path = DEFAULT_SURFACE_ADMIN_PATH,
    exclusions_path: Path = _codegen.DEFAULT_EXCLUSIONS_PATH,
    schema_path: Path | None = None,
) -> ParityReport:
    load_surface.cache_clear()
    entries = load_surface(workspace_path=surface_path, admin_path=surface_admin_path)
    surface_tree = _surface_command_tree(entries)
    resolved_tree, override_keys, override_covered = _resolved_command_tree(
        surface_path=surface_path,
        surface_admin_path=surface_admin_path,
    )
    missing, extra, changed = _diff_help_tree(
        surface_tree=surface_tree,
        resolved_tree=resolved_tree,
        override_keys=override_keys,
    )

    schema = _load_schema(schema_path)
    operations = tuple(_iter_openapi_operations(schema))
    exclusions = _codegen.load_exclusions(exclusions_path)
    excluded = _excluded_operation_ids(operations, exclusions)
    openapi_ids = {operation.operation_id for operation in operations}
    cli_ids = _operation_ids_from_surface(entries)
    covered_ids = cli_ids | override_covered | excluded

    return ParityReport(
        help_tree_missing=missing,
        help_tree_extra=extra,
        help_tree_changed=changed,
        missing_from_cli=tuple(sorted(openapi_ids - covered_ids)),
        removed_from_openapi=tuple(sorted(cli_ids - openapi_ids)),
        invalid_operation_ids=_invalid_operation_ids(operations),
    )


def _print_block(title: str, rows: Sequence[str]) -> None:
    if not rows:
        return
    sys.stderr.write(f"\n{title}\n")
    for row in rows:
        sys.stderr.write(f"  - {row}\n")


def print_report(report: ParityReport) -> None:
    if report.ok:
        sys.stdout.write("crewday cli parity: ok\n")
        return
    sys.stderr.write("crewday cli parity: drift detected\n")
    _print_block(
        "Missing Click commands from crewday --help:",
        report.help_tree_missing,
    )
    _print_block(
        "Extra Click commands not backed by the surface:",
        report.help_tree_extra,
    )
    _print_block("Changed Click command signatures:", report.help_tree_changed)
    _print_block(
        "OpenAPI operations missing from CLI surface:",
        report.missing_from_cli,
    )
    _print_block(
        "CLI surface operations removed from OpenAPI:",
        report.removed_from_openapi,
    )
    _print_block("Invalid operationId values:", report.invalid_operation_ids)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", type=Path, default=DEFAULT_SURFACE_PATH)
    parser.add_argument(
        "--surface-admin",
        type=Path,
        default=DEFAULT_SURFACE_ADMIN_PATH,
    )
    parser.add_argument(
        "--exclusions",
        type=Path,
        default=_codegen.DEFAULT_EXCLUSIONS_PATH,
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=None,
        help="Committed OpenAPI JSON to compare; defaults to the live app schema.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    codegen_status = _codegen.main(
        [
            "--check",
            "--surface",
            str(args.surface),
            "--surface-admin",
            str(args.surface_admin),
            "--exclusions",
            str(args.exclusions),
        ]
    )
    if codegen_status != 0:
        return codegen_status

    report = build_report(
        surface_path=args.surface,
        surface_admin_path=args.surface_admin,
        exclusions_path=args.exclusions,
        schema_path=args.schema,
    )
    print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
