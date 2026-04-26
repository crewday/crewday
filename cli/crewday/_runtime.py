"""Runtime command construction from ``_surface.json`` / ``_surface_admin.json``.

The codegen pipeline (Beads ``cd-1cfg``) freezes the FastAPI route
surface into two committed JSON files; this module turns those files
into a live :mod:`click` command tree at process start. The Click root
group from :mod:`crewday._main` is mounted with one
:class:`click.Group` per ``x_cli.group`` (or fallback ``group``) and
one command per ``x_cli.verb`` (or fallback ``name``) so users can
invoke ``crewday <group> <verb>`` without any per-endpoint Python
glue.

**Why generate at runtime rather than at codegen time.**  Generating
Python source would force a build step on every API change and double
the diff every time a route gains a parameter. Reading the descriptor
at startup keeps the surface a single committed JSON artefact; the
runtime cost is one ``json.load`` (cached via :func:`functools.cache`)
and one Click registration pass per process.

**Per-command behaviour.**  See the per-section docstrings below for:

* Path / query / body parameter wiring (§13 §"Runtime command
  construction", §"Parameter mapping").
* ``Idempotency-Key`` semantics — automatic on ``POST`` per §12
  §"Idempotency"; ``--idempotency-key`` overrides the auto-generated
  ULID. ``PATCH`` / ``PUT`` / ``DELETE`` do **not** auto-attach the
  header because the spec only documents ``POST`` as the verb the
  server caches against the key.
* Pagination — ``--all`` follows the cursor envelope by delegating
  to :meth:`crewday._client.CrewdayClient.iterate`; with
  ``--output ndjson`` the rows stream one JSON object per line.
* Error mapping — :class:`crewday._client.ApiError` propagates
  unchanged; :func:`crewday._main.handle_errors` already maps it to
  the right §13 exit code.

See ``docs/specs/13-cli.md`` §"Runtime command construction",
§"Pagination", §"Output", §"Idempotency-Key";
``docs/specs/12-rest-api.md`` §"Idempotency", §"Pagination".
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any, Final

import click
import httpx

from crewday._client import CrewdayClient
from crewday._globals import CrewdayContext
from crewday._main import ConfigError

__all__ = [
    "DEFAULT_SURFACE_ADMIN_PATH",
    "DEFAULT_SURFACE_PATH",
    "ClientFactory",
    "SurfaceEntry",
    "SurfaceHttp",
    "SurfaceParam",
    "load_surface",
    "register_generated_commands",
]


# ---------------------------------------------------------------------------
# Descriptor paths + types
# ---------------------------------------------------------------------------


_PACKAGE_DIR: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parent
DEFAULT_SURFACE_PATH: Final[pathlib.Path] = _PACKAGE_DIR / "_surface.json"
DEFAULT_SURFACE_ADMIN_PATH: Final[pathlib.Path] = _PACKAGE_DIR / "_surface_admin.json"


# Body-bearing methods. ``GET`` and ``DELETE`` never expose ``--field``
# / ``--body-file`` even when the surface entry has a ``body_schema_ref``
# (which a defensively-coded handler may declare for documentation
# without actually reading the body). The CLI follows §13's contract
# of "body wiring only on body-bearing verbs" so a user trying to send
# JSON on a ``GET`` lands on a clean ``no such option`` error rather
# than an inert flag.
_BODY_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH"})

# Server-side idempotency-store methods per §12 §"Idempotency": only
# ``POST`` is cached as ``(token_id, key) -> (status, body_hash)`` for
# 24 h. ``PATCH`` / ``PUT`` / ``DELETE`` are not in the cache table; the
# CLI therefore does not auto-mint a key for them. A user who wants one
# anyway can pass ``--idempotency-key`` explicitly.
_AUTO_IDEMPOTENT_METHODS: Final[frozenset[str]] = frozenset({"POST"})


@dataclass(frozen=True, slots=True)
class SurfaceHttp:
    """The HTTP coordinates of a generated command."""

    method: str
    path: str


@dataclass(frozen=True, slots=True)
class SurfaceParam:
    """One path or query parameter from the descriptor."""

    name: str
    required: bool
    schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SurfaceEntry:
    """Typed wrapper around one entry in ``_surface*.json``.

    The descriptor is the wire-stable contract; this dataclass is the
    in-memory shape consumed by the runtime. Anything we add to the
    JSON shape (e.g. agent-confirmation copy) lands here as a typed
    field so call sites read like a model, not a dict-of-Any.
    """

    name: str
    group: str
    operation_id: str | None
    summary: str | None
    http: SurfaceHttp
    idempotent: bool
    path_params: tuple[SurfaceParam, ...]
    query_params: tuple[SurfaceParam, ...]
    body_schema_ref: str | None
    response_schema_ref: str | None
    x_cli: Mapping[str, Any] | None
    x_agent_confirm: Mapping[str, Any] | None

    @property
    def cli_group(self) -> str:
        """Resolved group: ``x_cli.group`` if set, else ``group``."""
        if self.x_cli is not None:
            override = self.x_cli.get("group")
            if isinstance(override, str) and override:
                return override
        return self.group

    @property
    def cli_verb(self) -> str:
        """Resolved verb: ``x_cli.verb`` if set, else ``name``."""
        if self.x_cli is not None:
            override = self.x_cli.get("verb")
            if isinstance(override, str) and override:
                return override
        return self.name

    @property
    def has_cursor_pagination(self) -> bool:
        """``True`` when the entry exposes a ``cursor`` query parameter.

        Detection is structural: an endpoint with a ``cursor`` query
        param is a paginated list per §12 §"Pagination". The CLI then
        wires ``--all`` to walk it via
        :meth:`crewday._client.CrewdayClient.iterate`. We deliberately
        do not inspect the response schema because the surface only
        carries the ``$ref`` (we'd have to round-trip to the live
        OpenAPI to resolve it — pointless when the cursor query param
        is the same signal).
        """
        return any(p.name == "cursor" for p in self.query_params)


ClientFactory = Callable[[CrewdayContext], CrewdayClient]
"""Callable that builds a :class:`CrewdayClient` from the active context.

