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
_MUTATING_METHODS: Final[frozenset[str]] = frozenset({"post", "put", "patch", "delete"})
_ADMIN_API_PREFIX: Final[str] = "/admin/api/v1"
_WORKSPACE_API_PREFIX: Final[str] = "/w/{slug}/api/v1"
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
    operation: Mapping[str, Any]

    @property
    def is_admin(self) -> bool:
        return self.path == _ADMIN_API_PREFIX or self.path.startswith(
            f"{_ADMIN_API_PREFIX}/"
        )

    @property
    def is_workspace(self) -> bool:
        return self.path == _WORKSPACE_API_PREFIX or self.path.startswith(
            f"{_WORKSPACE_API_PREFIX}/"
        )


def _has_valid_x_cli_metadata(operation: Mapping[str, Any]) -> bool:
    x_cli = operation.get("x-cli")
    if not isinstance(x_cli, Mapping):
        return False
    if x_cli.get("hidden") is True:
        return False
    group = x_cli.get("group")
    verb = x_cli.get("verb")
    return (
        isinstance(group, str) and bool(group) and isinstance(verb, str) and bool(verb)
    )


@dataclass(frozen=True, slots=True)
class ParityReport:
    """Result of the checks that do not mutate the worktree."""

    help_tree_missing: tuple[str, ...]
    help_tree_extra: tuple[str, ...]
    help_tree_changed: tuple[str, ...]
    missing_from_cli: tuple[str, ...]
    workspace_missing_x_cli: tuple[str, ...]
    workspace_hidden_without_reviewed_exclusion: tuple[str, ...]
    workspace_mutation_classification_invalid: tuple[str, ...]
    admin_missing_x_cli: tuple[str, ...]
    admin_missing_from_surface: tuple[str, ...]
    admin_hidden_without_reviewed_exclusion: tuple[str, ...]
    admin_unavailable_without_reviewed_exclusion: tuple[str, ...]
    admin_mutation_confirmation_missing: tuple[str, ...]
    missing_agent_links: tuple[str, ...]
    invalid_agent_links: tuple[str, ...]
    removed_from_openapi: tuple[str, ...]
    invalid_operation_ids: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (
            self.help_tree_missing
            or self.help_tree_extra
            or self.help_tree_changed
            or self.missing_from_cli
            or self.workspace_missing_x_cli
            or self.workspace_hidden_without_reviewed_exclusion
            or self.workspace_mutation_classification_invalid
            or self.admin_missing_x_cli
            or self.admin_missing_from_surface
            or self.admin_hidden_without_reviewed_exclusion
            or self.admin_unavailable_without_reviewed_exclusion
            or self.admin_mutation_confirmation_missing
            or self.missing_agent_links
            or self.invalid_agent_links
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
        loaded = _codegen.load_committed_schema()
        if not isinstance(loaded, Mapping):
            raise TypeError("committed OpenAPI schema must be a mapping")
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
            operation_id = operation.get("operationId")
            if isinstance(operation_id, str) and operation_id:
                yield OpenApiOperation(
                    operation_id=operation_id,
                    path=path,
                    method=method.upper(),
                    operation=operation,
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


def _is_hidden(operation: OpenApiOperation) -> bool:
    x_cli = operation.operation.get("x-cli")
    return isinstance(x_cli, Mapping) and x_cli.get("hidden") is True


def _is_cli_candidate(operation: OpenApiOperation) -> bool:
    return not _is_hidden(operation) and _has_valid_x_cli_metadata(operation.operation)


def _admin_eligible_operations(
    operations: Sequence[OpenApiOperation],
    excluded: set[str],
) -> tuple[OpenApiOperation, ...]:
    return tuple(
        operation
        for operation in operations
        if operation.is_admin
        and operation.operation_id not in excluded
        and not _is_hidden(operation)
    )


def _workspace_unexcluded_operations(
    operations: Sequence[OpenApiOperation],
    excluded: set[str],
) -> tuple[OpenApiOperation, ...]:
    return tuple(
        operation
        for operation in operations
        if operation.is_workspace and operation.operation_id not in excluded
    )


def _operation_mutates(operation: OpenApiOperation) -> bool:
    x_cli = operation.operation.get("x-cli")
    if isinstance(x_cli, Mapping) and x_cli.get("mutates") is True:
        return True
    return operation.method.lower() in _MUTATING_METHODS


def _admin_default_confirmation_policy_applies(operation: OpenApiOperation) -> bool:
    return operation.is_admin


def _admin_missing_x_cli(operations: Sequence[OpenApiOperation]) -> tuple[str, ...]:
    return tuple(
        sorted(
            operation.operation_id
            for operation in operations
            if not _is_cli_candidate(operation)
        )
    )


def _workspace_missing_x_cli(
    operations: Sequence[OpenApiOperation],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            operation.operation_id
            for operation in operations
            if not _is_hidden(operation)
            and not _has_valid_x_cli_metadata(operation.operation)
        )
    )


def _workspace_hidden_without_reviewed_exclusion(
    operations: Sequence[OpenApiOperation],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            operation.operation_id for operation in operations if _is_hidden(operation)
        )
    )


