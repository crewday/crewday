"""Resolve generated ``x-agent-links`` metadata for CLI output."""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "AgentLinkRoutes",
    "ResolvedAgentLinks",
    "RouteDefinition",
    "coerce_route_definition",
    "resolve_agent_links",
]


ResolvedAgentLinks = dict[str, object]
AgentLinkRoutes = Mapping[str, "RouteDefinition"]


@dataclass(frozen=True, slots=True)
class RouteDefinition:
    name: str
    scope: str
    template: str
    params: frozenset[str]
    query: frozenset[str]


def coerce_route_definition(raw: object) -> RouteDefinition | None:
    route = _as_mapping(raw)
    if route is None:
        return None
    name = route.get("name")
    scope = route.get("scope")
    template = route.get("template")
    if not (
        isinstance(name, str)
        and name
        and isinstance(scope, str)
        and scope in {"workspace", "admin", "public"}
        and isinstance(template, str)
        and template.startswith("/")
    ):
        return None
    return RouteDefinition(
        name=name,
        scope=scope,
        template=template,
        params=frozenset(_field_names(route.get("params"))),
        query=frozenset(_field_names(route.get("query"))),
    )


def resolve_agent_links(
    policy: object,
    *,
    routes: AgentLinkRoutes,
    workspace_slug: str | None,
    path_vars: Mapping[str, object],
    query: Mapping[str, object],
    request_body: object,
    response_body: object,
) -> ResolvedAgentLinks | None:
    raw_policy = _as_mapping(policy)
    if raw_policy is None or raw_policy.get("policy") != "links":
        return None
    raw_links = raw_policy.get("links")
    if not isinstance(raw_links, list):
        return None

    context = _ResolveContext(
        workspace_slug=workspace_slug,
        path_vars=path_vars,
        query=query,
        request_body=request_body,
        response_body=response_body,
    )
    top_level: list[dict[str, object]] = []
    item_groups: list[dict[str, object]] = []
    warnings: list[dict[str, str]] = []
    for raw_link in raw_links:
        link = _as_mapping(raw_link)
        if link is None:
            continue
        if _uses_item_binding(link):
            _resolve_item_links(
                link,
                routes=routes,
                context=context,
                item_groups=item_groups,
                warnings=warnings,
            )
            continue
        resolved = _resolve_one_link(link, routes=routes, context=context)
        if isinstance(resolved, dict):
            top_level.append(resolved)
        elif isinstance(resolved, str):
            warnings.append({"rel": _link_rel(link), "reason": resolved})

    if not top_level and not item_groups and not warnings:
        return None
    payload: ResolvedAgentLinks = {"links": top_level}
    if item_groups:
        payload["items"] = item_groups
    if warnings:
        payload["warnings"] = warnings
    return payload


@dataclass(frozen=True, slots=True)
class _ResolveContext:
    workspace_slug: str | None
    path_vars: Mapping[str, object]
    query: Mapping[str, object]
    request_body: object
    response_body: object


@dataclass(frozen=True, slots=True)
class _ItemResolveContext:
    base: _ResolveContext
    item: object

    @property
    def workspace_slug(self) -> str | None:
        return self.base.workspace_slug

    @property
    def path_vars(self) -> Mapping[str, object]:
        return self.base.path_vars

    @property
    def query(self) -> Mapping[str, object]:
        return self.base.query

    @property
    def request_body(self) -> object:
        return self.base.request_body

    @property
    def response_body(self) -> object:
        return self.base.response_body


def _resolve_item_links(
    link: Mapping[str, object],
    *,
    routes: AgentLinkRoutes,
    context: _ResolveContext,
    item_groups: list[dict[str, object]],
    warnings: list[dict[str, str]],
) -> None:
    for index, item in enumerate(_response_items(context.response_body)):
        item_context = _ItemResolveContext(base=context, item=item)
        resolved = _resolve_one_link(link, routes=routes, context=item_context)
        if isinstance(resolved, dict):
            item_groups.append({"index": index, "links": [resolved]})
        elif isinstance(resolved, str):
            warnings.append({"rel": _link_rel(link), "reason": resolved})