The default factory raises :class:`ConfigError` because profile
resolution lives in the not-yet-built :mod:`crewday._config` module
(Beads ``cd-cksj``). Tests inject a factory that returns a
pre-configured client wired to :class:`httpx.MockTransport`.
"""


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _coerce_param(raw: Mapping[str, Any]) -> SurfaceParam:
    """Promote one descriptor-shaped dict into a :class:`SurfaceParam`."""
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"surface param missing 'name': {raw!r}")
    schema = raw.get("schema")
    if not isinstance(schema, Mapping):
        # The codegen always emits a dict here, even for parameters
        # with no further constraints — schema=={} is a legal empty
        # mapping. A non-mapping value indicates a corrupt descriptor.
        raise ValueError(f"surface param {name!r} has non-mapping schema")
    return SurfaceParam(
        name=name,
        required=bool(raw.get("required", False)),
        schema=schema,
    )


def _coerce_entry(raw: Mapping[str, Any]) -> SurfaceEntry:
    """Promote one descriptor-shaped dict into a :class:`SurfaceEntry`."""
    http_raw = raw.get("http")
    if not isinstance(http_raw, Mapping):
        raise ValueError(f"surface entry missing 'http' mapping: {raw!r}")
    method = http_raw.get("method")
    path = http_raw.get("path")
    if not isinstance(method, str) or not method:
        raise ValueError(f"surface entry has invalid http.method: {http_raw!r}")
    if not isinstance(path, str) or not path:
        raise ValueError(f"surface entry has invalid http.path: {http_raw!r}")

    name = raw.get("name")
    group = raw.get("group")
    if not isinstance(name, str) or not name:
        raise ValueError(f"surface entry missing 'name': {raw!r}")
    if not isinstance(group, str) or not group:
        raise ValueError(f"surface entry missing 'group': {raw!r}")

    raw_path_params = raw.get("path_params") or []
    raw_query_params = raw.get("query_params") or []
    if not isinstance(raw_path_params, list) or not isinstance(raw_query_params, list):
        raise ValueError(f"surface entry has malformed param lists: {raw!r}")

    return SurfaceEntry(
        name=name,
        group=group,
        operation_id=_optional_str(raw.get("operation_id")),
        summary=_optional_str(raw.get("summary")),
        http=SurfaceHttp(method=method.upper(), path=path),
        idempotent=bool(raw.get("idempotent", False)),
        path_params=tuple(_coerce_param(p) for p in raw_path_params),
        query_params=tuple(_coerce_param(p) for p in raw_query_params),
        body_schema_ref=_optional_str(raw.get("body_schema_ref")),
        response_schema_ref=_optional_str(raw.get("response_schema_ref")),
        x_cli=raw.get("x_cli") if isinstance(raw.get("x_cli"), Mapping) else None,
        x_agent_confirm=(
            raw.get("x_agent_confirm")
            if isinstance(raw.get("x_agent_confirm"), Mapping)
            else None
        ),
    )


def _optional_str(value: object) -> str | None:
    """Return ``value`` if it is a non-empty string, else ``None``."""
    if isinstance(value, str) and value:
        return value
    return None


def _read_surface_file(path: pathlib.Path) -> list[Mapping[str, Any]]:
    """Read one descriptor file, tolerating an empty list (``[]``).

    A missing file is **not** silently ignored — the descriptor is the
    contract between codegen and runtime, and a missing file means the
    CLI is shipping without a surface (which would silently render an
    empty command tree). The admin descriptor is permitted to be the
    literal empty list because the admin route tree is not yet
    populated; an *absent* file still raises.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"surface descriptor missing: {path}. Run "
            "'uv run python -m crewday._codegen' to regenerate."
        )
    with path.open("rb") as fh:
        loaded = json.load(fh)
    if not isinstance(loaded, list):
        raise ValueError(
            f"surface descriptor at {path} must be a JSON list, got "
            f"{type(loaded).__name__}"
        )
    # ``[]`` is legal — admin surface today. A list of non-dicts is not.
    for entry in loaded:
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"surface descriptor at {path} contains non-mapping entry: {entry!r}"
            )
    return loaded