def _has_agent_confirmation(operation: OpenApiOperation) -> bool:
    raw = operation.operation.get("x-agent-confirm")
    return raw is True or isinstance(raw, Mapping)


def _agent_classification_count(operation: OpenApiOperation) -> int:
    return sum(
        (
            _has_agent_confirmation(operation),
            operation.operation.get("x-agent-forbidden") is True,
            operation.operation.get("x-interactive-only") is True,
        )
    )


def _workspace_mutation_classification_invalid(
    operations: Sequence[OpenApiOperation],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            operation.operation_id
            for operation in operations
            if _operation_mutates(operation)
            and _agent_classification_count(operation) != 1
        )
    )


def _admin_hidden_without_reviewed_exclusion(
    operations: Sequence[OpenApiOperation],
    excluded: set[str],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            operation.operation_id
            for operation in operations
            if operation.is_admin
            and _is_hidden(operation)
            and operation.operation_id not in excluded
        )
    )


def _admin_unavailable_without_reviewed_exclusion(
    operations: Sequence[OpenApiOperation],
    excluded: set[str],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            operation.operation_id
            for operation in operations
            if operation.is_admin
            and not _is_hidden(operation)
            and operation.operation_id not in excluded
            and (
                operation.operation.get("x-agent-forbidden") is True
                or operation.operation.get("x-interactive-only") is True
            )
        )
    )


def _admin_mutation_confirmation_missing(
    operations: Sequence[OpenApiOperation],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            operation.operation_id
            for operation in operations
            if _operation_mutates(operation)
            and operation.operation.get("x-agent-confirm") is None
            and not _admin_default_confirmation_policy_applies(operation)
        )
    )


def _agent_link_policy_problem(
    operation: Mapping[str, Any],
    route_manifest: Mapping[str, Mapping[str, Any]],
) -> str | None:
    raw = operation.get("x-agent-links")
    if not isinstance(raw, Mapping):
        return "missing x-agent-links"
    policy = raw.get("policy")
    if policy == "none":
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return "policy none requires a non-empty reason"
        if "links" in raw:
            return "policy none must not include links"
        return None
    if policy != "links":
        return "policy must be 'links' or 'none'"
    if "reason" in raw:
        return "policy links must not include reason"
    links = raw.get("links")
    if not isinstance(links, list) or not links:
        return "policy links requires a non-empty links list"
    for index, item in enumerate(links):
        problem = _agent_link_entry_problem(item, route_manifest=route_manifest)
        if problem is not None:
            return f"links[{index}]: {problem}"
    return None


def _agent_link_entry_problem(
    item: object,
    *,
    route_manifest: Mapping[str, Mapping[str, Any]],
) -> str | None:
    if not isinstance(item, Mapping):
        return "entry must be an object"
    expected = {"rel", "label", "route", "params", "query"}
    keys = {str(key) for key in item}
    if keys != expected:
        return f"entry keys must be {sorted(expected)}"
    for key in ("rel", "label", "route"):
        value = item.get(key)
        if not isinstance(value, str) or not value:
            return f"{key} must be a non-empty string"
    params = item.get("params")
    query = item.get("query")
    if not isinstance(params, Mapping):
        return "params must be an object"
    if not isinstance(query, Mapping):
        return "query must be an object"
    route_name = item["route"]
    route = route_manifest.get(route_name)
    if route is None:
        return f"route {route_name!r} is not in the agent-linkable manifest"
    required_params = _route_field_names(route.get("params"))
    if set(params) != required_params:
        return f"params must bind route params {sorted(required_params)}"
    allowed_query = _route_field_names(route.get("query"))
    extra_query = set(query) - allowed_query
    if extra_query:
        return f"query keys are not allowed by route manifest: {sorted(extra_query)}"
    return None


