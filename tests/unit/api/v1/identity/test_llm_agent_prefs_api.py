"""HTTP round-trip tests for LLM agent preference routes."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.v1.llm import router as llm_router
from app.tenancy import WorkspaceContext
from tests.unit.api.v1.identity.conftest import build_client


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", llm_router)], factory, ctx)


def test_workspace_agent_preferences_round_trip_via_api(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    client = _client(ctx, factory)

    response = client.put(
        "/workspace/agent_prefs",
        json={
            "body_md": "Keep owner replies formal.",
            "blocked_actions": ["tasks.cancel"],
            "default_approval_mode": "strict",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope_kind"] == "workspace"
    assert body["scope_id"] == workspace_id
    assert body["body_md"] == "Keep owner replies formal."
    assert body["blocked_actions"] == ["tasks.cancel"]
    assert body["default_approval_mode"] == "strict"

    readback = client.get("/workspace/agent_prefs")
    assert readback.status_code == 200
    assert readback.json() == body


def test_self_agent_preferences_round_trip_via_api(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    client = _client(ctx, factory)

    response = client.put(
        "/users/me/agent_prefs",
        json={"body_md": "One paragraph maximum."},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope_kind"] == "user"
    assert body["scope_id"] == ctx.actor_id
    assert body["body_md"] == "One paragraph maximum."

    readback = client.get("/users/me/agent_prefs")
    assert readback.status_code == 200
    assert readback.json() == body


def test_agent_preferences_reject_secret_like_body(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    client = _client(ctx, factory)

    response = client.put(
        "/workspace/agent_prefs",
        json={"body_md": "wifi password: swordfish"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "preference_contains_secret"
