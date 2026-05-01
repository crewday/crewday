"""Unit tests for :mod:`app.agent.dispatcher` (cd-z3b7).

The dispatcher is exercised here against a synthetic mini-FastAPI
app + a hand-rolled OpenAPI-shaped dict; the integration suite
(``tests/integration/test_agent_dispatcher.py``) covers the live
production schema. Keeping the unit tier free of the full app
factory makes each branch a one-liner.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, Header, HTTPException, Query

from app.agent.dispatcher import OpenAPIToolDispatcher
from app.domain.agent.runtime import DelegatedToken, ToolCall


def _build_app() -> FastAPI:
    """Return a tiny FastAPI app exposing the operations under test."""
    app = FastAPI()
    router = APIRouter()

    @router.get("/w/{slug}/api/v1/echo")
    def echo(slug: str, q: str | None = Query(default=None)) -> dict[str, Any]:
        return {"slug": slug, "q": q}

    @router.post("/w/{slug}/api/v1/things")
    def create_thing(slug: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"slug": slug, "received": body, "id": "thing_001"}

    @router.delete("/w/{slug}/api/v1/things/{thing_id}")
    def delete_thing(slug: str, thing_id: str) -> dict[str, Any]:
        if thing_id == "missing":
            raise HTTPException(status_code=404, detail="not found")
        return {"slug": slug, "deleted": thing_id}

    @router.get("/w/{slug}/api/v1/echo_headers")
    def echo_headers(
        slug: str,
        authorization: str | None = Header(default=None),
        x_agent_channel: str | None = Header(default=None),
        x_agent_reason: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return {
            "slug": slug,
            "authorization": authorization,
            "x_agent_channel": x_agent_channel,
            "x_agent_reason": x_agent_reason,
        }

    @router.post("/w/{slug}/api/v1/echo_post_headers")
    def echo_post_headers(
        slug: str,
        body: dict[str, Any],
        content_type: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return {
            "slug": slug,
            "content_type": content_type,
            "authorization": authorization,
            "body": body,
        }

    app.include_router(router)
    return app


def _schema_with(*entries: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal OpenAPI-shaped schema from synthetic entries.

    Each entry: ``{"path": ..., "method": ..., "operationId": ...,
    "annotation": <x-agent-confirm value or None>}``.
    """
    paths: dict[str, dict[str, Any]] = {}
    for entry in entries:
        op: dict[str, Any] = {"operationId": entry["operationId"]}
        if entry.get("annotation") is not None:
            op["x-agent-confirm"] = entry["annotation"]
        if entry.get("parameters") is not None:
            op["parameters"] = entry["parameters"]
        paths.setdefault(entry["path"], {})[entry["method"]] = op
    return {"openapi": "3.1.0", "paths": paths}


def _token() -> DelegatedToken:
    return DelegatedToken(plaintext="mip_FAKEKEY_FAKESECRET", token_id="tok_001")


# ---------------------------------------------------------------------------
# Index construction
# ---------------------------------------------------------------------------


def test_duplicate_operation_id_raises_at_construction() -> None:
    schema = _schema_with(
        {"path": "/a", "method": "get", "operationId": "shared.op"},
        {"path": "/b", "method": "get", "operationId": "shared.op"},
    )
    with pytest.raises(ValueError, match="duplicate operationId"):
        OpenAPIToolDispatcher(
            app=FastAPI(),
            openapi=schema,
            workspace_slug="ws",
        )


def test_index_skips_non_operation_keys() -> None:
    """``parameters`` / ``summary`` etc on a path object aren't operations."""
    schema = {
        "paths": {
            "/x": {
                "summary": "A path",
                "parameters": [{"name": "common", "in": "query"}],
                "get": {"operationId": "x.read"},
            }
        }
    }
    disp = OpenAPIToolDispatcher(app=FastAPI(), openapi=schema, workspace_slug="ws")
    assert disp.operation_ids == frozenset({"x.read"})


# ---------------------------------------------------------------------------
# is_gated branches
# ---------------------------------------------------------------------------