def _route_field_names(raw: object) -> set[str]:
    if not isinstance(raw, list):
        return set()
    names: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _agent_link_policy_violations(
    operations: Sequence[OpenApiOperation],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    route_manifest = _codegen._load_route_manifest()
    missing: list[str] = []
    invalid: list[str] = []
    for operation in operations:
        problem = _agent_link_policy_problem(
            operation.operation,
            route_manifest=route_manifest,
        )
        if problem is None:
            continue
        if problem == "missing x-agent-links":
            missing.append(operation.operation_id)
        else:
            invalid.append(f"{operation.operation_id}: {problem}")
    return tuple(sorted(missing)), tuple(sorted(invalid))


def _operation_ids_from_surface(entries: Sequence[SurfaceEntry]) -> set[str]:
    return {
        entry.operation_id
        for entry in entries
        if entry.operation_id is not None
        and _has_valid_x_cli_metadata({"x-cli": entry.x_cli})
    }


def _operation_ids_from_surface_file(path: Path) -> set[str]:
    with path.open("rb") as fh:
        raw = json.load(fh)
    if not isinstance(raw, list):
        return set()
    return {
        op_id
        for item in raw
        if isinstance(item, Mapping)
        and isinstance((op_id := item.get("operation_id")), str)
        and op_id
    }


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
    cli_operations = tuple(
        operation
        for operation in operations
        if _is_cli_candidate(operation) and operation.operation_id not in excluded
    )
    openapi_ids = {operation.operation_id for operation in cli_operations}
    cli_ids = _operation_ids_from_surface(entries)
    covered_ids = cli_ids | override_covered | excluded
    workspace_unexcluded = _workspace_unexcluded_operations(operations, excluded)
    admin_eligible = _admin_eligible_operations(operations, excluded)
    admin_surface_ids = _operation_ids_from_surface_file(surface_admin_path)
    missing_agent_links, invalid_agent_links = _agent_link_policy_violations(
        cli_operations
    )

    return ParityReport(
        help_tree_missing=missing,
        help_tree_extra=extra,
        help_tree_changed=changed,
        missing_from_cli=tuple(sorted(openapi_ids - covered_ids)),
        workspace_missing_x_cli=_workspace_missing_x_cli(workspace_unexcluded),
        workspace_hidden_without_reviewed_exclusion=(
            _workspace_hidden_without_reviewed_exclusion(workspace_unexcluded)
        ),
        workspace_mutation_classification_invalid=(
            _workspace_mutation_classification_invalid(workspace_unexcluded)
        ),
        admin_missing_x_cli=_admin_missing_x_cli(admin_eligible),
        admin_missing_from_surface=tuple(
            sorted(
                operation.operation_id
                for operation in admin_eligible
                if _is_cli_candidate(operation)
                and operation.operation_id not in admin_surface_ids
            )
        ),
        admin_hidden_without_reviewed_exclusion=(
            _admin_hidden_without_reviewed_exclusion(operations, excluded)
        ),
        admin_unavailable_without_reviewed_exclusion=(
            _admin_unavailable_without_reviewed_exclusion(operations, excluded)
        ),
        admin_mutation_confirmation_missing=_admin_mutation_confirmation_missing(
            admin_eligible
        ),
        missing_agent_links=missing_agent_links,
        invalid_agent_links=invalid_agent_links,
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
        "Workspace operations missing valid x-cli metadata or reviewed exclusion:",
        report.workspace_missing_x_cli,
    )
    _print_block(
        "Hidden workspace operations missing a reviewed exclusion:",
        report.workspace_hidden_without_reviewed_exclusion,
    )
    _print_block(
        "Mutating workspace operations missing exactly one agent classification:",
        report.workspace_mutation_classification_invalid,
    )
    _print_block(
        "Admin operations missing valid x-cli metadata or reviewed exclusion:",
        report.admin_missing_x_cli,
    )
    _print_block(
        "Admin operations missing from _surface_admin.json:",
        report.admin_missing_from_surface,
    )
    _print_block(
        "Hidden admin operations missing a reviewed exclusion:",
        report.admin_hidden_without_reviewed_exclusion,
    )
    _print_block(
        "Forbidden or interactive-only admin operations missing a reviewed exclusion:",
        report.admin_unavailable_without_reviewed_exclusion,
    )
    _print_block(
        "Admin mutating operations missing confirmation coverage:",
        report.admin_mutation_confirmation_missing,
    )
    _print_block(
        "CLI/agent operations missing x-agent-links policy:",
        report.missing_agent_links,
    )
    _print_block(
        "CLI/agent operations with invalid x-agent-links policy:",
        report.invalid_agent_links,
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
        help=(
            "Path to an OpenAPI JSON file to compare against; "
            "defaults to docs/api/openapi.json (regenerate via 'make openapi')."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    codegen_argv = [
        "--check",
        "--surface",
        str(args.surface),
        "--surface-admin",
        str(args.surface_admin),
        "--exclusions",
        str(args.exclusions),
    ]
    if args.schema is not None:
        # Keep the parity check's view of the schema consistent across
        # the codegen drift check and the report build — without this
        # the codegen would silently fall back to ``docs/api/openapi.json``
        # while ``build_report`` reads the user-supplied file.
        codegen_argv.extend(["--openapi", str(args.schema)])
    codegen_status = _codegen.main(codegen_argv)

    report = build_report(
        surface_path=args.surface,
        surface_admin_path=args.surface_admin,
        exclusions_path=args.exclusions,
        schema_path=args.schema,
    )
    print_report(report)
    if codegen_status != 0:
        return codegen_status
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