@cache
def load_surface(
    *,
    workspace_path: pathlib.Path = DEFAULT_SURFACE_PATH,
    admin_path: pathlib.Path = DEFAULT_SURFACE_ADMIN_PATH,
) -> tuple[SurfaceEntry, ...]:
    """Read both descriptor files, merge, return a typed tuple.

    Cached via :func:`functools.cache` keyed on the path arguments —
    the default path arguments cache the package-shipped descriptors;
    tests pin alternate paths (e.g. tmp-path fixtures) and get their
    own cache entry. The cache is process-wide; tests that need to
    re-load after editing the JSON should call
    :meth:`load_surface.cache_clear`.
    """
    workspace_entries = _read_surface_file(workspace_path)
    admin_entries = _read_surface_file(admin_path)
    merged: list[SurfaceEntry] = [
        _coerce_entry(raw) for raw in (*workspace_entries, *admin_entries)
    ]
    return tuple(merged)


# ---------------------------------------------------------------------------
# Type coercion helpers for Click options
# ---------------------------------------------------------------------------


def _resolve_schema_type(schema: Mapping[str, Any]) -> str:
    """Return the primary OpenAPI type for a parameter schema.

    Handles the FastAPI-emitted ``anyOf: [{type: X}, {type: null}]``
    optional pattern by picking the non-null branch — we only care
    about the value type at the CLI layer; ``required=False`` already
    encodes the optionality.
    """
    direct_type = schema.get("type")
    if isinstance(direct_type, str):
        return direct_type
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        for branch in any_of:
            if not isinstance(branch, Mapping):
                continue
            branch_type = branch.get("type")
            if isinstance(branch_type, str) and branch_type != "null":
                return branch_type
    return "string"