def _disp_for_gating(
    annotation: Any = None,
    *,
    always_gated: frozenset[str] = frozenset(),
) -> OpenAPIToolDispatcher:
    schema = _schema_with(
        {
            "path": "/items",
            "method": "post",
            "operationId": "items.create",
            "annotation": annotation,
        }
    )
    return OpenAPIToolDispatcher(
        app=FastAPI(),
        openapi=schema,
        workspace_slug="ws",
        always_gated_tools=always_gated,
    )


def test_is_gated_unknown_tool_returns_not_gated() -> None:
    disp = _disp_for_gating()
    decision = disp.is_gated(ToolCall(id="c1", name="not.a.tool", input={}))
    assert decision.gated is False


def test_is_gated_no_annotation_returns_not_gated() -> None:
    disp = _disp_for_gating()
    decision = disp.is_gated(ToolCall(id="c1", name="items.create", input={}))
    assert decision.gated is False


def test_is_gated_annotation_object_uses_summary_field() -> None:
    disp = _disp_for_gating({"summary": "Create item {name}?", "risk": "high"})
    decision = disp.is_gated(ToolCall(id="c1", name="items.create", input={}))
    assert decision.gated is True
    assert decision.card_summary == "Create item {name}?"
    assert decision.card_risk == "high"
    assert decision.pre_approval_source == "annotation"


def test_is_gated_annotation_object_accepts_message_alias() -> None:
    """Live codebase has both ``summary`` and ``message`` shapes; both work."""
    disp = _disp_for_gating({"message": "Upload proof?"})
    decision = disp.is_gated(ToolCall(id="c1", name="items.create", input={}))
    assert decision.gated is True
    assert decision.card_summary == "Upload proof?"
    # §12 default risk is ``medium`` when the annotation omits it.
    assert decision.card_risk == "medium"
    assert decision.pre_approval_source == "annotation"


def test_is_gated_annotation_true_uses_default_summary() -> None:
    disp = _disp_for_gating(True)
    decision = disp.is_gated(ToolCall(id="c1", name="items.create", input={}))
    assert decision.gated is True
    assert decision.card_summary == "Confirm this action."
    # §12 default risk is ``medium`` when the annotation is bare ``True``.
    assert decision.card_risk == "medium"
    assert decision.pre_approval_source == "annotation"


def test_is_gated_workspace_policy_beats_annotation() -> None:
    disp = _disp_for_gating(
        {"summary": "should be ignored", "risk": "high"},
        always_gated=frozenset({"items.create"}),
    )
    decision = disp.is_gated(ToolCall(id="c1", name="items.create", input={}))
    assert decision.gated is True
    assert decision.pre_approval_source == "workspace_policy"
    assert decision.card_risk == "medium"
    assert "items.create" in decision.card_summary


# ---------------------------------------------------------------------------
# dispatch — round-trips against a live mini-FastAPI app
# ---------------------------------------------------------------------------


def test_dispatch_unknown_tool_returns_404() -> None:
    disp = _disp_for_gating()
    result = disp.dispatch(
        ToolCall(id="c1", name="nope", input={}),
        token=_token(),
        headers={},
    )
    assert result.status_code == 404
    assert result.body == {"detail": "tool not found"}
    assert result.mutated is False


def test_dispatch_get_routes_query_params_through() -> None:
    app = _build_app()
    schema = app.openapi()
    echo_op = next(
        op["operationId"]
        for path, methods in schema["paths"].items()
        for method, op in methods.items()
        if method == "get" and "echo" in path
    )
    disp = OpenAPIToolDispatcher(
        app=app,
        openapi=schema,
        workspace_slug="ws-test",
    )
    result = disp.dispatch(
        ToolCall(id="c1", name=echo_op, input={"q": "hello"}),
        token=_token(),
        headers={},
    )
    assert result.status_code == 200
    assert result.body == {"slug": "ws-test", "q": "hello"}
    assert result.mutated is False


def test_dispatch_post_serialises_body_and_marks_mutated() -> None:
    app = _build_app()
    schema = app.openapi()
    # Find the create operation id FastAPI assigned.
    create_op = next(
        op["operationId"]
        for path, methods in schema["paths"].items()
        for method, op in methods.items()
        if method == "post" and "things" in path
    )
    disp = OpenAPIToolDispatcher(app=app, openapi=schema, workspace_slug="ws-test")
    result = disp.dispatch(
        ToolCall(id="c1", name=create_op, input={"name": "widget"}),
        token=_token(),
        headers={"X-Agent-Channel": "web_owner_sidebar"},
    )
    assert result.status_code == 200
    assert result.mutated is True
    assert isinstance(result.body, dict)
    assert result.body == {
        "slug": "ws-test",
        "received": {"name": "widget"},
        "id": "thing_001",
    }


