"""HTTP round-trip tests for LLM agent preference routes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.llm.models import AgentPreference, BudgetLedger
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.api.v1 import llm as llm_module
from app.api.v1.llm import build_workspace_llm_router
from app.api.v1.llm import router as llm_router
from app.events.registry import Event
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client, ctx_for, seed_worker_user


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", llm_router)], factory, ctx)


def _flat_client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client(
        [("/w/{slug}/api/v1", build_workspace_llm_router())], factory, ctx
    )


def _seed_property(factory: sessionmaker[Session], *, workspace_id: str) -> str:
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    property_id = new_ulid()
    with factory() as session:
        session.add(
            Property(
                id=property_id,
                name="Villa Sud",
                kind="residence",
                address="1 Test Lane",
                address_json={
                    "line1": "1 Test Lane",
                    "line2": None,
                    "city": "Nice",
                    "state_province": None,
                    "postal_code": None,
                    "country": "FR",
                },
                country="FR",
                locale=None,
                default_currency=None,
                timezone="Europe/Paris",
                lat=None,
                lon=None,
                client_org_id=None,
                owner_user_id=None,
                tags_json=[],
                welcome_defaults_json={},
                property_notes_md="",
                created_at=now,
                updated_at=now,
                deleted_at=None,
            )
        )
        session.flush()
        session.add(
            PropertyWorkspace(
                property_id=property_id,
                workspace_id=workspace_id,
                label="Villa Sud",
                membership_role="owner_workspace",
                status="active",
                created_at=now,
            )
        )
        session.commit()
    return property_id


def _property_pinned_worker_ctx(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    workspace_slug: str,
    property_id: str,
) -> WorkspaceContext:
    with factory() as session:
        worker_id = seed_worker_user(
            session,
            workspace_id=workspace_id,
            email="pinned-worker@example.com",
            display_name="Pinned Worker",
        )
        with tenant_agnostic():
            session.execute(
                delete(RoleGrant).where(
                    RoleGrant.workspace_id == workspace_id,
                    RoleGrant.user_id == worker_id,
                )
            )
            session.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=workspace_id,
                    user_id=worker_id,
                    grant_role="worker",
                    scope_property_id=property_id,
                    created_at=datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC),
                    created_by_user_id=None,
                )
            )
        session.commit()
    return ctx_for(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=worker_id,
        grant_role="worker",
        actor_was_owner_member=False,
    )


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
        ("GET", "/agent_preferences/property/{property_id}"),
        ("PUT", "/agent_preferences/property/{property_id}"),
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
        ("PUT", "/agent_preferences/property/{property_id}"),
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
        ("GET", "/agent_preferences/property/{property_id}"),
        ("PUT", "/agent_preferences/property/{property_id}"),
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
        ("PUT", "/agent_preferences/property/{property_id}"),
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


def test_property_agent_preferences_empty_read_and_round_trip(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    property_id = _seed_property(factory, workspace_id=workspace_id)
    client = _client(ctx, factory)

    empty = client.get(f"/agent_preferences/property/{property_id}")

    assert empty.status_code == 200, empty.text
    assert empty.json() == {
        "scope_kind": "property",
        "scope_id": property_id,
        "body_md": "",
        "token_count": 0,
        "updated_by_user_id": None,
        "updated_at": None,
        "writable": True,
        "soft_cap": 8000,
        "hard_cap": 16000,
        "blocked_actions": [],
        "default_approval_mode": "auto",
    }

    response = client.put(
        f"/agent_preferences/property/{property_id}",
        json={
            "body_md": "Use the side gate for vendor arrivals.",
            "blocked_actions": ["tasks.cancel"],
            "default_approval_mode": "strict",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope_kind"] == "property"
    assert body["scope_id"] == property_id
    assert body["body_md"] == "Use the side gate for vendor arrivals."
    assert body["blocked_actions"] == ["tasks.cancel"]
    assert body["default_approval_mode"] == "strict"
    readback = client.get(f"/agent_preferences/property/{property_id}")
    assert readback.status_code == 200
    assert readback.json() == body
    with factory() as session:
        row = session.scalar(
            select(AgentPreference).where(
                AgentPreference.workspace_id == workspace_id,
                AgentPreference.scope_kind == "property",
                AgentPreference.scope_id == property_id,
            )
        )
    assert row is not None
    assert row.body_md == "Use the side gate for vendor arrivals."


def test_property_agent_preferences_flat_workspace_route_registered(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    property_id = _seed_property(factory, workspace_id=workspace_id)
    client = _flat_client(ctx, factory)

    response = client.get(
        f"/w/{ctx.workspace_slug}/api/v1/agent_preferences/property/{property_id}"
    )

    assert response.status_code == 200, response.text
    assert response.json()["scope_kind"] == "property"
    assert response.json()["scope_id"] == property_id


def test_property_agent_preferences_worker_can_read_visible_property_but_not_write(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    owner, factory, workspace_id = owner_ctx
    property_id = _seed_property(factory, workspace_id=workspace_id)
    worker = _property_pinned_worker_ctx(
        factory,
        workspace_id=workspace_id,
        workspace_slug=owner.workspace_slug,
        property_id=property_id,
    )
    owner_client = _client(owner, factory)
    seeded = owner_client.put(
        f"/agent_preferences/property/{property_id}",
        json={"body_md": "Keep arrivals quiet after 22:00."},
    )
    assert seeded.status_code == 200, seeded.text
    client = _client(worker, factory)

    read = client.get(f"/agent_preferences/property/{property_id}")
    write = client.put(
        f"/agent_preferences/property/{property_id}",
        json={"body_md": "Try to overwrite."},
    )

    assert read.status_code == 200, read.text
    assert read.json()["body_md"] == "Keep arrivals quiet after 22:00."
    assert read.json()["writable"] is False
    assert write.status_code == 403


def test_property_agent_preferences_deny_read_without_property_access(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    owner, factory, workspace_id = owner_ctx
    visible_property_id = _seed_property(factory, workspace_id=workspace_id)
    hidden_property_id = _seed_property(factory, workspace_id=workspace_id)
    worker = _property_pinned_worker_ctx(
        factory,
        workspace_id=workspace_id,
        workspace_slug=owner.workspace_slug,
        property_id=visible_property_id,
    )
    client = _client(worker, factory)

    response = client.get(f"/agent_preferences/property/{hidden_property_id}")

    assert response.status_code == 403


def test_property_agent_preferences_reject_cross_workspace_property_id(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    with factory() as session:
        other_owner = bootstrap_user(
            session,
            email="other-owner@example.com",
            display_name="Other Owner",
        )
        other_workspace = bootstrap_workspace(
            session,
            slug="other-agent-prefs",
            name="Other Agent Prefs",
            owner_user_id=other_owner.id,
        )
        session.commit()
        other_workspace_id = other_workspace.id
    other_property_id = _seed_property(factory, workspace_id=other_workspace_id)
    other_ctx = ctx_for(
        workspace_id=other_workspace_id,
        workspace_slug="other-agent-prefs",
        actor_id=other_owner.id,
        grant_role="manager",
        actor_was_owner_member=True,
    )
    other_client = _client(other_ctx, factory)
    seeded = other_client.put(
        f"/agent_preferences/property/{other_property_id}",
        json={"body_md": "Sibling workspace only."},
    )
    assert seeded.status_code == 200, seeded.text
    client = _client(ctx, factory)

    read = client.get(f"/agent_preferences/property/{other_property_id}")
    write = client.put(
        f"/agent_preferences/property/{other_property_id}",
        json={"body_md": "Do not create a shadow row."},
    )

    assert read.status_code == 403
    assert write.status_code == 403
    with factory() as session:
        leaked = session.scalar(
            select(AgentPreference).where(
                AgentPreference.workspace_id == workspace_id,
                AgentPreference.scope_kind == "property",
                AgentPreference.scope_id == other_property_id,
            )
        )
    assert leaked is None


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"body_md": "wifi password: swordfish"}, "preference_contains_secret"),
        ({"body_md": "x" * 64_004}, "preference_too_large"),
    ],
)
def test_property_agent_preferences_reject_invalid_save_body(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    payload: dict[str, str],
    error: str,
) -> None:
    ctx, factory, workspace_id = owner_ctx
    property_id = _seed_property(factory, workspace_id=workspace_id)
    client = _client(ctx, factory)

    response = client.put(f"/agent_preferences/property/{property_id}", json=payload)

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error"] == error


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
