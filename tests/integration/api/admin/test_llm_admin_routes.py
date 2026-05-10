"""Integration tests for deployment-admin LLM graph routes."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.identity.models import ApiToken, User
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.llm.models import (
    LlmAssignment,
    LlmCapabilityInheritance,
    LlmModel,
    LlmPromptTemplate,
    LlmPromptTemplateRevision,
    LlmProvider,
    LlmProviderModel,
    LlmUsage,
)
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.transport import admin_sse
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.domain.llm.router import DEPLOYMENT_DEFAULT_CACHE_WORKSPACE_ID
from app.events.bus import EventBus
from app.events.types import LlmAssignmentChanged
from app.main import create_app
from app.tenancy import tenant_agnostic
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

pytestmark = pytest.mark.integration


_TEST_UA = "pytest-admin-llm-integration"
_TEST_ACCEPT_LANGUAGE = "en"
_PINNED = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SeededLlm:
    workspace_id: str
    provider_id: str
    model_id: str
    provider_model_id: str
    assignment_id: str
    prompt_id: str


@pytest.fixture
def pinned_settings(db_url: str) -> Settings:
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-admin-llm-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        profile="prod",
        vite_dev_url="http://127.0.0.1:5173",
        smtp_host=None,
        smtp_from=None,
        demo_mode=False,
        demo_frame_ancestors=None,
        hsts_enabled=False,
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def real_make_uow(engine: Engine) -> Iterator[None]:
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def client(
    pinned_settings: Settings,
    real_make_uow: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setattr("app.auth.session.get_settings", lambda: pinned_settings)
    monkeypatch.setattr("app.api.admin.llm._now", lambda: _PINNED)
    app = create_app(settings=pinned_settings)
    with TestClient(
        app,
        base_url="https://testserver",
        headers={
            "User-Agent": _TEST_UA,
            "Accept-Language": _TEST_ACCEPT_LANGUAGE,
        },
        raise_server_exceptions=False,
    ) as c:
        yield c


def _seed_admin(
    session_factory: sessionmaker[Session],
    *,
    settings: Settings,
) -> str:
    with session_factory() as s:
        user = bootstrap_user(
            s, email=f"admin-{new_ulid()}@example.com", display_name="Admin"
        )
        with tenant_agnostic():
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=None,
                    user_id=user.id,
                    grant_role="manager",
                    scope_kind="deployment",
                    created_at=_PINNED,
                )
            )
            s.flush()
        result = issue(
            s,
            user_id=user.id,
            has_owner_grant=False,
            ua=_TEST_UA,
            ip="127.0.0.1",
            accept_language=_TEST_ACCEPT_LANGUAGE,
            settings=settings,
        )
        s.commit()
        return result.cookie_value


def _seed_llm_graph(session_factory: sessionmaker[Session]) -> SeededLlm:
    with session_factory() as s, tenant_agnostic():
        workspace_id = new_ulid()
        provider_id = new_ulid()
        model_id = new_ulid()
        provider_model_id = new_ulid()
        assignment_id = new_ulid()
        prompt_id = new_ulid()

        s.add(
            Workspace(
                id=workspace_id,
                slug=f"llm-{workspace_id[-6:].lower()}",
                name="LLM Smoke",
                plan="free",
                quota_json={},
                created_at=_PINNED,
            )
        )
        s.flush()
        s.add(
            LlmProvider(
                id=provider_id,
                name="OpenRouter",
                provider_type="openrouter",
                api_endpoint=None,
                api_key_envelope_ref="envelope:llm:openrouter:test",
                default_model=None,
                timeout_s=60,
                requests_per_minute=120,
                priority=0,
                is_enabled=True,
                created_at=_PINNED,
                updated_at=_PINNED,
                updated_by_user_id=None,
            )
        )
        s.add(
            LlmModel(
                id=model_id,
                canonical_name="google/gemma-3-27b-it",
                display_name="Gemma 3 27B",
                vendor="google",
                capabilities=["chat", "function_calling", "json_mode", "vision"],
                context_window=128000,
                max_output_tokens=8192,
                is_active=True,
                price_source="openrouter",
                price_source_model_id=None,
                notes=None,
                created_at=_PINNED,
                updated_at=_PINNED,
                updated_by_user_id=None,
            )
        )
        s.flush()
        s.add(
            LlmProviderModel(
                id=provider_model_id,
                provider_id=provider_id,
                model_id=model_id,
                api_model_id="google/gemma-3-27b-it",
                input_cost_per_million=Decimal("0.1000"),
                output_cost_per_million=Decimal("0.2000"),
                fixed_cost_per_call_usd=None,
                max_tokens_override=None,
                temperature_override=None,
                supports_system_prompt=True,
                supports_temperature=True,
                reasoning_effort="",
                extra_api_params={},
                price_source_override="",
                price_source_model_id_override=None,
                price_last_synced_at=None,
                is_enabled=True,
                created_at=_PINNED,
                updated_at=_PINNED,
            )
        )
        s.flush()
        s.add(
            LlmAssignment(
                id=assignment_id,
                workspace_id=None,
                capability="chat.manager",
                model_id=provider_model_id,
                provider="OpenRouter",
                priority=0,
                enabled=True,
                max_tokens=2048,
                temperature=0.2,
                extra_api_params={"top_p": 0.9},
                required_capabilities=["chat", "function_calling"],
                created_at=_PINNED,
            )
        )
        s.add(
            LlmCapabilityInheritance(
                id=new_ulid(),
                workspace_id=None,
                capability="chat.admin",
                inherits_from="chat.manager",
                created_at=_PINNED,
            )
        )
        s.add(
            LlmPromptTemplate(
                id=prompt_id,
                capability="chat.manager",
                name="Manager chat",
                template="You are the manager assistant.",
                version=1,
                is_active=True,
                default_hash="not-the-current",
                notes=None,
                created_at=_PINNED,
                updated_at=_PINNED,
            )
        )
        s.add(
            LlmUsage(
                id=new_ulid(),
                workspace_id=workspace_id,
                capability="chat.manager",
                provider_model_id=provider_model_id,
                tokens_in=100,
                tokens_out=40,
                cost_cents=17,
                cost_usd=Decimal("0.174200"),
                latency_ms=250,
                status="ok",
                correlation_id=new_ulid(),
                attempt=0,
                assignment_id=assignment_id,
                fallback_attempts=0,
                finish_reason="stop",
                actor_user_id=None,
                token_id=None,
                agent_label=None,
                created_at=_PINNED,
            )
        )
        s.add(
            LlmUsage(
                id=new_ulid(),
                workspace_id=workspace_id,
                capability="chat.admin",
                provider_model_id=provider_model_id,
                tokens_in=10,
                tokens_out=4,
                cost_cents=0,
                cost_usd=Decimal("0.000400"),
                latency_ms=180,
                status="ok",
                correlation_id=new_ulid(),
                attempt=0,
                assignment_id=assignment_id,
                fallback_attempts=0,
                finish_reason="stop",
                actor_user_id=None,
                token_id=None,
                agent_label=None,
                created_at=_PINNED - timedelta(minutes=1),
            )
        )
        s.add(
            LlmUsage(
                id=new_ulid(),
                workspace_id=workspace_id,
                capability="chat.manager",
                provider_model_id=provider_model_id,
                tokens_in=1,
                tokens_out=1,
                cost_cents=0,
                cost_usd=Decimal("0.000006"),
                latency_ms=120,
                status="ok",
                correlation_id=new_ulid(),
                attempt=0,
                assignment_id=assignment_id,
                fallback_attempts=0,
                finish_reason="stop",
                actor_user_id=None,
                token_id=None,
                agent_label=None,
                created_at=_PINNED - timedelta(days=30),
            )
        )
        s.add(
            LlmUsage(
                id=new_ulid(),
                workspace_id=workspace_id,
                capability="chat.manager",
                provider_model_id=provider_model_id,
                tokens_in=1,
                tokens_out=1,
                cost_cents=50,
                cost_usd=Decimal("0.500000"),
                latency_ms=120,
                status="ok",
                correlation_id=new_ulid(),
                attempt=0,
                assignment_id=assignment_id,
                fallback_attempts=0,
                finish_reason="stop",
                actor_user_id=None,
                token_id=None,
                agent_label=None,
                created_at=_PINNED - timedelta(days=30, microseconds=1),
            )
        )
        s.add(
            LlmUsage(
                id=new_ulid(),
                workspace_id=workspace_id,
                capability="chat.manager",
                provider_model_id="retired-provider-model",
                tokens_in=20,
                tokens_out=8,
                cost_cents=1,
                cost_usd=Decimal("0.010000"),
                latency_ms=190,
                status="ok",
                correlation_id=new_ulid(),
                attempt=0,
                assignment_id=None,
                fallback_attempts=0,
                finish_reason="stop",
                actor_user_id=None,
                token_id=None,
                agent_label=None,
                created_at=_PINNED - timedelta(minutes=2),
            )
        )
        s.commit()
        return SeededLlm(
            workspace_id=workspace_id,
            provider_id=provider_id,
            model_id=model_id,
            provider_model_id=provider_model_id,
            assignment_id=assignment_id,
            prompt_id=prompt_id,
        )


def _wipe(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s, tenant_agnostic():
        for model in (
            LlmUsage,
            LlmAssignment,
            LlmCapabilityInheritance,
            LlmPromptTemplateRevision,
            LlmPromptTemplate,
            LlmProviderModel,
            LlmModel,
            LlmProvider,
            ApiToken,
            SessionRow,
            UserWorkspace,
            RoleGrant,
            AuditLog,
            Workspace,
            User,
        ):
            for row in s.scalars(select(model)).all():
                s.delete(row)
        s.commit()


class TestAdminLlmRoutes:
    def test_graph_is_deployment_admin_gated(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        try:
            assert client.get("/admin/api/v1/llm/graph").status_code == 404

            client.cookies.set(
                SESSION_COOKIE_NAME,
                _seed_admin(session_factory, settings=pinned_settings),
            )
            resp = client.get("/admin/api/v1/llm/graph")
            assert resp.status_code == 200, resp.text
            assert set(resp.json()) == {
                "providers",
                "models",
                "provider_models",
                "capabilities",
                "inheritance",
                "assignments",
                "assignment_issues",
                "totals",
            }
        finally:
            _wipe(session_factory)

    def test_graph_calls_prompts_and_sync_pricing_shapes(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        try:
            client.cookies.set(
                SESSION_COOKIE_NAME,
                _seed_admin(session_factory, settings=pinned_settings),
            )
            seeded = _seed_llm_graph(session_factory)
            with session_factory() as s, tenant_agnostic():
                s.add(
                    LlmAssignment(
                        id="default-fallback",
                        workspace_id=None,
                        capability="default",
                        model_id=seeded.provider_model_id,
                        provider="OpenRouter",
                        priority=1,
                        enabled=True,
                        max_tokens=None,
                        temperature=None,
                        extra_api_params={},
                        required_capabilities=["chat", "function_calling"],
                        created_at=_PINNED,
                    )
                )
                s.commit()

            graph = client.get("/admin/api/v1/llm/graph")
            assert graph.status_code == 200, graph.text
            body = graph.json()
            assert body["providers"][0]["id"] == seeded.provider_id
            assert body["providers"][0]["api_key_status"] == "present"
            assert body["providers"][0]["spend_usd_30d"] == 0.174606
            assert body["providers"][0]["calls_30d"] == 3
            assert body["models"][0]["id"] == seeded.model_id
            assert body["models"][0]["spend_usd_30d"] == 0.174606
            assert body["models"][0]["calls_30d"] == 3
            assert body["provider_models"][0]["id"] == seeded.provider_model_id
            assert body["provider_models"][0]["spend_usd_30d"] == 0.174606
            assert body["provider_models"][0]["calls_30d"] == 3
            assert body["assignments"][0]["id"] == seeded.assignment_id
            assert body["assignments"][0]["spend_usd_30d"] == 0.174606
            assert body["assignments"][0]["calls_30d"] == 3
            assert body["assignments"][0]["direct_spend_usd_30d"] == 0.174206
            assert body["assignments"][0]["direct_calls_30d"] == 2
            assert body["assignments"][0]["inherited_spend_usd_30d"] == 0.0004
            assert body["assignments"][0]["inherited_calls_30d"] == 1
            chat_manager = next(
                entry
                for entry in body["capabilities"]
                if entry["key"] == "chat.manager"
            )
            assert chat_manager["spend_usd_30d"] == 0.174606
            assert chat_manager["calls_30d"] == 3
            assert chat_manager["direct_spend_usd_30d"] == 0.174206
            assert chat_manager["direct_calls_30d"] == 2
            assert chat_manager["inherited_spend_usd_30d"] == 0.0004
            assert chat_manager["inherited_calls_30d"] == 1
            chat_admin = next(
                entry for entry in body["capabilities"] if entry["key"] == "chat.admin"
            )
            assert chat_admin["spend_usd_30d"] == 0.0004
            assert chat_admin["calls_30d"] == 1
            assert chat_admin["direct_spend_usd_30d"] == 0.0004
            assert chat_admin["direct_calls_30d"] == 1
            assert chat_admin["inherited_spend_usd_30d"] == 0
            assert chat_admin["inherited_calls_30d"] == 0
            default_assignments = [
                assignment
                for assignment in body["assignments"]
                if assignment["capability"] == "default"
            ]
            assert [assignment["priority"] for assignment in default_assignments] == [
                0,
                1,
            ]
            assert default_assignments[0]["is_deployment_default"] is True
            assert {
                "capability": "tasks.nl_intake",
                "inherits_from": "default",
                "source": "implicit_default",
            } in body["inheritance"]
            assert body["totals"]["spend_usd_30d"] == 0.174606
            assert body["totals"]["calls_30d"] == 3

            calls = client.get("/admin/api/v1/llm/calls")
            assert calls.status_code == 200, calls.text
            assert calls.json()[0] == {
                "at": "2026-04-30T12:00:00Z",
                "capability": "chat.manager",
                "model_id": seeded.provider_model_id,
                "input_tokens": 100,
                "output_tokens": 40,
                "cost_usd": "0.174200",
                "cost_cents": 17,
                "latency_ms": 250,
                "status": "ok",
                "assignment_id": seeded.assignment_id,
                "provider_model_id": seeded.provider_model_id,
                "prompt_template_id": None,
                "prompt_version": None,
                "fallback_attempts": 0,
                "raw_response_available": False,
            }
            retired_call = next(
                call
                for call in calls.json()
                if call["model_id"] == "retired-provider-model"
            )
            assert retired_call["provider_model_id"] is None
            assert retired_call["cost_usd"] == "0.010000"

            prompts = client.get("/admin/api/v1/llm/prompts")
            assert prompts.status_code == 200, prompts.text
            assert prompts.json()[0]["id"] == seeded.prompt_id
            assert prompts.json()[0]["is_customised"] is True

            reset = client.post(
                f"/admin/api/v1/llm/prompts/{seeded.prompt_id}/reset-to-default"
            )
            assert reset.status_code == 200, reset.text
            reset_body = reset.json()
            assert reset_body["is_customised"] is False
            assert reset_body["revisions_count"] == 1
            assert reset_body["template"] != "You are the manager assistant."

            sync = client.post("/admin/api/v1/llm/sync-pricing")
            assert sync.status_code == 200, sync.text
            sync_body = sync.json()
            assert sync_body["updated"] == 0
            assert sync_body["errors"] == 0
            assert sync_body["deltas"][0]["status"] == "unchanged"
        finally:
            _wipe(session_factory)

    def test_assignment_create_publishes_assignment_changed(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        try:
            client.cookies.set(
                SESSION_COOKIE_NAME,
                _seed_admin(session_factory, settings=pinned_settings),
            )
            seeded = _seed_llm_graph(session_factory)
            published: list[LlmAssignmentChanged] = []
            event_bus = EventBus()
            event_bus.subscribe(LlmAssignmentChanged)(published.append)
            admin_events: list[dict[str, object]] = []
            monkeypatch.setattr("app.api.admin.llm.default_event_bus", event_bus)
            monkeypatch.setattr(
                admin_sse.default_admin_fanout,
                "publish",
                lambda **kwargs: admin_events.append(kwargs),
            )

            resp = client.post(
                "/admin/api/v1/llm/assignments",
                json={
                    "capability": "tasks.assist",
                    "provider_model_id": seeded.provider_model_id,
                    "priority": 0,
                    "is_enabled": True,
                },
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["capability"] == "tasks.assist"
            assert body["required_capabilities"] == ["chat"]
            assert [event.workspace_id for event in published] == [
                DEPLOYMENT_DEFAULT_CACHE_WORKSPACE_ID
            ]
            assert [event["kind"] for event in admin_events] == [
                "admin.llm.assignment_updated"
            ]
            assert admin_events[0]["payload"]["workspace_id"] is None
        finally:
            _wipe(session_factory)

    def test_inheritance_crud_is_deployment_level_and_publishes_refetch(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        try:
            client.cookies.set(
                SESSION_COOKIE_NAME,
                _seed_admin(session_factory, settings=pinned_settings),
            )
            _seed_llm_graph(session_factory)
            published: list[LlmAssignmentChanged] = []
            event_bus = EventBus()
            event_bus.subscribe(LlmAssignmentChanged)(published.append)
            admin_events: list[dict[str, object]] = []
            monkeypatch.setattr("app.api.admin.llm.default_event_bus", event_bus)
            monkeypatch.setattr(
                admin_sse.default_admin_fanout,
                "publish",
                lambda **kwargs: admin_events.append(kwargs),
            )

            created = client.post(
                "/admin/api/v1/llm/inheritance",
                json={
                    "capability": "tasks.assist",
                    "inherits_from": "chat.manager",
                },
            )
            assert created.status_code == 200, created.text
            assert created.json() == {
                "capability": "tasks.assist",
                "inherits_from": "chat.manager",
                "source": "explicit",
            }
            with session_factory() as s, tenant_agnostic():
                row = s.scalar(
                    select(LlmCapabilityInheritance).where(
                        LlmCapabilityInheritance.capability == "tasks.assist"
                    )
                )
                assert row is not None
                assert row.workspace_id is None

            updated = client.put(
                "/admin/api/v1/llm/inheritance/tasks.assist",
                json={"inherits_from": "default"},
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["inherits_from"] == "default"

            deleted = client.delete("/admin/api/v1/llm/inheritance/tasks.assist")
            assert deleted.status_code == 204, deleted.text
            graph = client.get("/admin/api/v1/llm/graph")
            assert graph.status_code == 200, graph.text
            assert {
                "capability": "tasks.assist",
                "inherits_from": "default",
                "source": "implicit_default",
            } in graph.json()["inheritance"]
            assert [event.workspace_id for event in published] == [
                DEPLOYMENT_DEFAULT_CACHE_WORKSPACE_ID,
                DEPLOYMENT_DEFAULT_CACHE_WORKSPACE_ID,
                DEPLOYMENT_DEFAULT_CACHE_WORKSPACE_ID,
            ]
            assert [event["kind"] for event in admin_events] == [
                "admin.llm.assignment_updated",
                "admin.llm.assignment_updated",
                "admin.llm.assignment_updated",
            ]
            assert all(
                event["payload"]["workspace_id"] is None for event in admin_events
            )
        finally:
            _wipe(session_factory)

    def test_inheritance_crud_rejects_unknown_capabilities_self_loops_and_cycles(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        try:
            client.cookies.set(
                SESSION_COOKIE_NAME,
                _seed_admin(session_factory, settings=pinned_settings),
            )
            _seed_llm_graph(session_factory)

            unknown_child = client.post(
                "/admin/api/v1/llm/inheritance",
                json={
                    "capability": "not.real",
                    "inherits_from": "default",
                },
            )
            assert unknown_child.status_code == 422, unknown_child.text
            assert unknown_child.json()["error"] == "unknown_capability"
            assert unknown_child.json()["capability"] == "not.real"

            unknown_parent = client.post(
                "/admin/api/v1/llm/inheritance",
                json={
                    "capability": "tasks.assist",
                    "inherits_from": "not.real",
                },
            )
            assert unknown_parent.status_code == 422, unknown_parent.text
            assert unknown_parent.json()["error"] == "unknown_capability"
            assert unknown_parent.json()["capability"] == "not.real"

            self_loop = client.post(
                "/admin/api/v1/llm/inheritance",
                json={
                    "capability": "tasks.assist",
                    "inherits_from": "tasks.assist",
                },
            )
            assert self_loop.status_code == 422, self_loop.text
            assert self_loop.json()["error"] == "capability_inheritance_self_loop"

            default_child = client.post(
                "/admin/api/v1/llm/inheritance",
                json={
                    "capability": "default",
                    "inherits_from": "tasks.assist",
                },
            )
            assert default_child.status_code == 422, default_child.text
            assert (
                default_child.json()["error"]
                == "default_capability_inheritance_forbidden"
            )

            cycle = client.post(
                "/admin/api/v1/llm/inheritance",
                json={
                    "capability": "chat.manager",
                    "inherits_from": "chat.admin",
                },
            )
            assert cycle.status_code == 422, cycle.text
            assert cycle.json()["error"] == "capability_inheritance_cycle"
        finally:
            _wipe(session_factory)

    def test_writes_are_admin_gated_and_validate_assignment_capabilities(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        try:
            assert client.post("/admin/api/v1/llm/sync-pricing").status_code == 404

            client.cookies.set(
                SESSION_COOKIE_NAME,
                _seed_admin(session_factory, settings=pinned_settings),
            )
            seeded = _seed_llm_graph(session_factory)

            duplicate = client.post(
                "/admin/api/v1/llm/providers",
                json={
                    "name": "OpenRouter",
                    "provider_type": "openrouter",
                },
            )
            assert duplicate.status_code == 409, duplicate.text
            assert duplicate.json()["error"] == "provider_name_exists"

            override = client.post(
                "/admin/api/v1/llm/assignments",
                json={
                    "capability": "tasks.assist",
                    "provider_model_id": seeded.provider_model_id,
                    "priority": 1,
                    "required_capabilities": [],
                },
            )
            assert override.status_code == 422, override.text
            assert override.json()["error"] == "required_capabilities_mismatch"

            model = client.post(
                "/admin/api/v1/llm/models",
                json={
                    "canonical_name": "text-only-test",
                    "display_name": "Text Only",
                    "vendor": "test",
                    "capabilities": ["chat"],
                },
            )
            assert model.status_code == 200, model.text
            provider_model = client.post(
                "/admin/api/v1/llm/provider-models",
                json={
                    "provider_id": seeded.provider_id,
                    "model_id": model.json()["id"],
                    "api_model_id": "text-only-test",
                },
            )
            assert provider_model.status_code == 200, provider_model.text

            missing = client.post(
                "/admin/api/v1/llm/assignments",
                json={
                    "capability": "expenses.autofill",
                    "provider_model_id": provider_model.json()["id"],
                    "priority": 1,
                },
            )
            assert missing.status_code == 422, missing.text
            assert missing.json()["error"] == "assignment_missing_capability"
            assert missing.json()["missing_capabilities"] == ["vision", "json_mode"]
        finally:
            _wipe(session_factory)

    def test_assignment_update_can_clear_nullable_fields_and_reorder_is_exact(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        try:
            client.cookies.set(
                SESSION_COOKIE_NAME,
                _seed_admin(session_factory, settings=pinned_settings),
            )
            seeded = _seed_llm_graph(session_factory)

            cleared = client.put(
                f"/admin/api/v1/llm/assignments/{seeded.assignment_id}",
                json={
                    "max_tokens": None,
                    "temperature": None,
                    "extra_api_params": None,
                },
            )
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["max_tokens"] is None
            assert cleared.json()["temperature"] is None
            assert cleared.json()["extra_api_params"] == {}

            added = client.post(
                "/admin/api/v1/llm/assignments",
                json={
                    "capability": "chat.manager",
                    "provider_model_id": seeded.provider_model_id,
                    "priority": 1,
                },
            )
            assert added.status_code == 200, added.text

            partial = client.patch(
                "/admin/api/v1/llm/assignments/reorder",
                json=[
                    {
                        "capability": "chat.manager",
                        "ids_in_priority_order": [added.json()["id"]],
                    }
                ],
            )
            assert partial.status_code == 422, partial.text
            assert partial.json()["error"] == "assignment_reorder_mismatch"
        finally:
            _wipe(session_factory)

    def test_openapi_includes_llm_admin_surface(self, client: TestClient) -> None:
        assert isinstance(client.app, FastAPI)
        schema = client.app.openapi()
        paths = schema["paths"]
        for path in (
            "/admin/api/v1/llm/graph",
            "/admin/api/v1/llm/calls",
            "/admin/api/v1/llm/prompts",
            "/admin/api/v1/llm/sync-pricing",
            "/admin/api/v1/llm/providers",
            "/admin/api/v1/llm/models",
            "/admin/api/v1/llm/provider-models",
            "/admin/api/v1/llm/assignments",
            "/admin/api/v1/llm/inheritance",
        ):
            assert path in paths