def _click_type_for(schema: Mapping[str, Any]) -> click.ParamType:
    """Return the :class:`click.ParamType` matching ``schema``."""
    type_name = _resolve_schema_type(schema)
    if type_name == "integer":
        return click.INT
    if type_name == "number":
        return click.FLOAT
    if type_name == "boolean":
        return click.BOOL
    # ``string``, ``array`` (element type still string at CLI level),
    # any unknown — default to plain string. The server validates the
    # exact shape; round-tripping through Click's stricter types would
    # double-validate and risk drift.
    return click.STRING


def _array_inner_schema(schema: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the element schema if ``schema`` describes an array.

    The descriptor preserves OpenAPI's ``{type: array, items: {...}}``
    shape verbatim. We use the element schema to type-coerce repeated
    flag values (``--tag a --tag b``).
    """
    if _resolve_schema_type(schema) != "array":
        return None
    items = schema.get("items")
    return items if isinstance(items, Mapping) else {}


def _option_name(param_name: str) -> str:
    """Map a parameter name (snake_case) to a CLI option (kebab-case).

    The argument-name argument keeps the underscore form so the Click
    callback receives ``credential_id`` rather than ``credential-id``,
    matching the descriptor key. The flag itself uses ``--``-kebab so
    long flags read naturally on the command line.
    """
    return f"--{param_name.replace('_', '-')}"


# ---------------------------------------------------------------------------
# Body assembly
# ---------------------------------------------------------------------------


def _parse_field_pair(raw: str) -> tuple[str, str]:
    """Split ``"key=value"`` into ``(key, value)``.

    Click validates that each ``--field`` argument has the ``=`` form;
    a missing ``=`` raises :class:`click.BadParameter` so the user
    sees a focused message rather than a server-side validation error
    five steps later. ``value`` may be an empty string (``--field
    note=``) which is forwarded as-is; the server decides whether an
    empty string is acceptable for that field.
    """
    if "=" not in raw:
        raise click.BadParameter(
            f"--field must be of the form KEY=VALUE (got {raw!r})",
            param_hint="--field",
        )
    key, _, value = raw.partition("=")
    if not key:
        raise click.BadParameter(
            f"--field KEY must not be empty (got {raw!r})",
            param_hint="--field",
        )
    return key, value


def _build_body(
    *,
    field_pairs: tuple[str, ...],
    body_file: pathlib.Path | None,
) -> Any | None:
    """Compose the JSON request body from CLI args.

    Mutually exclusive: passing both ``--field`` and ``--body-file``
    is a user error (the source of truth would be ambiguous). Returns
    ``None`` when neither is supplied so the client can omit the body
    altogether (FastAPI tolerates a missing ``application/json`` body
    when every field is optional).
    """
    if field_pairs and body_file is not None:
        raise click.UsageError(
            "--field and --body-file are mutually exclusive; pick one."
        )
    if body_file is not None:
        try:
            with body_file.open("rb") as fh:
                return json.load(fh)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(
                f"--body-file {body_file} is not valid JSON: {exc.msg}",
                param_hint="--body-file",
            ) from exc
    if not field_pairs:
        return None
    body: dict[str, str] = {}
    for raw in field_pairs:
        key, value = _parse_field_pair(raw)
        body[key] = value  # last write wins, per the spec
    return body


# ---------------------------------------------------------------------------
# Click option assembly
# ---------------------------------------------------------------------------


def _attach_path_options(
    decorator_chain: Callable[..., Any],
    params: tuple[SurfaceParam, ...],
) -> Callable[..., Any]:
    """Stack ``click.option`` for each path parameter onto ``decorator_chain``.

    Path params are always required at the CLI layer — the URL cannot
    be constructed without them. ``slug`` is *not* in this list (it
    flows from ``--workspace`` / ``CREWDAY_WORKSPACE``); FastAPI's
    OpenAPI listing already excludes it from the per-route parameters
    because it lives on the workspace router prefix.
    """
    chain = decorator_chain
    # Iterate in reverse so the first param in the descriptor renders
    # leftmost in ``--help`` (Click stacks decorators bottom-up).
    for param in reversed(params):
        chain = click.option(
            _option_name(param.name),
            param.name,
            type=_click_type_for(param.schema),
            required=True,
            help=f"path parameter {param.name!r}",
        )(chain)
    return chain


def _attach_query_options(
    decorator_chain: Callable[..., Any],
    params: tuple[SurfaceParam, ...],
    *,
    skip_cursor_param: bool,
) -> Callable[..., Any]:
    """Stack ``click.option`` for each query parameter.

    ``skip_cursor_param`` suppresses the literal ``cursor`` query flag
    when the entry is paginated — the runtime owns cursor walking via
    ``--all`` (or returns the server-supplied next cursor on a
    single-page call), so a user-visible ``--cursor`` flag would be
    redundant and confusing.
    """
    chain = decorator_chain
    for param in reversed(params):
        if skip_cursor_param and param.name == "cursor":
            continue
        inner = _array_inner_schema(param.schema)
        if inner is not None:
            # Repeated flag: ``--tag a --tag b`` → ``("a", "b")``. We
            # type-coerce the element using the inner schema so an
            # ``array of integer`` flag still parses each element as
            # an int (the server expects the list of typed values).
            chain = click.option(
                _option_name(param.name),
                param.name,
                type=_click_type_for(inner),
                multiple=True,
                required=param.required,
                help=f"query parameter {param.name!r} (repeatable)",
            )(chain)
            continue

        default = param.schema.get("default")
        if default is None and param.required:
            chain = click.option(
                _option_name(param.name),
                param.name,
                type=_click_type_for(param.schema),
                required=True,
                help=f"query parameter {param.name!r}",
            )(chain)
        else:
            chain = click.option(
                _option_name(param.name),
                param.name,
                type=_click_type_for(param.schema),
                default=default,
                show_default=default is not None,
                required=False,
                help=f"query parameter {param.name!r}",
            )(chain)
    return chain


def _attach_body_options(decorator_chain: Callable[..., Any]) -> Callable[..., Any]:
    """Stack ``--field`` (repeating) and ``--body-file`` onto a body verb."""
    chain = decorator_chain
    chain = click.option(
        "--body-file",
        "body_file",
        type=click.Path(
            exists=True,
            dir_okay=False,
            readable=True,
            path_type=pathlib.Path,
        ),
        default=None,
        help="JSON file to send as the request body (mutually exclusive with --field).",
    )(chain)
    chain = click.option(
        "--field",
        "field_pairs",
        multiple=True,
        metavar="KEY=VALUE",
        help="One key=value pair to send in the JSON body. Repeat for each field.",
    )(chain)
    return chain


def _attach_idempotency_option(
    decorator_chain: Callable[..., Any],
) -> Callable[..., Any]:
    """Stack ``--idempotency-key`` for verbs that auto-attach a key."""
    return click.option(
        "--idempotency-key",
        "idempotency_key",
        default=None,
        help=(
            "Override the auto-generated Idempotency-Key (§12 "
            "Idempotency); a stable value lets you retry safely."
        ),
    )(decorator_chain)


def _attach_pagination_option(
    decorator_chain: Callable[..., Any],
) -> Callable[..., Any]:
    """Stack ``--all`` for cursor-paginated list verbs."""
    return click.option(
        "--all",
        "follow_all",
        is_flag=True,
        default=False,
        help=(
            "Follow the cursor envelope and return every row "
            "(streams as ndjson under -o ndjson)."
        ),
    )(decorator_chain)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


# TODO(cd-oe5j): route through crewday._output.format() once the
# json/yaml/table/ndjson formatter lands. For now every command emits
# pretty-printed JSON via json.dumps so agents can pipe through jq.
def _emit(payload: object) -> None:
    """Write ``payload`` to stdout as pretty JSON."""
    click.echo(json.dumps(payload, indent=2, sort_keys=False, default=str))


def _emit_ndjson(rows: Iterator[Mapping[str, Any]]) -> None:
    """Stream ``rows`` to stdout as NDJSON (one JSON object per line)."""
    for row in rows:
        sys.stdout.write(json.dumps(row, sort_keys=False, default=str))
        sys.stdout.write("\n")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Path / URL assembly
# ---------------------------------------------------------------------------


def _resolve_path(
    *,
    template: str,
    path_param_values: Mapping[str, object],
    workspace: str | None,
) -> str:
    """Substitute ``{slug}`` and other path params into ``template``.

    ``{slug}`` is filled from ``CrewdayContext.workspace`` (set via
    ``--workspace`` / ``CREWDAY_WORKSPACE``); a missing workspace on a
    ``/w/{slug}/...`` path raises :class:`ConfigError` so the §13
    exit-code mapping lands on the config-error slot. Other path
    params must be supplied via their ``--<name>`` Click option (Click
    enforces ``required=True``); we still defend against accidental
    omission with a clear error.

    Every path-param value is URL-encoded with ``safe=""`` so a value
    that happens to contain ``/``, ``?``, ``#``, ``%``, or whitespace
    cannot escape its component and route to a different endpoint —
    today's surface uses ULIDs and UUIDs that round-trip unchanged,
    but the next agent who adds a free-text path param shouldn't have
    to remember to escape. The slug is shape-validated upstream
    (``^[a-z][a-z0-9-]{1,38}[a-z0-9]$``) so the encoding is a no-op
    for it; we still pass it through the same helper so the contract
    is uniform.
    """
    substitutions: dict[str, str] = {}
    if "{slug}" in template:
        if not workspace:
            raise ConfigError(
                "this command targets /w/<slug>/... but no workspace is set "
                "(pass --workspace or set CREWDAY_WORKSPACE)."
            )
        substitutions["slug"] = urllib.parse.quote(workspace, safe="")
    for name, value in path_param_values.items():
        substitutions[name] = urllib.parse.quote(str(value), safe="")
    try:
        return template.format(**substitutions)
    except KeyError as missing:
        raise ConfigError(
            f"path template {template!r} requires {missing.args[0]!r} "
            "but it was not supplied."
        ) from None


def _build_query_params(
    *,
    query_params: tuple[SurfaceParam, ...],
    cli_values: Mapping[str, object],
) -> dict[str, Any]:
    """Project the Click-collected values into the HTTP query dict.

    ``None`` and empty tuples (no ``--tag`` repeats) are dropped so we
    don't send ``?cursor=&open_only=`` triplets that the server would
    otherwise interpret as the literal empty string. The cursor param
    is included if it landed on ``cli_values`` — the runtime suppresses
    its option (see :func:`_attach_query_options`) but keeps the slot
    so the pagination loop can write to it.
    """
    out: dict[str, Any] = {}
    for param in query_params:
        if param.name not in cli_values:
            continue
        value = cli_values[param.name]
        if value is None:
            continue
        if isinstance(value, tuple):
            if not value:
                continue
            out[param.name] = list(value)
            continue
        out[param.name] = value
    return out


# ---------------------------------------------------------------------------
# Command callback
# ---------------------------------------------------------------------------


def _narrow_field_pairs(value: object) -> tuple[str, ...]:
    """Return ``value`` as a tuple of field-pair strings.

    Click's ``multiple=True`` always hands the callback a ``tuple`` of
    the option's parsed type (here: ``str``). We narrow defensively so
    a future surface entry mis-emitting a non-string element fails
    fast in our code rather than deep inside :func:`_parse_field_pair`.
    """
    if value is None:
        return ()
    if not isinstance(value, tuple):
        raise TypeError(
            f"--field option must collect into a tuple, got {type(value).__name__}"
        )
    if not all(isinstance(item, str) for item in value):
        raise TypeError("--field values must all be strings")
    return value


def _narrow_body_file(value: object) -> pathlib.Path | None:
    """Return ``value`` as an optional :class:`pathlib.Path`.

    Click's ``click.Path(path_type=pathlib.Path)`` either yields
    ``None`` (when the option was omitted) or a real
    :class:`pathlib.Path`; this helper just makes that guarantee
    explicit at the call site.
    """
    if value is None:
        return None
    if not isinstance(value, pathlib.Path):
        raise TypeError(
            f"--body-file must resolve to a pathlib.Path, got {type(value).__name__}"
        )
    return value


def _make_callback(
    *,
    entry: SurfaceEntry,
    client_factory: ClientFactory,
) -> Callable[..., None]:
    """Return the Click callback that executes ``entry``.

    The callback is a closure over ``entry`` + ``client_factory``;
    every per-invocation dependency (workspace slug, output mode,
    idempotency-key generator) flows through :class:`CrewdayContext`
    via :func:`click.pass_obj`. Tests inject ``client_factory`` to
    swap the real HTTP transport for a :class:`httpx.MockTransport`.
    """
    path_param_names = {p.name for p in entry.path_params}
    query_param_names = {p.name for p in entry.query_params}
    body_methods = entry.http.method in _BODY_METHODS
    auto_idempotent = entry.http.method in _AUTO_IDEMPOTENT_METHODS
    paginated = entry.has_cursor_pagination

    @click.pass_obj
    def callback(ctx: CrewdayContext, /, **kwargs: object) -> None:
        path_values = {k: kwargs[k] for k in path_param_names if k in kwargs}
        query_values = {k: kwargs[k] for k in query_param_names if k in kwargs}

        url = _resolve_path(
            template=entry.http.path,
            path_param_values=path_values,
            workspace=ctx.workspace,
        )
        params = _build_query_params(
            query_params=entry.query_params,
            cli_values=query_values,
        )

        body: object | None = None
        if body_methods:
            body = _build_body(
                field_pairs=_narrow_field_pairs(kwargs.get("field_pairs")),
                body_file=_narrow_body_file(kwargs.get("body_file")),
            )

        idempotency_key: str | None = None
        if auto_idempotent:
            override = kwargs.get("idempotency_key")
            if isinstance(override, str) and override:
                idempotency_key = override
            else:
                idempotency_key = ctx.idempotency_key_factory()

        with client_factory(ctx) as client:
            if paginated and bool(kwargs.get("follow_all", False)):
                _run_paginated(client=client, url=url, params=params, ctx=ctx)
                return

            response = client.request(
                entry.http.method,
                url,
                json=body,
                params=params,
                idempotency_key=idempotency_key,
            )
            _emit_response(response)

    return callback


def _emit_response(response: httpx.Response) -> None:
    """Pretty-print a response body, tolerating empty or non-JSON payloads.

    A 204-style empty body lands as ``""`` from :attr:`httpx.Response.text`;
    we emit ``null`` so JSON consumers (``jq``, downstream agents) see a
    valid document. Non-JSON bodies (rare on the v1 API; the server emits
    JSON for every documented endpoint) fall back to the raw text wrapped
    in ``{"raw": "..."}`` so the output stays JSON-shaped.
    """
    text = response.text
    if not text:
        _emit(None)
        return
    try:
        payload = response.json()
    except ValueError:
        _emit({"raw": text})
        return
    _emit(payload)


def _run_paginated(
    *,
    client: CrewdayClient,
    url: str,
    params: Mapping[str, Any],
    ctx: CrewdayContext,
) -> None:
    """Walk ``client.iterate(url, ...)`` honouring the active output mode.

    Under ``--output ndjson`` rows stream one per line so a downstream
    ``jq`` pipeline can start emitting before the server finishes
    sending the last page. Under any other output mode the rows are
    aggregated into a single JSON list — agents that want one JSON
    document still get one even when the server paginated.
    """
    iterator = client.iterate(url, params=params)
    if ctx.output == "ndjson":
        _emit_ndjson(iterator)
        return
    _emit(list(iterator))


# ---------------------------------------------------------------------------
# Group / command registration
# ---------------------------------------------------------------------------


def _build_command(
    entry: SurfaceEntry,
    *,
    client_factory: ClientFactory,
) -> click.Command:
    """Compose one :class:`click.Command` for a surface entry."""
    callback = _make_callback(entry=entry, client_factory=client_factory)

    decorated: Callable[..., Any] = callback
    decorated = _attach_path_options(decorated, entry.path_params)
    decorated = _attach_query_options(
        decorated,
        entry.query_params,
        skip_cursor_param=entry.has_cursor_pagination,
    )

    if entry.http.method in _BODY_METHODS:
        decorated = _attach_body_options(decorated)

    if entry.http.method in _AUTO_IDEMPOTENT_METHODS:
        decorated = _attach_idempotency_option(decorated)

    if entry.has_cursor_pagination:
        decorated = _attach_pagination_option(decorated)

    summary = (
        ((entry.x_cli or {}).get("summary") if entry.x_cli is not None else None)
        or entry.summary
        or f"{entry.http.method} {entry.http.path}"
    )

    cmd = click.command(
        name=entry.cli_verb,
        help=summary,
    )(decorated)
    return cmd


def _default_client_factory(ctx: CrewdayContext) -> CrewdayClient:
    """Production stub until profile resolution lands (Beads cd-cksj).

    Raising :class:`ConfigError` (rather than instantiating with a
    placeholder URL) means a user who runs a generated command before
    the profile system exists sees a focused error pointing at the
    Beads task — not a spurious connection refused on a localhost URL.
    """
    raise ConfigError(
        "profile resolution is not yet implemented; pass a "
        "client_factory= to register_generated_commands() or wait for "
        "Beads cd-cksj to land the config loader."
    )


def register_generated_commands(
    root: click.Group,
    *,
    client_factory: ClientFactory | None = None,
    workspace_path: pathlib.Path = DEFAULT_SURFACE_PATH,
    admin_path: pathlib.Path = DEFAULT_SURFACE_ADMIN_PATH,
) -> None:
    """Mount every entry from the descriptor onto ``root``.

    Idempotent at the descriptor level: re-loading the descriptor is
    a cache hit, but re-registering on the same root would shadow the
    earlier commands silently. Callers (currently only
    :mod:`crewday._main`) call this once at startup; tests that build
    their own root call it once on that root.

    ``client_factory`` defaults to :func:`_default_client_factory`,
    which raises :class:`ConfigError` until cd-cksj lands the profile
    loader. Tests inject a factory that returns a pre-configured
    :class:`CrewdayClient` wired to :class:`httpx.MockTransport`.
    """
    factory = client_factory if client_factory is not None else _default_client_factory
    entries = load_surface(workspace_path=workspace_path, admin_path=admin_path)

    groups: dict[str, click.Group] = {}
    for entry in entries:
        group_name = entry.cli_group
        if group_name not in groups:
            group = click.Group(
                name=group_name,
                help=f"{group_name} commands",
            )
            groups[group_name] = group
            root.add_command(group)
        groups[group_name].add_command(_build_command(entry, client_factory=factory))
