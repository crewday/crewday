"""HTTP round-trip tests for LLM agent preference routes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.llm.models import AgentPreference, BudgetLedger
from app.api.v1 import llm as llm_module
from app.api.v1.llm import build_workspace_llm_router
from app.api.v1.llm import router as llm_router
from app.events.registry import Event
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.unit.api.v1.identity.conftest import build_client


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", llm_router)], factory, ctx)


def test_visible_llm_routes_have_cli_and_agent_annotations() -> None:
    app = FastAPI()
    app.include_router(llm_router)

    operations = {
        (method.upper(), path): operation
        for path, methods in app.openapi()["paths"].items()
        for method, operation in methods.items()
    }

    visible_routes = {
        ("GET", "/agent_preferences/workspace"),
        ("PUT", "/agent_preferences/workspace"),
        ("GET", "/agent_preferences/workspace/upstream_pii_consent"),
        ("PUT", "/agent_preferences/workspace/upstream_pii_consent"),
        ("GET", "/agent_preferences/me"),
        ("PUT", "/agent_preferences/me"),
        ("GET", "/me/agent_approval_mode"),
        ("PUT", "/me/agent_approval_mode"),
        ("GET", "/workspace/usage"),
    }
    for key in visible_routes:
        assert operations[key]["x-cli"]["summary"]

    mutating_routes = {
        ("PUT", "/agent_preferences/workspace"),
        ("PUT", "/agent_preferences/workspace/upstream_pii_consent"),
        ("PUT", "/agent_preferences/me"),
        ("PUT", "/me/agent_approval_mode"),
    }
    for key in mutating_routes:
        assert operations[key]["x-cli"]["mutates"] is True
        assert "x-agent-confirm" in operations[key]


def test_flat_llm_routes_have_cli_and_agent_annotations() -> None:
    app = FastAPI()
    app.include_router(build_workspace_llm_router())

    operations = {
        (method.upper(), path): operation
        for path, methods in app.openapi()["paths"].items()
        for method, operation in methods.items()
    }

    visible_routes = {
        ("GET", "/agent_preferences/workspace"),
        ("PUT", "/agent_preferences/workspace"),
        ("GET", "/agent_preferences/workspace/upstream_pii_consent"),
        ("PUT", "/agent_preferences/workspace/upstream_pii_consent"),
        ("GET", "/agent_preferences/me"),
        ("PUT", "/agent_preferences/me"),
        ("GET", "/me/agent_approval_mode"),
        ("PUT", "/me/agent_approval_mode"),
        ("GET", "/workspace/usage"),
    }
    for key in visible_routes:
        assert operations[key]["x-cli"]["summary"]

    mutating_routes = {
        ("PUT", "/agent_preferences/workspace"),
        ("PUT", "/agent_preferences/workspace/upstream_pii_consent"),
        ("PUT", "/agent_preferences/me"),
        ("PUT", "/me/agent_approval_mode"),
    }
    for key in mutating_routes:
        assert operations[key]["x-cli"]["mutates"] is True
        assert "x-agent-confirm" in operations[key]


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)


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


def test_workspace_agent_preferences_round_trip_via_spec_path(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    client = _client(ctx, factory)

    response = client.put(
        "/agent_preferences/workspace",
        json={
            "body_md": "Prefer terse task summaries.",
            "blocked_actions": ["payroll.issue"],
            "default_approval_mode": "auto",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope_kind"] == "workspace"
    assert body["scope_id"] == workspace_id
    assert body["body_md"] == "Prefer terse task summaries."

    readback = client.get("/agent_preferences/workspace")
    assert readback.status_code == 200
    assert readback.json() == body


def test_workspace_upstream_pii_consent_default_empty(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    client = _client(ctx, factory)

    response = client.get("/agent_preferences/workspace/upstream_pii_consent")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "upstream_pii_consent": [],
        "available_tokens": ["legal_name", "email", "phone", "address"],
    }


def test_workspace_upstream_pii_consent_writes_valid_tokens_and_creates_row(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    client = _client(ctx, factory)

    response = client.put(
        "/agent_preferences/workspace/upstream_pii_consent",
        json={"upstream_pii_consent": ["email", "legal_name"]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["upstream_pii_consent"] == ["legal_name", "email"]
    with factory() as session:
        row = session.scalar(
            select(AgentPreference).where(
                AgentPreference.workspace_id == workspace_id,
                AgentPreference.scope_kind == "workspace",
                AgentPreference.scope_id == workspace_id,
            )
        )
        assert row is not None
        assert row.upstream_pii_consent == ["legal_name", "email"]


def test_workspace_upstream_pii_consent_rejects_invalid_token(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    client = _client(ctx, factory)

    response = client.put(
        "/agent_preferences/workspace/upstream_pii_consent",
        json={"upstream_pii_consent": ["email", "ssn"]},
    )

    assert response.status_code == 422


def test_workspace_upstream_pii_consent_rejects_missing_token_list(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    client = _client(ctx, factory)
    seeded = client.put(
        "/agent_preferences/workspace/upstream_pii_consent",
        json={"upstream_pii_consent": ["email"]},
    )
    assert seeded.status_code == 200, seeded.text

    response = client.put(
        "/agent_preferences/workspace/upstream_pii_consent",
        json={},
    )

    assert response.status_code == 422
    with factory() as session:
        row = session.scalar(
            select(AgentPreference).where(
                AgentPreference.workspace_id == workspace_id,
                AgentPreference.scope_kind == "workspace",
                AgentPreference.scope_id == workspace_id,
            )
        )
        assert row is not None
        assert row.upstream_pii_consent == ["email"]
        audits = session.scalars(
            select(AuditLog)
            .where(AuditLog.workspace_id == workspace_id)
            .where(AuditLog.action == "agent_preference.upstream_pii_consent.updated")
        ).all()
    assert len(audits) == 1


def test_workspace_upstream_pii_consent_audits_only_effective_changes(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    client = _client(ctx, factory)

    first = client.put(
        "/agent_preferences/workspace/upstream_pii_consent",
        json={"upstream_pii_consent": ["phone"]},
    )
    second = client.put(
        "/agent_preferences/workspace/upstream_pii_consent",
        json={"upstream_pii_consent": ["phone"]},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    with factory() as session:
        rows = session.scalars(
            select(AuditLog)
            .where(AuditLog.workspace_id == workspace_id)
            .where(AuditLog.action == "agent_preference.upstream_pii_consent.updated")
        ).all()
    assert len(rows) == 1
    assert rows[0].entity_kind == "agent_preference"
    assert rows[0].diff == {
        "before": {"upstream_pii_consent": []},
        "after": {"upstream_pii_consent": ["phone"]},
    }


def test_workspace_upstream_pii_consent_empty_noop_creates_no_audit(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    client = _client(ctx, factory)

    response = client.put(
        "/agent_preferences/workspace/upstream_pii_consent",
        json={"upstream_pii_consent": []},
    )

    assert response.status_code == 200, response.text
    with factory() as session:
        pref = session.scalar(
            select(AgentPreference).where(
                AgentPreference.workspace_id == workspace_id,
                AgentPreference.scope_kind == "workspace",
                AgentPreference.scope_id == workspace_id,
            )
        )
        assert pref is not None
        assert pref.upstream_pii_consent == []
        audits = session.scalars(
            select(AuditLog).where(
                AuditLog.action == "agent_preference.upstream_pii_consent.updated"
            )
        ).all()
    assert audits == []


def test_workspace_upstream_pii_consent_denies_worker(
    worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
) -> None:
    ctx, factory, _workspace_id, _worker_id = worker_ctx
    client = _client(ctx, factory)

    read = client.get("/agent_preferences/workspace/upstream_pii_consent")
    write = client.put(
        "/agent_preferences/workspace/upstream_pii_consent",
        json={"upstream_pii_consent": ["email"]},
    )

    assert read.status_code == 403
    assert write.status_code == 403


def test_workspace_upstream_pii_consent_denies_manager_who_is_not_owner(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    non_owner_manager = replace(ctx, actor_was_owner_member=False)
    client = _client(non_owner_manager, factory)

    read = client.get("/agent_preferences/workspace/upstream_pii_consent")
    write = client.put(
        "/agent_preferences/workspace/upstream_pii_consent",
        json={"upstream_pii_consent": ["email"]},
    )

    assert read.status_code == 403
    assert write.status_code == 403


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


def test_self_agent_preferences_round_trip_via_spec_path(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    client = _client(ctx, factory)

    response = client.put(
        "/agent_preferences/me",
        json={"body_md": "Ask before moving calendar events."},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope_kind"] == "user"
    assert body["scope_id"] == ctx.actor_id
    assert body["body_md"] == "Ask before moving calendar events."

    readback = client.get("/agent_preferences/me")
    assert readback.status_code == 200
    assert readback.json() == body


def test_self_agent_preference_update_publishes_user_scoped_event(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    bus = _CapturingBus()
    monkeypatch.setattr(llm_module, "default_event_bus", bus)
    client = _client(ctx, factory)

    response = client.put(
        "/agent_preferences/me",
        json={"body_md": "Use direct bullets."},
    )

    assert response.status_code == 200, response.text
    assert [event.name for event in bus.events] == ["agent.settings.changed"]
    assert bus.events[0].actor_user_id == ctx.actor_id
    assert bus.events[0].changed_keys == ("agent_preferences.me",)


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
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error"] == "preference_contains_secret"


def test_agent_preferences_reject_oversized_body(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    client = _client(ctx, factory)

    response = client.put(
        "/workspace/agent_prefs",
        json={"body_md": "x" * 64_004},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error"] == "preference_too_large"


def test_my_agent_approval_mode_round_trip(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    client = _client(ctx, factory)

    initial = client.get("/me/agent_approval_mode")
    assert initial.status_code == 200
    assert initial.json() == {"mode": "strict"}

    response = client.put("/me/agent_approval_mode", json={"mode": "auto"})
    assert response.status_code == 200, response.text
    assert response.json() == {"mode": "auto"}

    readback = client.get("/me/agent_approval_mode")
    assert readback.status_code == 200
    assert readback.json() == {"mode": "auto"}


def test_my_agent_approval_mode_missing_user_returns_problem_json(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    client = _client(replace(ctx, actor_id="missing_user"), factory)

    response = client.get("/me/agent_approval_mode")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error"] == "user_not_found"


def test_my_agent_approval_mode_update_publishes_user_scoped_event(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    bus = _CapturingBus()
    monkeypatch.setattr(llm_module, "default_event_bus", bus)
    client = _client(ctx, factory)

    response = client.put("/me/agent_approval_mode", json={"mode": "auto"})

    assert response.status_code == 200, response.text
    assert [event.name for event in bus.events] == ["agent.settings.changed"]
    assert bus.events[0].actor_user_id == ctx.actor_id
    assert bus.events[0].changed_keys == ("agent_approval_mode",)


def test_workspace_usage_reads_budget_ledger(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    with factory() as s:
        s.add(
            BudgetLedger(
                id=new_ulid(),
                workspace_id=workspace_id,
                period_start=now - timedelta(days=30),
                period_end=now,
                spent_cents=320,
                cap_cents=1000,
                updated_at=now,
            )
        )
        s.commit()
    client = _client(ctx, factory)

    response = client.get("/workspace/usage")

    assert response.status_code == 200
    assert response.json() == {
        "percent": 32,
        "paused": False,
        "window_label": "Rolling 30 days",
    }