def test_dispatch_failed_write_is_not_mutated() -> None:
    app = _build_app()
    schema = app.openapi()
    delete_op = next(
        op["operationId"]
        for path, methods in schema["paths"].items()
        for method, op in methods.items()
        if method == "delete"
    )
    disp = OpenAPIToolDispatcher(app=app, openapi=schema, workspace_slug="ws-test")
    result = disp.dispatch(
        ToolCall(id="c1", name=delete_op, input={"thing_id": "missing"}),
        token=_token(),
        headers={},
    )
    assert result.status_code == 404
    assert result.mutated is False


def test_dispatch_missing_path_var_returns_422() -> None:
    """A required path variable that isn't provided is a 422."""
    schema = _schema_with(
        {
            "path": "/things/{thing_id}",
            "method": "delete",
            "operationId": "things.delete",
        }
    )
    disp = OpenAPIToolDispatcher(
        app=FastAPI(), openapi=schema, workspace_slug="ws-test"
    )
    result = disp.dispatch(
        ToolCall(id="c1", name="things.delete", input={}),
        token=_token(),
        headers={},
    )
    assert result.status_code == 422
    assert isinstance(result.body, dict)
    assert "thing_id" in str(result.body.get("detail", ""))
    assert result.mutated is False


def test_dispatch_authorization_header_overrides_caller() -> None:
    """The dispatcher always uses the delegated token Bearer, never the caller's.

    The route under test echoes the headers it received back to the
    body so the assertion can read off the wire-level value rather
    than infer success from a 200.
    """
    app = _build_app()
    schema = app.openapi()
    echo_headers_op = next(
        op["operationId"]
        for path, methods in schema["paths"].items()
        for method, op in methods.items()
        if method == "get" and "echo_headers" in path
    )
    disp = OpenAPIToolDispatcher(app=app, openapi=schema, workspace_slug="ws-test")
    result = disp.dispatch(
        ToolCall(id="c1", name=echo_headers_op, input={}),
        token=_token(),
        headers={
            "Authorization": "Bearer SHOULD_BE_OVERRIDDEN",
            "X-Agent-Channel": "web_owner_sidebar",
            "X-Agent-Reason": "test.override",
        },
    )
    assert result.status_code == 200
    assert isinstance(result.body, dict)
    # Authorization must be the delegated token's Bearer, never the caller's.
    assert result.body["authorization"] == "Bearer mip_FAKEKEY_FAKESECRET"
    # X-Agent-* headers pass through unchanged.
    assert result.body["x_agent_channel"] == "web_owner_sidebar"
    assert result.body["x_agent_reason"] == "test.override"


def test_dispatch_caller_content_type_collapses_case_insensitively() -> None:
    """A caller's lowercase ``content-type`` should not duplicate our preset.

    The dispatcher pre-fills ``Content-Type: application/json`` on
    body-bearing methods. A caller passing ``content-type`` in a
    different casing should land on the same dict slot, not a second
    one. The route under test echoes the resolved ``Content-Type``
    header back to the body so the assertion can read off the
    wire-level value the caller's variant won.
    """
    app = _build_app()
    schema = app.openapi()
    echo_post_op = next(
        op["operationId"]
        for path, methods in schema["paths"].items()
        for method, op in methods.items()
        if method == "post" and "echo_post_headers" in path
    )
    disp = OpenAPIToolDispatcher(app=app, openapi=schema, workspace_slug="ws-test")
    result = disp.dispatch(
        ToolCall(id="c1", name=echo_post_op, input={"name": "widget"}),
        token=_token(),
        headers={"content-type": "application/json; charset=utf-8"},
    )
    assert result.status_code == 200
    assert isinstance(result.body, dict)
    # The caller's casing wins onto the same Content-Type slot.
    assert result.body["content_type"] == "application/json; charset=utf-8"
    # And Authorization is still owned by the dispatcher.
    assert result.body["authorization"] == "Bearer mip_FAKEKEY_FAKESECRET"
