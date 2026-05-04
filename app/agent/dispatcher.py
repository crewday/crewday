"""OpenAPI-driven in-process :class:`ToolDispatcher` (cd-z3b7).

The agent runtime (:mod:`app.domain.agent.runtime`) takes a
:class:`~app.domain.agent.runtime.ToolDispatcher` Protocol port. This
module is its production implementation: it walks the FastAPI app's
OpenAPI surface to map ``ToolCall.name`` (the operation id) onto a
concrete ``(METHOD, path)`` pair and invokes the route in-process via
:class:`fastapi.testclient.TestClient`.

Spec references:

* ``docs/specs/11-llm-and-agents.md`` §"Embedded agents",
  §"The agent-first invariant" — every tool the agent can call is
  also reachable as a CLI verb, mirroring the same OpenAPI surface.
* ``docs/specs/12-rest-api.md`` §"Annotations",
  §"Agent confirmation extension" — the ``x-agent-confirm`` shape
  the gate decision walks.
* ``docs/specs/13-cli.md`` §"CLI generation from OpenAPI" — the same
  index pattern the CLI codegen uses (cd-lato), kept independent so
  this module does not depend on the CLI's serialised surface file.

The dispatcher carries two responsibilities:

* :meth:`OpenAPIToolDispatcher.is_gated` — pre-flight question the
  runtime asks before dispatching. The decision walks (in order)
  the workspace's "always gated" list, then the route's
  ``x-agent-confirm`` annotation. An unknown tool is **not** gated
  here — gating an unknown name would teach the model to bypass
  with a misspelling; the dispatch path returns a clean 404.
* :meth:`OpenAPIToolDispatcher.dispatch` — execute the call. The
  runtime hands a :class:`DelegatedToken` and the ``X-Agent-*``
  headers; the dispatcher folds the token onto an
  ``Authorization: Bearer …`` header and invokes the FastAPI route.

The module deliberately does **not** wire itself into the production
agent call site (no edit to ``app/api/v1/agent.py``) — that follow-up
lives on its own Beads task so the dispatcher contract can be
reviewed and tested in isolation first.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any, Final, Literal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.llm.ports import Tool
from app.domain.agent.runtime import (
    DelegatedToken,
    GateDecision,
    ToolCall,
    ToolResult,
)

__all__ = [
    "OpenAPIToolDispatcher",
    "make_default_dispatcher",
]


# Methods that mutate state on success. Used to seed
# :attr:`ToolResult.mutated`; the dispatcher additionally requires
# ``status_code < 400`` so a failed write reports ``mutated=False``
# (the runtime then skips the audit row, matching the §11 contract:
# audit rides on observable mutations only).
_MUTATING_METHODS: Final[frozenset[str]] = frozenset({"POST", "PATCH", "PUT", "DELETE"})

# Methods whose body the dispatcher serialises as the JSON request
# payload. ``GET``/``DELETE`` send their non-path inputs as query
# parameters (``DELETE`` body semantics are nominally allowed by
# RFC 9110 but ignored by most servers / clients; FastAPI's TestClient
# in particular round-trips body=None as Content-Length: 0 which
# trips a few downstream validators — keep it simple).
_BODY_METHODS: Final[frozenset[str]] = frozenset({"POST", "PATCH", "PUT"})


@dataclass(frozen=True, slots=True)
class _OperationEntry:
    """One row of the dispatcher's operation index.

    Frozen + slotted so the index can be pre-computed once at
    construction time and passed by reference through
    :meth:`OpenAPIToolDispatcher.dispatch`.
    """

    method: str
    path: str
    operation: Mapping[str, Any]


def _build_index(
    paths: Mapping[str, Any],
) -> dict[str, _OperationEntry]:
    """Walk ``paths`` and build the ``operationId`` -> entry index.

    Raises :class:`ValueError` when two operations share the same
    ``operationId`` — that would teach the model the wrong target
    and silently dispatch one of the two without recourse. The
    OpenAPI spec mandates uniqueness; we surface the violation
    loudly at construction so the operator notices before the
    dispatcher ever sees a real call.
    """
    index: dict[str, _OperationEntry] = {}
    for path, methods in paths.items():
        if not isinstance(methods, Mapping):
            continue
        for method, operation in methods.items():
            if not isinstance(method, str):
                continue
            if not isinstance(operation, Mapping):
                continue
            op_id = operation.get("operationId")
            if not isinstance(op_id, str) or not op_id:
                continue
            if op_id in index:
                prior = index[op_id]
                raise ValueError(
                    f"duplicate operationId {op_id!r}: "
                    f"{prior.method.upper()} {prior.path} vs "
                    f"{method.upper()} {path}"
                )
            index[op_id] = _OperationEntry(
                method=method.upper(),
                path=path,
                operation=operation,
            )
    return index


def _build_tool_catalog(
    index: Mapping[str, _OperationEntry],
    *,
    workspace_slug: str,
    components: Mapping[str, Any],
) -> tuple[Tool, ...]:
    """Build the deterministic function-calling catalog for ``index``."""
    return tuple(
        {
            "name": op_id,
            "description": _tool_description(op_id, entry.operation),
            "input_schema": _tool_input_schema(
                entry,
                workspace_slug=workspace_slug,
                components=components,
            ),
        }
        for op_id, entry in sorted(index.items())
    )


def _tool_description(op_id: str, operation: Mapping[str, Any]) -> str:
    raw = operation.get("description") or operation.get("summary")
    return raw if isinstance(raw, str) and raw.strip() else op_id


def _tool_input_schema(
    entry: _OperationEntry,
    *,
    workspace_slug: str,
    components: Mapping[str, Any],
) -> dict[str, object]:
    properties: dict[str, object] = {}
    required: list[str] = []
    advertised_path_params: set[str] = set()

    parameters = entry.operation.get("parameters")
    if not isinstance(parameters, list):
        parameters = []

    injected_slug = bool(workspace_slug) and "{slug}" in entry.path
    for param in parameters:
        if not isinstance(param, Mapping):
            continue
        loc = param.get("in")
        if loc not in ("path", "query"):
            continue
        name = param.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name == "slug" and injected_slug:
            continue
        properties[name] = _parameter_schema(param, components=components)
        if param.get("description") is not None:
            description = param["description"]
            if isinstance(description, str) and description:
                property_schema = properties[name]
                if isinstance(property_schema, dict):
                    property_schema.setdefault("description", description)
        if param.get("required") is True:
            required.append(name)
        if loc == "path":
            advertised_path_params.add(name)

    for name in sorted(_path_template_vars(entry.path) - advertised_path_params):
        if name == "slug" and injected_slug:
            continue
        properties[name] = {"type": "string"}
        required.append(name)

    body_schema = _request_body_schema(entry.operation, components=components)
    if body_schema is not None:
        if body_schema.get("type") == "object":
            body_properties = body_schema.get("properties")
            if isinstance(body_properties, Mapping):
                for name, body_property_schema in body_properties.items():
                    if isinstance(name, str):
                        properties[name] = (
                            dict(body_property_schema)
                            if isinstance(body_property_schema, Mapping)
                            else {}
                        )
            body_required = body_schema.get("required")
            if isinstance(body_required, list):
                required.extend(name for name in body_required if isinstance(name, str))
        else:
            properties["body"] = body_schema
            required.append("body")

    tool_schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
    }
    tool_schema["additionalProperties"] = False
    if required:
        tool_schema["required"] = sorted(set(required))
    return tool_schema


def _parameter_schema(
    parameter: Mapping[str, Any],
    *,
    components: Mapping[str, Any],
) -> dict[str, object]:
    raw_schema = parameter.get("schema")
    if not isinstance(raw_schema, Mapping):
        return {}
    return _resolve_local_refs(raw_schema, components=components)


def _request_body_schema(
    operation: Mapping[str, Any],
    *,
    components: Mapping[str, Any],
) -> dict[str, object] | None:
    request_body = operation.get("requestBody")
    if not isinstance(request_body, Mapping):
        return None
    content = request_body.get("content")
    if not isinstance(content, Mapping):
        return None
    media = content.get("application/json")
    if not isinstance(media, Mapping):
        return None
    schema = media.get("schema")
    return (
        _resolve_local_refs(schema, components=components)
        if isinstance(schema, Mapping)
        else None
    )


def _resolve_local_refs(
    schema: Mapping[str, Any],
    *,
    components: Mapping[str, Any],
    seen: frozenset[str] = frozenset(),
) -> dict[str, object]:
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        if ref in seen:
            return dict(schema)
        name = ref.rsplit("/", 1)[-1]
        schemas = components.get("schemas")
        if isinstance(schemas, Mapping):
            target = schemas.get(name)
            if isinstance(target, Mapping):
                return _resolve_local_refs(
                    target,
                    components=components,
                    seen=seen | {ref},
                )

    resolved: dict[str, object] = {}
    for key, value in schema.items():
        if isinstance(value, Mapping):
            resolved[key] = _resolve_local_refs(
                value,
                components=components,
                seen=seen,
            )
        elif isinstance(value, list):
            resolved[key] = [
                _resolve_local_refs(item, components=components, seen=seen)
                if isinstance(item, Mapping)
                else item
                for item in value
            ]
        else:
            resolved[key] = value
    return resolved


def _path_template_vars(path: str) -> set[str]:
    """Return ``{name}`` variables from an OpenAPI path template."""
    template_vars: set[str] = set()
    cursor = path
    while True:
        start = cursor.find("{")
        if start == -1:
            break
        end = cursor.find("}", start)
        if end == -1:
            break
        template_vars.add(cursor[start + 1 : end])
        cursor = cursor[end + 1 :]
    return template_vars


def _split_inputs(
    entry: _OperationEntry,
    inputs: Mapping[str, object],
    *,
    components: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, object], object | None]:
    """Split ``inputs`` into ``(path_vars, query_params, body)``.

    Path templates use the ``{name}`` syntax FastAPI emits; every
    such variable must appear in ``inputs`` or the call is
    rejected with :class:`ValueError`. The remaining keys are
    classified by the route's ``parameters`` array — anything
    declared ``in: query`` becomes a query parameter; anything
    declared ``in: path`` we already consumed; the leftovers become
    the body (for body-bearing methods) or query parameters
    (for ``GET``/``DELETE`` where body is not supported).

    Query is returned as a plain dict (not ``Mapping``) so the caller
    can mutate-merge without aliasing risk. Object request bodies use
    the leftover-input dict; non-object bodies are passed as the raw
    ``body`` argument advertised in the tool schema.
    """
    parameters = entry.operation.get("parameters")
    if not isinstance(parameters, list):
        parameters = []

    path_param_names: set[str] = set()
    query_param_names: set[str] = set()
    for param in parameters:
        if not isinstance(param, Mapping):
            continue
        loc = param.get("in")
        name = param.get("name")
        if not isinstance(name, str):
            continue
        if loc == "path":
            path_param_names.add(name)
        elif loc == "query":
            query_param_names.add(name)

    # Templates the path actually carries. We use the OpenAPI path
    # template (``/w/{slug}/...``) as the source of truth — a
    # ``parameters`` declaration that doesn't appear in the path is
    # still honoured (e.g. inherited query params), but a path
    # variable that's missing from ``parameters`` (older codegen)
    # still gets resolved.
    template_vars = _path_template_vars(entry.path)

    # Path-binding takes priority: any input key that matches a
    # path template var is bound there, even if the OpenAPI
    # ``parameters`` list also names it as a query.
    path_vars: dict[str, str] = {}
    remaining: dict[str, object] = dict(inputs)
    for var in template_vars | path_param_names:
        if var in remaining:
            value = remaining.pop(var)
            path_vars[var] = str(value)

    missing = template_vars - set(path_vars)
    if missing:
        raise ValueError(
            f"missing required path parameters for "
            f"{entry.method} {entry.path}: {sorted(missing)}"
        )

    if entry.method in _BODY_METHODS:
        # Body-bearing methods: anything declared ``in: query`` lifts
        # out as a query param; the rest is the JSON body. An empty
        # body becomes ``None`` so the TestClient sends
        # ``Content-Length: 0`` rather than ``"null"``.
        query: dict[str, object] = {}
        body: dict[str, object] = {}
        for key, value in remaining.items():
            if key in query_param_names:
                query[key] = value
            else:
                body[key] = value
        body_schema = _request_body_schema(entry.operation, components=components)
        if (
            body_schema is not None
            and body_schema.get("type") != "object"
            and set(body) == {"body"}
        ):
            return path_vars, query, body["body"]
        return path_vars, query, (body if body else None)

    # Read methods: the request has no body, so any input that
    # isn't a path var becomes a query param. We don't filter by
    # the declared ``query_param_names`` because the OpenAPI spec
    # doesn't always enumerate optional query knobs (FastAPI emits
    # them when the route uses :class:`Query`, but free-form
    # filters appear plain).
    return path_vars, remaining, None


def _format_path(template: str, path_vars: Mapping[str, str]) -> str:
    """Substitute ``{name}`` placeholders with URL-encoded values.

    Uses :class:`str.format_map` rather than
    :func:`urllib.parse.quote` per-segment so we get a
    deterministic exception (``KeyError``) on a missing var — the
    caller already validated the set, so a hit here would be a
    programming error.
    """
    # ``format_map`` calls ``str(value)`` for each placeholder; we
    # already coerced to str in ``_split_inputs`` so this is purely
    # a substitution pass. URL-encoding happens on the TestClient
    # side via :class:`httpx.URL`.
    return template.format_map(path_vars)


def _coerce_body(response_body: bytes, content_type: str | None) -> object:
    """Return the response body decoded for :attr:`ToolResult.body`.

    Empty body → ``None`` (the runtime renders ``null`` into the
    prompt context). JSON content type → :func:`json.loads`. Any
    other content type → the raw text. JSON that fails to parse
    falls back to the raw text so the LLM still has something to
    react to (rather than the dispatcher raising).
    """
    if not response_body:
        return None
    text = response_body.decode("utf-8", errors="replace")
    if content_type and "application/json" in content_type.lower():
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def _confirm_metadata(
    annotation: object,
) -> tuple[str, Literal["low", "medium", "high"]] | None:
    """Resolve ``x-agent-confirm`` into ``(summary, risk)`` or ``None``.

    Accepts both shapes the live codebase emits:

    * Object with ``summary`` (canonical per §12 "Agent confirmation
      extension") **or** ``message`` (older sites and the tests under
      ``cli/tests``); ``risk`` optional.
    * Boolean ``True`` (``app/api/v1/auth/me_avatar.py:467`` etc.) —
      generic message, default risk.

    Returns ``None`` when the annotation is absent or shaped in a
    way the dispatcher doesn't understand (so a malformed entry
    falls through to "not gated" rather than crashing the runtime).

    The default risk is ``medium`` per §12 "Agent confirmation
    extension" — that is the spec-mandated fallback when the
    annotation is present but ``risk`` is omitted.
    """
    if annotation is True:
        return "Confirm this action.", "medium"
    if isinstance(annotation, Mapping):
        # The spec says ``summary``, two early sites use ``message``;
        # accept either, ``summary`` wins when both appear.
        raw_summary = annotation.get("summary") or annotation.get("message")
        summary = (
            raw_summary if isinstance(raw_summary, str) else "Confirm this action."
        )
        raw_risk = annotation.get("risk")
        risk: Literal["low", "medium", "high"] = (
            raw_risk if raw_risk in ("low", "medium", "high") else "medium"
        )
        return summary, risk
    return None


class OpenAPIToolDispatcher:
    """In-process :class:`ToolDispatcher` backed by a FastAPI app.

    Construction pre-computes the ``operationId`` index so each
    :meth:`dispatch` is a flat hash lookup; the
    :class:`fastapi.testclient.TestClient` is built lazily on first
    use and reused. The class is safe to pin once per process —
    the index does not depend on per-call state.

    ``always_gated_tools`` is the workspace's policy projection:
    every tool name in the set goes through the HITL pipeline
    regardless of the route's annotation. Per §11 "Per-workspace
    always gated", the policy beats the route's per-call default,
    so this set is checked before the annotation.

    ``workspace_slug`` is auto-substituted into any ``{slug}``
    template in the matched path so the agent never has to thread
    the value through every call. **Routes whose path template does
    not carry ``{slug}`` ignore it silently** — admin-tooling,
    health, and bare-host routes (per §12 "Workspace-scoped paths")
    are reachable without complaint, and a workspace-pinned
    dispatcher won't accidentally inject ``slug`` into the body of
    such a route. Callers that must call cross-workspace routes
    can override the default by passing ``slug`` in
    :attr:`ToolCall.input`.
    """

    def __init__(
        self,
        *,
        app: FastAPI,
        openapi: Mapping[str, Any] | Callable[[], Mapping[str, Any]],
        workspace_slug: str,
        always_gated_tools: AbstractSet[str] = frozenset(),
    ) -> None:
        self._app = app
        self._workspace_slug = workspace_slug
        self._always_gated_tools = frozenset(always_gated_tools)
        schema = openapi() if callable(openapi) else openapi
        paths = schema.get("paths") if isinstance(schema, Mapping) else None
        if not isinstance(paths, Mapping):
            paths = {}
        self._index = _build_index(paths)
        components = schema.get("components") if isinstance(schema, Mapping) else None
        if not isinstance(components, Mapping):
            components = {}
        self._components = components
        self._tools = _build_tool_catalog(
            self._index,
            workspace_slug=self._workspace_slug,
            components=components,
        )
        # The TestClient is heavy (it spins a context manager around
        # the ASGI app); building it once and stashing the instance
        # keeps the dispatch hot-path flat. Tests can swap the field
        # in for a fake when they want to bypass the real app.
        self._client: TestClient | None = None

    # -- introspection (used by callers that want to enumerate tools)

    @property
    def operation_ids(self) -> frozenset[str]:
        """Return every tool name the dispatcher knows about."""
        return frozenset(self._index)

    @property
    def tools(self) -> tuple[Tool, ...]:
        """Return every OpenAPI operation as a provider-ready tool schema."""
        return self._tools

    # -- ToolDispatcher Protocol --------------------------------------

    def is_gated(self, call: ToolCall) -> GateDecision:
        """Walk the gate-decision rules described in the module docstring."""
        entry = self._index.get(call.name)
        if entry is None:
            # Unknown tool: do not gate. The dispatch path returns a
            # 404 and the runtime threads the result back into the
            # prompt; gating an unknown name would let a misspelling
            # bypass the workspace policy.
            return GateDecision(gated=False)

        if call.name in self._always_gated_tools:
            return GateDecision(
                gated=True,
                card_summary=f"{call.name} requires confirmation.",
                card_risk="medium",
                pre_approval_source="workspace_policy",
            )

        annotation = entry.operation.get("x-agent-confirm")
        resolved = _confirm_metadata(annotation)
        if resolved is None:
            return GateDecision(gated=False)
        summary, risk = resolved
        return GateDecision(
            gated=True,
            card_summary=summary,
            card_risk=risk,
            pre_approval_source="annotation",
        )

    def dispatch(
        self,
        call: ToolCall,
        *,
        token: DelegatedToken,
        headers: Mapping[str, str],
    ) -> ToolResult:
        """Resolve ``call`` against the index and invoke it in-process."""
        entry = self._index.get(call.name)
        if entry is None:
            return ToolResult(
                call_id=call.id,
                status_code=404,
                body={"detail": "tool not found"},
                mutated=False,
            )

        # Inject the dispatcher's pinned ``workspace_slug`` ahead of
        # the split so workspace-scoped paths (``/w/{slug}/...``)
        # bind without forcing every caller to thread it through. An
        # explicit ``slug`` in the call input wins (admin tooling can
        # target other workspaces if it has the authority). We only
        # inject when the path template actually carries ``{slug}``;
        # otherwise the value would leak into the body of a non-
        # workspace route and trip a server-side validator.
        merged_inputs: dict[str, object] = dict(call.input)
        if "{slug}" in entry.path and "slug" not in merged_inputs:
            merged_inputs["slug"] = self._workspace_slug

        try:
            path_vars, query, body = _split_inputs(
                entry,
                merged_inputs,
                components=self._components,
            )
        except ValueError as exc:
            # Bad input — return 422 so the LLM treats it the same way
            # FastAPI would surface a validation failure. The body
            # carries the structured error so the next prompt iteration
            # can correct itself.
            return ToolResult(
                call_id=call.id,
                status_code=422,
                body={"detail": str(exc)},
                mutated=False,
            )

        url = _format_path(entry.path, path_vars)
        request_headers = self._build_headers(token, headers, has_body=body is not None)

        client = self._get_client()
        # ``httpx`` accepts only str/int/float/bool/None values for
        # query params (or sequences thereof). The agent's input
        # surface is JSON-shaped, so any nested dict/list reaching
        # this branch is a programming error in the calling tool —
        # coerce primitives, JSON-encode the rest so the request
        # still goes out and the server-side validator can reject it
        # with a precise error.
        query_params: dict[str, str] = {}
        for key, value in query.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                query_params[key] = "" if value is None else str(value)
            else:
                query_params[key] = json.dumps(value)
        response = client.request(
            method=entry.method,
            url=url,
            params=query_params if query_params else None,
            json=body,
            headers=request_headers,
        )
        body_obj = _coerce_body(response.content, response.headers.get("content-type"))
        mutated = entry.method in _MUTATING_METHODS and response.status_code < 400
        return ToolResult(
            call_id=call.id,
            status_code=response.status_code,
            body=body_obj,
            mutated=mutated,
        )

    # -- internals -----------------------------------------------------

    def _get_client(self) -> TestClient:
        """Return the cached :class:`TestClient`, building it if needed.

        ``raise_server_exceptions=False`` lets a 500 from a broken
        handler surface as a real :class:`ToolResult` (status 500,
        body the error envelope) instead of bubbling out of the
        dispatcher and crashing the agent turn. The runtime then
        renders the failure into the prompt context so the LLM can
        react — a server fault should not silently truncate the
        conversation.
        """
        if self._client is None:
            self._client = TestClient(self._app, raise_server_exceptions=False)
        return self._client

    def _build_headers(
        self,
        token: DelegatedToken,
        caller_headers: Mapping[str, str],
        *,
        has_body: bool,
    ) -> dict[str, str]:
        """Merge caller headers with the runtime-mandated extras.

        Caller-supplied ``X-Agent-*`` values win over any default
        we'd compute here — the runtime is the source of truth for
        the audit attribution headers. ``Authorization`` is always
        rewritten to the delegated token: we never want a stale
        Bearer to leak in if the caller forgot to clear it.

        Header keys are case-insensitive on the wire; we collapse a
        caller's ``content-type`` onto the same dict slot as our
        pre-set ``Content-Type`` so the merged request doesn't carry
        two contradicting copies (httpx would normalise them, but the
        intent is clearer if we own the precedence here).
        """
        merged: dict[str, str] = {
            "Authorization": f"Bearer {token.plaintext}",
        }
        if has_body:
            merged["Content-Type"] = "application/json"
        # Case-insensitive lookup of pre-filled keys so a caller's
        # ``content-type`` lands on the same slot as ``Content-Type``
        # (and Authorization stays owned by us regardless of casing).
        canonical = {key.lower(): key for key in merged}
        for key, value in caller_headers.items():
            if key.lower() == "authorization":
                # Authorization is owned by us; drop the caller's copy.
                continue
            existing = canonical.get(key.lower())
            if existing is not None:
                merged[existing] = value
            else:
                merged[key] = value
                canonical[key.lower()] = key
        return merged


def make_default_dispatcher(
    app: FastAPI,
    workspace_slug: str,
    *,
    always_gated_tools: AbstractSet[str] = frozenset(),
) -> OpenAPIToolDispatcher:
    """Build a dispatcher against ``app``'s live OpenAPI surface.

    The factory delays calling :meth:`FastAPI.openapi` until the
    dispatcher actually needs it (the schema build is non-trivial on
    a large app — it walks every router). Production wiring that
    pre-computes the schema for caching can pass it in directly via
    the :class:`OpenAPIToolDispatcher` constructor.
    """
    return OpenAPIToolDispatcher(
        app=app,
        openapi=app.openapi,
        workspace_slug=workspace_slug,
        always_gated_tools=always_gated_tools,
    )