def _resolve_one_link(
    link: Mapping[str, object],
    *,
    routes: AgentLinkRoutes,
    context: _ResolveContext | _ItemResolveContext,
) -> dict[str, object] | str | None:
    route_name = link.get("route")
    rel = link.get("rel")
    label = link.get("label")
    if not (
        isinstance(route_name, str)
        and route_name
        and isinstance(rel, str)
        and rel
        and isinstance(label, str)
        and label
    ):
        return None
    route = routes.get(route_name)
    if route is None:
        return f"route {route_name!r} is not agent-linkable"

    raw_params = _as_mapping(link.get("params")) or {}
    raw_query = _as_mapping(link.get("query")) or {}
    params: dict[str, str] = {}
    for name in route.params:
        if name not in raw_params:
            return f"route {route_name!r} missing binding for {name!r}"
        value = _resolve_value(raw_params[name], context)
        if value is None:
            return f"route {route_name!r} could not resolve {name!r}"
        params[name] = str(value)

    query: dict[str, str] = {}
    for name, binding in raw_query.items():
        if name not in route.query:
            return f"route {route_name!r} does not allow query key {name!r}"
        value = _resolve_value(binding, context)
        if value is not None:
            query[name] = str(value)

    href = _href(
        route, params=params, query=query, workspace_slug=context.workspace_slug
    )
    if href is None:
        return f"route {route_name!r} could not produce a safe href"
    return {"rel": rel, "label": label, "route": route_name, "href": href}


def _href(
    route: RouteDefinition,
    *,
    params: Mapping[str, str],
    query: Mapping[str, str],
    workspace_slug: str | None,
) -> str | None:
    path = route.template
    for name, value in params.items():
        path = path.replace(f":{name}", urllib.parse.quote(value, safe=""))
    if ":" in path:
        return None
    if route.scope == "workspace":
        if not workspace_slug:
            return None
        path = f"/w/{urllib.parse.quote(workspace_slug, safe='')}{path}"
    if not path.startswith("/") or path.startswith("//"):
        return None
    if path == "/api" or path.startswith("/api/") or "/api/" in path:
        return None
    if query:
        path = f"{path}?{urllib.parse.urlencode(sorted(query.items()))}"
    return path


def _resolve_value(
    binding: object,
    context: _ResolveContext | _ItemResolveContext,
) -> object | None:
    if not isinstance(binding, str) or not binding.startswith("$"):
        return binding
    source, _, tail = binding[1:].partition(".")
    if source == "resolved" and tail == "workspace_slug":
        return context.workspace_slug
    root: object
    if source == "response":
        root = context.response_body
    elif source == "request":
        root = context.request_body
    elif source == "path":
        root = context.path_vars
    elif source == "query":
        root = context.query
    elif source == "item" and isinstance(context, _ItemResolveContext):
        root = context.item
    else:
        return None
    return _get_path(root, tail)


def _get_path(root: object, dotted: str) -> object | None:
    current = root
    if not dotted:
        return current
    for part in dotted.split("."):
        mapping = _as_mapping(current)
        if mapping is None or part not in mapping:
            return None
        current = mapping[part]
    return current


def _response_items(response_body: object) -> Sequence[object]:
    if isinstance(response_body, list):
        return response_body
    mapping = _as_mapping(response_body)
    if mapping is None:
        return ()
    data = mapping.get("data")
    if isinstance(data, list):
        return data
    return ()


def _uses_item_binding(link: Mapping[str, object]) -> bool:
    return _mapping_uses_item(link.get("params")) or _mapping_uses_item(
        link.get("query")
    )


def _mapping_uses_item(value: object) -> bool:
    mapping = _as_mapping(value)
    if mapping is None:
        return False
    return any(
        isinstance(item, str) and item.startswith("$item.") for item in mapping.values()
    )


def _link_rel(link: Mapping[str, object]) -> str:
    rel = link.get("rel")
    return rel if isinstance(rel, str) and rel else "unknown"


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _field_names(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    names: list[str] = []
    for item in raw:
        mapping = _as_mapping(item)
        if mapping is None:
            continue
        name = mapping.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)
