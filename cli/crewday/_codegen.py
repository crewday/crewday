"""CLI surface codegen — produce ``_surface.json`` / ``_surface_admin.json``.

This module is the build-time bridge between the live FastAPI schema
and the runtime CLI dispatcher. Its job is to serialise the subset of
the OpenAPI document the CLI cares about into two small, committed
JSON files that :mod:`crewday._runtime` (cd-lato) will load at import
time to register Click groups and commands.

**Two surfaces, one generator.**  Every operation is partitioned into
one of:

* ``cli/crewday/_surface.json`` — workspace-scoped + bare-host auth
  verbs (everything under ``/api/v1/...`` or ``/w/<slug>/api/v1/...``
  that is NOT under ``/admin/api/v1/``).
* ``cli/crewday/_surface_admin.json`` — deployment-admin verbs under
  ``/admin/api/v1/...``. Today the route tree is empty, so the file
  is ``[]``; keeping the seam wired lets the admin embedded agent
  (§11) load a distinct tool catalog without auth'ing against the
  tenant API just to discover its verbs.

**Pipeline** (documented here; each step is a helper below):

1. Set ``CREWDAY_ROOT_KEY`` to a zeroed dummy if unset — the app
   factory refuses to boot without one, but codegen does not need a
   real secret (we never actually decrypt anything).
2. Import :func:`app.api.factory.create_app` and call ``app.openapi()``.
3. Load :file:`cli/crewday/_exclusions.yaml` (operation-id or path-glob
   based, each entry carries a mandatory ``reason``).
4. Walk ``schema["paths"]``; for each ``(path, method, operation)``
   skip if excluded, otherwise classify into the workspace / admin
   surface by path prefix and derive ``(group, name)`` via the
   heuristic described in ``docs/specs/13-cli.md`` §"CLI generation
   from OpenAPI".
5. Build a compact per-entry dict (see :func:`_build_entry`) preserving
   path / query params, request and response schema refs, idempotency,
   ``x-cli`` and ``x-agent-confirm`` extensions.
6. Sort entries deterministically by ``(group, name, method, path)``.
7. Write both surface files as ``json.dumps(..., indent=2,
   sort_keys=True) + "\\n"`` so the diff stays line-oriented and
   agnostic to Python's ``dict`` insertion order.

**Modes.**  ``python -m crewday._codegen`` writes both files;
``--check`` diffs the in-memory output against the committed copy
(exit 1 when they differ); ``--dry-run`` prints the would-be files to
stdout without touching disk.

See ``docs/specs/13-cli.md`` §"Surface descriptor
(``_surface.json`` + ``_surface_admin.json``)", §"Runtime command
construction", §"Exclusions"; ``docs/specs/12-rest-api.md``
§"CLI surface extensions (``x-cli``)" and §"Agent confirmation
extension (``x-agent-confirm``)".
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import yaml

__all__ = [
    "DEFAULT_EXCLUSIONS_PATH",
    "DEFAULT_SURFACE_ADMIN_PATH",
    "DEFAULT_SURFACE_PATH",
    "CollisionError",
    "DummyRootKey",
    "Exclusion",
    "ExclusionError",
    "classify_surface",
    "derive_group_name",
    "generate_surfaces",
    "load_exclusions",
    "main",
]

_log = logging.getLogger(__name__)

# Canonical on-disk paths for the three committed artefacts.
_PACKAGE_DIR: Final[Path] = Path(__file__).resolve().parent
DEFAULT_SURFACE_PATH: Final[Path] = _PACKAGE_DIR / "_surface.json"
DEFAULT_SURFACE_ADMIN_PATH: Final[Path] = _PACKAGE_DIR / "_surface_admin.json"
DEFAULT_EXCLUSIONS_PATH: Final[Path] = _PACKAGE_DIR / "_exclusions.yaml"


# Idempotent HTTP verbs per RFC 9110. ``POST`` and ``PATCH`` are
# deliberately excluded — even a POST that is semantically idempotent
# at the app layer (via ``Idempotency-Key``) is not idempotent by
# protocol contract, which is what the CLI needs to know to decide
# whether a retry is safe.
_IDEMPOTENT_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "PUT", "DELETE"})

# Every HTTP method we render as a CLI command. FastAPI also emits
# ``options`` entries automatically; those are transport-level probes
# (CORS preflight) and never get a CLI verb. ``trace`` is not used.
_CLI_METHODS: Final[frozenset[str]] = frozenset(
    {"get", "post", "put", "patch", "delete", "head"}
)

# Dummy envelope-key value used when running codegen without a real
# deployment key set. ``CREWDAY_ROOT_KEY`` is validated as hex by the
# settings layer; 64 zeros is syntactically valid and never decrypts
# anything useful.
_DUMMY_ROOT_KEY: Final[str] = "00" * 32


class DummyRootKey:
    """Namespace constant so tests can reference the exact dummy value.

    Kept as a class (not a module-level ``Final``) so
    ``DummyRootKey.VALUE`` reads as a named intent rather than a magic
    literal at call sites.
    """

    VALUE: Final[str] = _DUMMY_ROOT_KEY


class ExclusionError(ValueError):
    """Raised when :file:`_exclusions.yaml` is malformed.

    The codegen fails loudly rather than silently skipping an invalid
    entry — a malformed exclusion could otherwise mask a bad operation
    from the CLI surface indefinitely. Missing ``reason:`` fields and
    entries that match neither ``operation_id`` nor ``path_pattern``
    both trip this.
    """


class CollisionError(ValueError):
    """Raised when two operations resolve to the same ``(group, name)``.

    The runtime (cd-lato) registers exactly one Click command per
    ``(group, verb)`` pair; a duplicate means one of the two commands
    would silently shadow the other. The generator refuses to emit a
    surface that contains a collision, forcing the API author to
    disambiguate via ``x-cli.group`` / ``x-cli.verb`` on the clashing
    route (spec §12 §"CLI surface extensions"). The error message
    lists every colliding pair with its operation ids so the fix is
    a mechanical edit.
    """


@dataclass(frozen=True, slots=True)
class Exclusion:
    """One entry from :file:`_exclusions.yaml`.

    Exactly one of ``operation_id`` / ``path_pattern`` is populated;
    the loader enforces the xor-ness. ``reason`` is mandatory per spec
    §13 "Exclusions" ("Adding an exclusion without a reason fails CI
    lint").
    """

    reason: str
    operation_id: str | None = None
    path_pattern: str | None = None

    def matches(self, *, operation_id: str | None, path: str) -> bool:
        """Return ``True`` if this exclusion covers the given operation.

        ``operation_id`` may legitimately be ``None`` for a FastAPI-
        auto-generated id case that is nonetheless excluded by path
        pattern — the pattern branch handles that.
        """
        if (
            self.operation_id is not None
            and operation_id is not None
            and self.operation_id == operation_id
        ):
            return True
        return bool(
            self.path_pattern is not None
            and fnmatch.fnmatchcase(path, self.path_pattern)
        )


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


def load_exclusions(path: Path = DEFAULT_EXCLUSIONS_PATH) -> list[Exclusion]:
    """Parse ``_exclusions.yaml`` into a list of :class:`Exclusion`.

    Shape (see the checked-in example):

    .. code-block:: yaml

       exclusions:
         - operation_id: "auth.passkey.login_start"
           reason: "browser-only WebAuthn ceremony"
         - path_pattern: "/w/{slug}/events"
           reason: "SSE transport"

    Every entry must carry a ``reason``. Each entry must set exactly
    one of ``operation_id`` / ``path_pattern`` — an entry with both or
    neither raises :class:`ExclusionError`. The loader tolerates an
    empty file (``exclusions: []``) and a missing file (returns an
    empty list with a debug log), so the codegen path works out of the
    box in a fresh clone.
    """
    if not path.is_file():
        _log.debug(
            "exclusions file missing; treating as empty",
            extra={"path": str(path)},
        )
        return []

    with path.open("rb") as fh:
        raw = yaml.safe_load(fh) or {}

    if not isinstance(raw, dict) or "exclusions" not in raw:
        raise ExclusionError(
            f"{path}: top-level must be a mapping with an 'exclusions' key"
        )

    entries = raw.get("exclusions") or []
    if not isinstance(entries, list):
        raise ExclusionError(
            f"{path}: 'exclusions' must be a list, got {type(entries).__name__}"
        )

    result: list[Exclusion] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ExclusionError(
                f"{path}[{idx}]: each exclusion must be a mapping, "
                f"got {type(entry).__name__}"
            )

        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ExclusionError(
                f"{path}[{idx}]: missing or empty 'reason' — "
                f"every exclusion must explain itself"
            )

        op_id = entry.get("operation_id")
        pattern = entry.get("path_pattern")

        has_op = isinstance(op_id, str) and op_id != ""
        has_pattern = isinstance(pattern, str) and pattern != ""

        if has_op == has_pattern:
            raise ExclusionError(
                f"{path}[{idx}]: must set exactly one of "
                f"'operation_id' / 'path_pattern' "
                f"(got operation_id={op_id!r}, path_pattern={pattern!r})"
            )

        result.append(
            Exclusion(
                reason=reason,
                operation_id=op_id if has_op else None,
                path_pattern=pattern if has_pattern else None,
            )
        )
    return result


def is_excluded(
    *,
    operation_id: str | None,
    path: str,
    exclusions: list[Exclusion],
) -> bool:
    """Return ``True`` when any exclusion matches."""
    return any(exc.matches(operation_id=operation_id, path=path) for exc in exclusions)


# ---------------------------------------------------------------------------
# Surface classification + group/name heuristic
# ---------------------------------------------------------------------------


SurfaceKind = Literal["workspace", "admin"]


def classify_surface(path: str) -> SurfaceKind:
    """Partition ``path`` into one of the two descriptor files.

    Spec §13 §"Surface descriptor": anything under ``/admin/api/v1/``
    is deployment-admin; everything else (bare-host ``/api/v1/*`` and
    workspace ``/w/<slug>/api/v1/*``) is workspace-scoped. The CLI
    today has no third surface.
    """
    if path.startswith("/admin/api/v1/") or path == "/admin/api/v1":
        return "admin"
    return "workspace"


def _path_segments(path: str) -> list[str]:
    """Return the non-empty, non-parameter segments of ``path``.

    Parameter segments are ``{name}`` placeholders; stripping them
    keeps the heuristic focused on the resource + action words. Leading
    ``/`` is dropped implicitly by :meth:`str.split`.
    """
    return [
        seg
        for seg in path.split("/")
        if seg and not (seg.startswith("{") and seg.endswith("}"))
    ]


def _path_ends_in_parameter(path: str) -> bool:
    """Return ``True`` if the last segment of ``path`` is ``{…}``.

    Used to distinguish item routes (``GET /tasks/{id}`` → ``show``)
    from collection routes (``GET /tasks`` → ``list``).
    """
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        return False
    last = segments[-1]
    return last.startswith("{") and last.endswith("}")


def _derive_group(path: str, operation: dict[str, Any]) -> str:
    """Return the CLI group name for this operation.

    Honours ``tags[-1]`` when the operation carries a tag list; falls
    back to the first non-parameter segment after ``/api/v1/`` (i.e.
    the resource root). This matches the §13 listings — tasks under
    ``time`` come from a handler tagged ``time``, and a tag-less
    ``/w/<slug>/api/v1/time/shifts`` would still resolve to ``time``
    from the path.
    """
    tags = operation.get("tags")
    if isinstance(tags, list) and tags:
        last_tag = tags[-1]
        if isinstance(last_tag, str) and last_tag:
            return last_tag

    segments = _path_segments(path)
    # Strip the leading workspace + API-version prefix to land on the
    # resource root. ``/w/{slug}/api/v1/time/shifts`` → ['api', 'v1',
    # 'time', 'shifts'] after parameter-stripping, since ``{slug}``
    # is gone; then the resource root is 'time'.
    for marker in ("api",):
        if marker in segments:
            idx = segments.index(marker)
            segments = segments[idx + 1 :]
            break
    # Drop the version segment ``v1`` / ``v2`` ...
    if segments and segments[0].startswith("v") and segments[0][1:].isdigit():
        segments = segments[1:]
    if segments:
        return segments[0]
    return "misc"


def _derive_name(
    *,
    method: str,
    path: str,
    group: str,
) -> str:
    """Return the CLI verb name for ``(method, path, group)``.

    The rule set mirrors the §13 listing:

    * ``POST /…/{id}/<action>``         → ``<action>`` (e.g. ``close``)
    * ``POST /…/<action>`` (collection) → ``<action>`` when the action
      word differs from the derived group name (``/shifts/open`` →
      ``open``, ``/invite/accept`` → ``accept``); otherwise
      ``create`` (``/me/tokens`` → ``create``).
    * ``POST /<resource>``              → ``create``
    * ``GET`` on a collection           → ``list``
    * ``GET`` on an item (``{param}``)  → ``show``
    * ``PATCH``                         → ``update``
    * ``PUT``                           → ``replace``
    * ``DELETE``                        → ``delete``
    * ``HEAD``                          → ``head``

    ``group`` is threaded in because the "action vs resource" split
    for ``POST`` is unambiguous only with it: ``/me/tokens`` looks
    exactly like ``/shifts/open`` until we know that ``tokens`` is
    the resource root (group) rather than an action word.
    """
    method_upper = method.upper()
    segments = [seg for seg in path.split("/") if seg]
    raw_last = segments[-1] if segments else ""
    last_is_param = raw_last.startswith("{") and raw_last.endswith("}")
    ends_param = _path_ends_in_parameter(path)

    if method_upper == "POST":
        # Path ends in a path param (``POST /users/{id}``) — rare but
        # spec-allowed; treat as ``create`` since no action word is
        # available. Handlers that want a different verb should set
        # ``x-cli.verb``.
        if last_is_param:
            return "create"

        # ``POST /<...>/{id}/<action>`` — the action word is the last
        # non-param segment, which follows a path-param segment.
        if len(segments) >= 2:
            prev = segments[-2]
            if prev.startswith("{") and prev.endswith("}"):
                return raw_last

        # ``POST /<...>/<action>`` — collection action. Distinguish
        # an action word (``open``, ``accept``) from a resource-root
        # POST (``/me/tokens`` → create) by comparing to the derived
        # group name: if the last segment matches the group it is the
        # resource noun and this is a plain collection ``create``.
        non_param_segments = _path_segments(path)
        if len(non_param_segments) >= 2 and raw_last != group:
            return raw_last
        return "create"

    if method_upper == "GET":
        return "show" if ends_param else "list"

    if method_upper == "PATCH":
        return "update"
    if method_upper == "PUT":
        return "replace"
    if method_upper == "DELETE":
        return "delete"
    if method_upper == "HEAD":
        return "head"
    # Unreachable under :data:`_CLI_METHODS`, but keep the fallback
    # explicit so a future method addition does not silently map to
    # a string literal.
    return method_upper.lower()


def derive_group_name(
    *,
    method: str,
    path: str,
    operation: dict[str, Any],
) -> tuple[str, str]:
    """Return ``(group, name)`` for an operation, honouring ``x-cli``.

    Spec §12 §"CLI surface extensions": when ``operation["x-cli"]``
    is present, its ``group`` / ``verb`` win over the heuristic. The
    override may also set ``hidden: true`` to exclude the operation
    entirely — callers check that separately via :func:`_is_hidden`.
    """
    x_cli = operation.get("x-cli")
    if isinstance(x_cli, dict):
        group = x_cli.get("group")
        verb = x_cli.get("verb")
        if isinstance(group, str) and group and isinstance(verb, str) and verb:
            return group, verb

    group = _derive_group(path, operation)
    return group, _derive_name(method=method, path=path, group=group)


def _is_hidden(operation: dict[str, Any]) -> bool:
    """Return ``True`` when ``x-cli.hidden`` is set on the operation."""
    x_cli = operation.get("x-cli")
    if isinstance(x_cli, dict):
        return bool(x_cli.get("hidden"))
    return False


# ---------------------------------------------------------------------------
# Entry assembly
# ---------------------------------------------------------------------------


def _filter_params(
    parameters: list[dict[str, Any]] | None,
    *,
    where: str,
) -> list[dict[str, Any]]:
    """Return the parameters with ``in == where``, preserving order.

    We keep only the three fields the runtime dispatcher cares about
    (``name`` / ``required`` / ``schema``) — ``description`` is helpful
    for ``--help`` but the runtime reads it lazily from the live
    OpenAPI when needed, not from the committed surface file. Keeping
    the descriptor small keeps the diff small.
    """
    if not parameters:
        return []
    filtered: list[dict[str, Any]] = []
    for param in parameters:
        if param.get("in") != where:
            continue
        entry: dict[str, Any] = {
            "name": param.get("name", ""),
            "required": bool(param.get("required", False)),
            "schema": param.get("schema", {}),
        }
        filtered.append(entry)
    return filtered


def _body_schema_ref(operation: dict[str, Any]) -> str | None:
    """Return the JSON body's ``$ref`` string, or ``None``.

    Operations whose body is an inline schema (no ``$ref``) fall
    through to ``None`` — the runtime will need to inspect the live
    OpenAPI for the inline shape. Current scaffolding uses Pydantic
    models everywhere so every body has a ref; the branch is kept
    for forward-compatibility.
    """
    body = operation.get("requestBody")
    if not isinstance(body, dict):
        return None
    content = body.get("content")
    if not isinstance(content, dict):
        return None
    json_content = content.get("application/json")
    if not isinstance(json_content, dict):
        return None
    schema = json_content.get("schema")
    if not isinstance(schema, dict):
        return None
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return ref
    return None


def _response_schema_ref(operation: dict[str, Any]) -> str | None:
    """Return the 2xx JSON response's ``$ref``, or ``None``.

    Picks the lowest-numbered 2xx status (200 > 201 > 202 …) so
    ``POST`` handlers that emit ``201`` still get their payload
    captured. ``default`` is ignored — it is almost always the error
    envelope.
    """
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None
    two_xx_codes = sorted(
        (code for code in responses if isinstance(code, str) and code.startswith("2")),
    )
    for code in two_xx_codes:
        entry = responses[code]
        if not isinstance(entry, dict):
            continue
        content = entry.get("content")
        if not isinstance(content, dict):
            continue
        json_content = content.get("application/json")
        if not isinstance(json_content, dict):
            continue
        schema = json_content.get("schema")
        if not isinstance(schema, dict):
            continue
        ref = schema.get("$ref")
        if isinstance(ref, str):
            return ref
    return None


def _build_entry(
    *,
    path: str,
    method: str,
    operation: dict[str, Any],
) -> dict[str, Any]:
    """Build the committed descriptor entry for one operation.

    Keep the shape small and explicit — the runtime (cd-lato) reads
    this dict verbatim, so every key matters and surprising extras
    would bloat the committed diff without a consumer.
    """
    method_upper = method.upper()
    group, name = derive_group_name(method=method, path=path, operation=operation)
    parameters = operation.get("parameters")
    params_list: list[dict[str, Any]] | None = None
    if isinstance(parameters, list):
        params_list = [p for p in parameters if isinstance(p, dict)]

    path_params = _filter_params(params_list, where="path")
    query_params = _filter_params(params_list, where="query")

    return {
        "group": group,
        "name": name,
        "operation_id": operation.get("operationId"),
        "summary": operation.get("summary"),
        "http": {"method": method_upper, "path": path},
        "path_params": path_params,
        "query_params": query_params,
        "body_schema_ref": _body_schema_ref(operation),
        "response_schema_ref": _response_schema_ref(operation),
        "idempotent": method_upper in _IDEMPOTENT_METHODS,
        "x_cli": operation.get("x-cli"),
        "x_agent_confirm": operation.get("x-agent-confirm"),
    }


# ---------------------------------------------------------------------------
# Surface generation
# ---------------------------------------------------------------------------


def generate_surfaces(
    *,
    schema: dict[str, Any],
    exclusions: list[Exclusion] | None = None,
) -> dict[SurfaceKind, list[dict[str, Any]]]:
    """Produce both surface lists from an OpenAPI schema.

    Pure function (no I/O): takes the schema + exclusions, returns a
    mapping from surface kind to sorted entry list. Keeping the core
    pure means the tests can exercise the full pipeline on synthetic
    schemas without booting FastAPI — much faster and no
    ``CREWDAY_ROOT_KEY`` dance.

    Sorting is deterministic on ``(group, name, method, path)`` so
    regenerating from the same schema twice yields byte-identical
    output. None-valued operation ids / summaries sort as empty
    strings to keep the tuple totally ordered.
    """
    exclusions = exclusions if exclusions is not None else []
    surfaces: dict[SurfaceKind, list[dict[str, Any]]] = {
        "workspace": [],
        "admin": [],
    }

    paths = schema.get("paths") or {}
    if not isinstance(paths, dict):
        return surfaces

    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            if not isinstance(method, str) or method.lower() not in _CLI_METHODS:
                continue
            if not isinstance(operation, dict):
                continue

            op_id = operation.get("operationId")
            if not isinstance(op_id, str):
                op_id = None

            if is_excluded(operation_id=op_id, path=path, exclusions=exclusions):
                continue
            if _is_hidden(operation):
                continue

            entry = _build_entry(path=path, method=method, operation=operation)
            surface = classify_surface(path)
            surfaces[surface].append(entry)

    for surface_entries in surfaces.values():
        surface_entries.sort(
            key=lambda e: (
                str(e.get("group") or ""),
                str(e.get("name") or ""),
                str(e["http"]["method"]),
                str(e["http"]["path"]),
            )
        )

    _check_collisions(surfaces)
    return surfaces


def _check_collisions(
    surfaces: dict[SurfaceKind, list[dict[str, Any]]],
) -> None:
    """Raise :class:`CollisionError` when any ``(group, name)`` repeats.

    Collisions are checked within each surface independently — a
    ``tokens.list`` in the workspace surface does not collide with a
    ``tokens.list`` in the admin surface (they live behind different
    Click root groups per spec §13 §"``crewday admin`` vs
    ``crewday deploy``"). The raised error lists every clashing pair
    with its operation ids and HTTP paths so the fix is mechanical:
    add an ``x-cli.group`` / ``x-cli.verb`` override on one of the
    routes.
    """
    clashes: list[str] = []
    for surface_kind, entries in surfaces.items():
        bucket: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entry in entries:
            key = (str(entry.get("group") or ""), str(entry.get("name") or ""))
            bucket.setdefault(key, []).append(entry)
        for (group, name), matches in sorted(bucket.items()):
            if len(matches) < 2:
                continue
            rows = "\n    ".join(
                f"{m.get('operation_id') or '<no-op-id>'}  "
                f"{m['http']['method']} {m['http']['path']}"
                for m in matches
            )
            clashes.append(
                f"[{surface_kind}] ({group!r}, {name!r}) has "
                f"{len(matches)} operations:\n    {rows}"
            )
    if clashes:
        raise CollisionError(
            "CLI surface has duplicate (group, verb) pairs — add an "
            "'x-cli.group' or 'x-cli.verb' override on one of the "
            "clashing routes (spec §12 §'CLI surface extensions'):\n\n"
            + "\n\n".join(clashes)
        )


def _ensure_root_key() -> None:
    """Set ``CREWDAY_ROOT_KEY`` to a dummy when unset.

    The app factory refuses to boot without an envelope key; codegen
    never decrypts anything, so the dummy value is fine. Leaving an
    operator-set key untouched matches the guarantee that running
    codegen against a real deployment does not accidentally overwrite
    secrets.
    """
    os.environ.setdefault("CREWDAY_ROOT_KEY", DummyRootKey.VALUE)


def load_live_schema() -> dict[str, Any]:
    """Boot the FastAPI app and return ``app.openapi()``.

    Imported lazily so the module stays cheap to import — the test
    suite exercises :func:`generate_surfaces` on synthetic inputs
    without paying the app-boot cost.
    """
    _ensure_root_key()
    # Lazy import: the runtime descriptor reader should never reach
    # :mod:`app.api.factory`, so keeping the edge inside the function
    # keeps the import graph clean.
    from app.api.factory import create_app

    app = create_app()
    return app.openapi()


def _serialise(entries: list[dict[str, Any]]) -> str:
    """Return the canonical JSON-string representation of ``entries``.

    ``sort_keys=True`` is paired with the entry-level sort in
    :func:`generate_surfaces` to give a byte-stable output across
    Python versions and dict-insertion orderings. Trailing newline
    keeps POSIX editors happy and matches the rest of the committed
    JSON tree (``docs/api/openapi.json``).
    """
    return json.dumps(entries, indent=2, sort_keys=True) + "\n"


def _read_committed(path: Path) -> str:
    """Return the committed file's contents, or empty when missing."""
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _write_if_changed(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` only if it differs. Return changed."""
    existing = _read_committed(path)
    if existing == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _format_diff(
    *,
    committed: str,
    fresh: str,
    label: str,
) -> str:
    """Return a unified-diff string naming ``label`` on both sides."""
    return "".join(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            fresh.splitlines(keepends=True),
            fromfile=f"{label} (committed)",
            tofile=f"{label} (fresh)",
            n=3,
        )
    )


def _check_surface(
    *,
    path: Path,
    entries: list[dict[str, Any]],
    label: str,
) -> tuple[bool, str]:
    """Return ``(changed, diff)`` for a single surface file."""
    fresh = _serialise(entries)
    committed = _read_committed(path)
    if committed == fresh:
        return False, ""
    return True, _format_diff(committed=committed, fresh=fresh, label=label)


def _run(args: argparse.Namespace) -> int:
    """Execute the selected mode. Returns a process exit code."""
    schema = load_live_schema()
    exclusions = load_exclusions(args.exclusions)
    try:
        surfaces = generate_surfaces(schema=schema, exclusions=exclusions)
    except CollisionError as exc:
        # Surface collisions are an API-author error, not an internal
        # bug — render the structured message without the argparse
        # wrapper's traceback so CI logs point straight at the fix.
        sys.stderr.write(f"crewday codegen: {exc}\n")
        return 2

    workspace_entries = surfaces["workspace"]
    admin_entries = surfaces["admin"]

    if args.dry_run:
        # Emit both surfaces to stdout as a single JSON object so a
        # human grepping can spot both at once and JSON tooling can
        # consume them. ``--dry-run`` is the "what would change?"
        # mode; drift detection is ``--check``.
        payload = {
            "_surface.json": workspace_entries,
            "_surface_admin.json": admin_entries,
        }
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return 0

    if args.check:
        workspace_changed, workspace_diff = _check_surface(
            path=args.surface,
            entries=workspace_entries,
            label=args.surface.name,
        )
        admin_changed, admin_diff = _check_surface(
            path=args.surface_admin,
            entries=admin_entries,
            label=args.surface_admin.name,
        )
        if not (workspace_changed or admin_changed):
            return 0

        # Drift: report WHICH operations changed on top of the raw
        # diff, so a reviewer can eyeball the impact without reading
        # the full JSON delta.
        sys.stderr.write("crewday codegen: committed surface out of date\n")
        for label, diff, changed in (
            (args.surface.name, workspace_diff, workspace_changed),
            (args.surface_admin.name, admin_diff, admin_changed),
        ):
            if changed:
                sys.stderr.write(f"\n--- {label} ---\n{diff}")
        sys.stderr.write("\nRun 'uv run python -m crewday._codegen' to regenerate.\n")
        return 1

    # Write mode.
    workspace_written = _write_if_changed(args.surface, _serialise(workspace_entries))
    admin_written = _write_if_changed(args.surface_admin, _serialise(admin_entries))
    if workspace_written:
        sys.stdout.write(f"wrote {args.surface} ({len(workspace_entries)} ops)\n")
    if admin_written:
        sys.stdout.write(f"wrote {args.surface_admin} ({len(admin_entries)} ops)\n")
    if not (workspace_written or admin_written):
        sys.stdout.write(
            f"surfaces unchanged ({len(workspace_entries)} workspace ops, "
            f"{len(admin_entries)} admin ops)\n"
        )
    return 0


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the ``python -m crewday._codegen`` argparse tree.

    argparse (rather than Click) because :mod:`crewday._runtime` will
    import :mod:`crewday._main` which already depends on Click — using
    Click here too would cross that seam and introduce an import cycle
    once the runtime loads the generated surfaces at import time.
    """
    parser = argparse.ArgumentParser(
        prog="python -m crewday._codegen",
        description=(
            "Generate cli/crewday/_surface.json + _surface_admin.json "
            "from the live FastAPI OpenAPI schema."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "Do not write files; exit 1 if the committed surface "
            "differs from a fresh generation (CI parity gate)."
        ),
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help=("Print the would-be surfaces to stdout as JSON without touching disk."),
    )
    parser.add_argument(
        "--surface",
        type=Path,
        default=DEFAULT_SURFACE_PATH,
        help="Path to the workspace surface file (default: %(default)s)",
    )
    parser.add_argument(
        "--surface-admin",
        type=Path,
        default=DEFAULT_SURFACE_ADMIN_PATH,
        help="Path to the admin surface file (default: %(default)s)",
    )
    parser.add_argument(
        "--exclusions",
        type=Path,
        default=DEFAULT_EXCLUSIONS_PATH,
        help="Path to the exclusions YAML (default: %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m crewday._codegen``.

    Returns the exit code so tests can call :func:`main` directly
    without spawning a subprocess. The module-level ``__main__`` guard
    forwards to :func:`sys.exit`.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    return _run(args)


if __name__ == "__main__":  # pragma: no cover — module CLI path.
    sys.exit(main())
