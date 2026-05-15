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
from sqlalchemy import Engine, func, select
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
from app.adapters.llm.openrouter import (
    OpenRouterModelMetadata,
    normalize_openrouter_model_id,
)
from app.adapters.llm.ports import (
    ChatMessage,
    LlmProviderError,
    LLMResponse,
    LlmTransportError,
    LLMUsage,
)
from app.api.transport import admin_sse
from app.auth import tokens as auth_tokens
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.domain.llm.router import DEPLOYMENT_DEFAULT_CACHE_WORKSPACE_ID
from app.events.bus import EventBus
from app.events.types import LlmAssignmentChanged
from app.main import create_app
from app.tenancy import WorkspaceContext, tenant_agnostic
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


class _FailingLLMClient:
    def chat(
        self,
        *,
        model_id: str,
        messages: list[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        thinking_level: str = "disabled",
        thinking_strategy: str = "none",
        tools: object = None,
        consents: object = None,
    ) -> LLMResponse:
        del (
            model_id,
            messages,
            max_tokens,
            temperature,
            thinking_level,
            thinking_strategy,
            tools,
            consents,
        )
        raise LlmProviderError(
            "provider rejected Authorization: Bearer sk-test-secret-token"
        )


class _RecordingLLMClient:
    def __init__(self) -> None:
        self.calls = 0
        self.messages: list[list[ChatMessage]] = []
        self.max_tokens: list[int] = []
        self.thinking_levels: list[str] = []
        self.thinking_strategies: list[str] = []

    def chat(
        self,
        *,
        model_id: str,
        messages: list[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        thinking_level: str = "disabled",
        thinking_strategy: str = "none",
        tools: object = None,
        consents: object = None,
    ) -> LLMResponse:
        del temperature
        del tools, consents
        self.calls += 1
        self.messages.append(messages)
        self.max_tokens.append(max_tokens)
        self.thinking_levels.append(thinking_level)
        self.thinking_strategies.append(thinking_strategy)
        return LLMResponse(
            text="ok",
            usage=LLMUsage(
                prompt_tokens=1_000, completion_tokens=1_000, total_tokens=2_000
            ),
            model_id=model_id,
            finish_reason="stop",
        )


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


def _seed_scoped_token(
    session_factory: sessionmaker[Session],
    *,
    scopes: dict[str, object],
) -> str:
    with session_factory() as s:
        user = bootstrap_user(
            s, email=f"agent-{new_ulid()}@example.com", display_name="Agent"
        )
        workspace_id = new_ulid()
        workspace_slug = f"token-{workspace_id[-6:].lower()}"
        s.add(
            Workspace(
                id=workspace_id,
                slug=workspace_slug,
                name="Token Workspace",
                plan="free",
                quota_json={},
                created_at=_PINNED,
            )
        )
        s.flush()
        result = auth_tokens.mint(
            s,
            WorkspaceContext(
                workspace_id=workspace_id,
                workspace_slug=workspace_slug,
                actor_id=user.id,
                actor_kind="user",
                actor_grant_role="manager",
                actor_was_owner_member=False,
                audit_correlation_id=new_ulid(),
            ),
            user_id=user.id,
            label="admin llm scoped token",
            scopes=scopes,
            expires_at=None,
            kind="scoped",
            now=_PINNED,
        )
        s.commit()
        return result.token


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
                capabilities=["chat", "function_calling", "json_mode", "vision"],
                context_window=128000,
                max_output_tokens=8192,
                thinking_level="medium",
                thinking_strategy="openrouter_extra_body",
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
                thinking_strategy_override=None,
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
                thinking_level_override=None,
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


def _gemma_4_metadata(
    model_id: str = "google/gemma-4-31b-it",
) -> OpenRouterModelMetadata:
    return OpenRouterModelMetadata(
        model_id=model_id,
        display_name="Gemma 4 31B Instruct",
        capabilities=[
            "chat",
            "vision",
            "audio_input",
            "reasoning",
            "function_calling",
            "json_mode",
            "streaming",
        ],
        context_window=131072,
        max_output_tokens=8192,
        input_cost_per_million=Decimal("0.1500"),
        output_cost_per_million=Decimal("0.4500"),
        fixed_cost_per_call_usd=Decimal("0.0012"),
        supports_system_prompt=True,
        supports_temperature=True,
        thinking_level="disabled",
        thinking_strategy="openrouter_extra_body",
    )


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

    def test_provider_model_playground_runs_fake_direct_test(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        try:
            seeded = _seed_llm_graph(session_factory)
            assert (
                client.post(
                    f"/admin/api/v1/llm/provider-models/{seeded.provider_model_id}"
                    "/playground",
                    json={"prompt": "hello"},
                ).status_code
                == 404
            )
            client.cookies.set(
                SESSION_COOKIE_NAME,
                _seed_admin(session_factory, settings=pinned_settings),
            )
            with session_factory() as s, tenant_agnostic():
                provider = s.get(LlmProvider, seeded.provider_id)
                assert provider is not None
                provider.provider_type = "fake"
                provider.api_key_envelope_ref = None
                before_usage = s.scalar(select(func.count(LlmUsage.id))) or 0
                s.commit()

            empty_prompt = client.post(
                f"/admin/api/v1/llm/provider-models/{seeded.provider_model_id}"
                "/playground",
                json={"prompt": "  "},
            )
            assert empty_prompt.status_code == 422, empty_prompt.text

            resp = client.post(
                f"/admin/api/v1/llm/provider-models/{seeded.provider_model_id}"
                "/playground",
                json={"prompt": "hello playground", "max_tokens": 32},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "ok"
            assert body["assistant_text"] == "hello playground"
            assert body["model_used"] == "google/gemma-3-27b-it"
            assert body["provider_used"] == "OpenRouter"
            assert body["provider_model_id"] == seeded.provider_model_id
            assert body["assignment_id"] is None
            assert body["input_tokens"] == len("hello playground")
            assert body["output_tokens"] == len("hello playground")
            assert body["finish_reason"] == "stop"
            assert body["cost_usd"] is not None
            assert body["error_message"] is None
            with session_factory() as s, tenant_agnostic():
                after_usage = s.scalar(select(func.count(LlmUsage.id))) or 0
            assert after_usage == before_usage
        finally:
            _wipe(session_factory)

    def test_provider_model_playground_uses_runtime_client_and_bounds_tokens(
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
            llm = _RecordingLLMClient()
            client.app.state.llm = llm
            with session_factory() as s, tenant_agnostic():
                provider = s.get(LlmProvider, seeded.provider_id)
                assert provider is not None
                provider.api_key_envelope_ref = None
                s.commit()

            ok = client.post(
                f"/admin/api/v1/llm/provider-models/{seeded.provider_model_id}"
                "/playground",
                json={
                    "prompt": "hello",
                    "max_tokens": 64,
                    "thinking_level": "high",
                    "thinking_strategy": "gemma_system_token",
                },
            )
            assert ok.status_code == 200, ok.text
            assert ok.json()["status"] == "ok"
            assert ok.json()["cost_usd"] == "0.000300"
            assert ok.json()["cost_cents"] == 0
            assert llm.calls == 1
            assert llm.max_tokens == [64]
            assert llm.thinking_levels == ["high"]
            assert llm.thinking_strategies == ["gemma_system_token"]

            too_many_tokens = client.post(
                f"/admin/api/v1/llm/provider-models/{seeded.provider_model_id}"
                "/playground",
                json={"prompt": "hello", "max_tokens": 8193},
            )
            assert too_many_tokens.status_code == 422, too_many_tokens.text
            assert too_many_tokens.json()["error"] == "max_tokens_exceeds_model_limit"
            assert (
                "exceeds this model's known output-token limit"
                in too_many_tokens.json()["detail"]
            )
            assert llm.calls == 1

            with session_factory() as s, tenant_agnostic():
                model = s.get(LlmModel, seeded.model_id)
                assert model is not None
                model.max_output_tokens = 131_072
                s.commit()

            huge_model_default = client.post(
                f"/admin/api/v1/llm/provider-models/{seeded.provider_model_id}"
                "/playground",
                json={"prompt": "hello"},
            )
            assert huge_model_default.status_code == 200, huge_model_default.text
            assert llm.max_tokens[-1] == 32_000
        finally:
            _wipe(session_factory)

    def test_provider_model_playground_accepts_multipart_image_upload(
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
            llm = _RecordingLLMClient()
            client.app.state.llm = llm
            with session_factory() as s, tenant_agnostic():
                provider = s.get(LlmProvider, seeded.provider_id)
                assert provider is not None
                provider.api_key_envelope_ref = None
                s.commit()

            resp = client.post(
                f"/admin/api/v1/llm/provider-models/{seeded.provider_model_id}"
                "/playground",
                data={"prompt": "describe this image"},
                files={"image_file": ("receipt.png", b"image-bytes", "image/png")},
            )

            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "ok"
            assert llm.calls == 1
            sent = llm.messages[0]
            assert sent[0]["role"] == "user"
            content = sent[0]["content"]
            assert isinstance(content, list)
            assert content[0] == {"type": "text", "text": "describe this image"}
            assert content[1]["type"] == "image_url"
            assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        finally:
            _wipe(session_factory)

    def test_provider_model_playground_rejects_disabled_and_assignment_mismatch(
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
            other_provider_model_id = new_ulid()
            with session_factory() as s, tenant_agnostic():
                provider = s.get(LlmProvider, seeded.provider_id)
                assert provider is not None
                provider.provider_type = "fake"
                provider.api_key_envelope_ref = None
                other_model_id = new_ulid()
                s.add(
                    LlmModel(
                        id=other_model_id,
                        canonical_name="test/other",
                        display_name="Other",
                        capabilities=["chat"],
                        context_window=None,
                        max_output_tokens=None,
                        thinking_level="disabled",
                        thinking_strategy="none",
                        is_active=True,
                        price_source="",
                        price_source_model_id=None,
                        notes=None,
                        created_at=_PINNED,
                        updated_at=_PINNED,
                        updated_by_user_id=None,
                    )
                )
                s.add(
                    LlmProviderModel(
                        id=other_provider_model_id,
                        provider_id=seeded.provider_id,
                        model_id=other_model_id,
                        api_model_id="test/other",
                        input_cost_per_million=Decimal("0"),
                        output_cost_per_million=Decimal("0"),
                        fixed_cost_per_call_usd=None,
                        max_tokens_override=None,
                        temperature_override=None,
                        supports_system_prompt=True,
                        supports_temperature=True,
                        thinking_strategy_override=None,
                        extra_api_params={},
                        price_source_override="",
                        price_source_model_id_override=None,
                        price_last_synced_at=None,
                        is_enabled=True,
                        created_at=_PINNED,
                        updated_at=_PINNED,
                    )
                )
                s.commit()

            good_assignment = client.post(
                f"/admin/api/v1/llm/provider-models/{seeded.provider_model_id}"
                "/playground",
                json={
                    "mode": "assignment",
                    "assignment_id": seeded.assignment_id,
                    "prompt": "assignment smoke",
                },
            )
            assert good_assignment.status_code == 200, good_assignment.text
            assert good_assignment.json()["assignment_id"] == seeded.assignment_id

            mismatch = client.post(
                f"/admin/api/v1/llm/provider-models/{other_provider_model_id}"
                "/playground",
                json={
                    "mode": "assignment",
                    "assignment_id": seeded.assignment_id,
                    "prompt": "assignment smoke",
                },
            )
            assert mismatch.status_code == 422, mismatch.text
            assert mismatch.json()["error"] == "assignment_provider_model_mismatch"

            with session_factory() as s, tenant_agnostic():
                provider_model = s.get(LlmProviderModel, seeded.provider_model_id)
                assert provider_model is not None
                provider_model.is_enabled = False
                s.commit()
            disabled_provider_model = client.post(
                f"/admin/api/v1/llm/provider-models/{seeded.provider_model_id}"
                "/playground",
                json={"prompt": "hello"},
            )
            assert disabled_provider_model.status_code == 422
            assert disabled_provider_model.json()["error"] == "provider_model_disabled"

            with session_factory() as s, tenant_agnostic():
                provider_model = s.get(LlmProviderModel, seeded.provider_model_id)
                provider = s.get(LlmProvider, seeded.provider_id)
                assert provider_model is not None
                assert provider is not None
                provider_model.is_enabled = True
                provider.is_enabled = False
                s.commit()
            disabled_provider = client.post(
                f"/admin/api/v1/llm/provider-models/{seeded.provider_model_id}"
                "/playground",
                json={"prompt": "hello"},
            )
            assert disabled_provider.status_code == 422
            assert disabled_provider.json()["error"] == "provider_disabled"

            missing = client.post(
                f"/admin/api/v1/llm/provider-models/{new_ulid()}/playground",
                json={"prompt": "hello"},
            )
            assert missing.status_code == 404
            assert missing.json()["error"] == "not_found"
        finally:
            _wipe(session_factory)

    def test_provider_model_playground_redacts_provider_errors(
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
            client.app.state.llm = _FailingLLMClient()
            resp = client.post(
                f"/admin/api/v1/llm/provider-models/{seeded.provider_model_id}"
                "/playground",
                json={"prompt": "hello"},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "error"
            assert body["assistant_text"] is None
            assert body["error_id"]
            assert body["error_code"] == "provider_rejected_request"
            assert "sk-test-secret-token" not in body["error_message"]
            assert "<redacted:credential>" in body["error_message"]
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
                for assignment_id, priority in (
                    ("default-primary", 0),
                    ("default-fallback", 1),
                ):
                    s.add(
                        LlmAssignment(
                            id=assignment_id,
                            workspace_id=None,
                            capability="default",
                            model_id=seeded.provider_model_id,
                            provider="OpenRouter",
                            priority=priority,
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
            assert "priority" not in body["providers"][0]
            assert body["providers"][0]["spend_usd_30d"] == 0.174606
            assert body["providers"][0]["calls_30d"] == 3
            assert body["models"][0]["id"] == seeded.model_id
            assert body["models"][0]["thinking_level"] == "medium"
            assert body["models"][0]["thinking_strategy"] == "openrouter_extra_body"
            assert body["models"][0]["spend_usd_30d"] == 0.174606
            assert body["models"][0]["calls_30d"] == 3
            assert body["provider_models"][0]["id"] == seeded.provider_model_id
            assert "thinking_level_override" not in body["provider_models"][0]
            assert "effective_thinking_level" not in body["provider_models"][0]
            assert body["provider_models"][0]["thinking_strategy_override"] is None
            assert (
                body["provider_models"][0]["effective_thinking_strategy"]
                == "openrouter_extra_body"
            )
            assert body["provider_models"][0]["spend_usd_30d"] == 0.174606
            assert body["provider_models"][0]["calls_30d"] == 3
            assert body["assignments"][0]["id"] == seeded.assignment_id
            assert body["assignments"][0]["thinking_level_override"] is None
            assert body["assignments"][0]["effective_thinking_level"] == "medium"
            assert (
                body["assignments"][0]["effective_thinking_strategy"]
                == "openrouter_extra_body"
            )
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
            assert [assignment["id"] for assignment in default_assignments] == [
                "default-primary",
                "default-fallback",
            ]
            assert all(
                assignment["is_deployment_default"] is False
                for assignment in default_assignments
            )
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
                    "thinking_level_override": "disabled",
                    "is_enabled": True,
                },
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["capability"] == "tasks.assist"
            assert body["required_capabilities"] == ["chat"]
            assert body["thinking_level_override"] == "disabled"
            assert body["effective_thinking_level"] == "disabled"
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

            stale_provider_priority = client.post(
                "/admin/api/v1/llm/providers",
                json={
                    "name": "Stale Priority Provider",
                    "provider_type": "fake",
                    "priority": 0,
                },
            )
            assert stale_provider_priority.status_code == 422, (
                stale_provider_priority.text
            )

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
                    "capabilities": ["chat"],
                    "thinking_level": "low",
                    "thinking_strategy": "openrouter_extra_body",
                },
            )
            assert model.status_code == 200, model.text
            assert model.json()["thinking_level"] == "low"
            assert model.json()["thinking_strategy"] == "openrouter_extra_body"

            default_strategy_model = client.post(
                "/admin/api/v1/llm/models",
                json={
                    "canonical_name": "default-thinking-strategy-test",
                    "display_name": "Default Thinking Strategy",
                    "capabilities": ["chat"],
                },
            )
            assert default_strategy_model.status_code == 200, (
                default_strategy_model.text
            )
            assert default_strategy_model.json()["thinking_strategy"] == "none"

            invalid_model = client.post(
                "/admin/api/v1/llm/models",
                json={
                    "canonical_name": "bad-thinking-test",
                    "display_name": "Bad Thinking",
                    "capabilities": ["chat"],
                    "thinking_level": "turbo",
                    "thinking_strategy": "none",
                },
            )
            assert invalid_model.status_code == 422, invalid_model.text

            invalid_strategy_model = client.post(
                "/admin/api/v1/llm/models",
                json={
                    "canonical_name": "bad-thinking-strategy-test",
                    "display_name": "Bad Thinking Strategy",
                    "capabilities": ["chat"],
                    "thinking_strategy": "turbo",
                },
            )
            assert invalid_strategy_model.status_code == 422, (
                invalid_strategy_model.text
            )

            provider_model = client.post(
                "/admin/api/v1/llm/provider-models",
                json={
                    "provider_id": seeded.provider_id,
                    "model_id": model.json()["id"],
                    "api_model_id": "text-only-test",
                    "thinking_strategy_override": "glm_extra_body",
                    "is_enabled": False,
                },
            )
            assert provider_model.status_code == 200, provider_model.text
            assert provider_model.json()["is_enabled"] is False
            assert "thinking_level_override" not in provider_model.json()
            assert "effective_thinking_level" not in provider_model.json()
            assert (
                provider_model.json()["thinking_strategy_override"] == "glm_extra_body"
            )
            assert (
                provider_model.json()["effective_thinking_strategy"] == "glm_extra_body"
            )

            inherited_provider_model = client.put(
                f"/admin/api/v1/llm/provider-models/{provider_model.json()['id']}",
                json={
                    "provider_id": seeded.provider_id,
                    "model_id": model.json()["id"],
                    "api_model_id": "text-only-test",
                    "thinking_strategy_override": "",
                    "is_enabled": False,
                },
            )
            assert inherited_provider_model.status_code == 200, (
                inherited_provider_model.text
            )
            assert inherited_provider_model.json()["is_enabled"] is False
            assert inherited_provider_model.json()["thinking_strategy_override"] is None
            assert (
                inherited_provider_model.json()["effective_thinking_strategy"]
                == "openrouter_extra_body"
            )

            unsupported_level_field = client.post(
                "/admin/api/v1/llm/provider-models",
                json={
                    "provider_id": seeded.provider_id,
                    "model_id": model.json()["id"],
                    "api_model_id": "bad-thinking-test",
                    "thinking_level_override": "turbo",
                },
            )
            assert unsupported_level_field.status_code == 422, (
                unsupported_level_field.text
            )

            invalid_provider_strategy = client.post(
                "/admin/api/v1/llm/provider-models",
                json={
                    "provider_id": seeded.provider_id,
                    "model_id": model.json()["id"],
                    "api_model_id": "bad-thinking-strategy-test",
                    "thinking_strategy_override": "turbo",
                },
            )
            assert invalid_provider_strategy.status_code == 422, (
                invalid_provider_strategy.text
            )

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

    def test_openrouter_model_preview_prefills_create_payload_and_detects_existing(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        try:
            assert (
                client.post(
                    "/admin/api/v1/llm/models/openrouter-preview",
                    json={"model_id_or_url": "google/gemma-4-31b-it"},
                ).status_code
                == 404
            )
            read_only_token = _seed_scoped_token(
                session_factory,
                scopes={"deployment.llm:read": True},
            )
            read_only = client.post(
                "/admin/api/v1/llm/models/openrouter-preview",
                json={"model_id_or_url": "google/gemma-4-31b-it"},
                headers={"Authorization": f"Bearer {read_only_token}"},
            )
            assert read_only.status_code == 404, read_only.text

            client.cookies.set(
                SESSION_COOKIE_NAME,
                _seed_admin(session_factory, settings=pinned_settings),
            )
            seeded = _seed_llm_graph(session_factory)
            existing_model_id = new_ulid()
            existing_provider_model_id = new_ulid()
            with session_factory() as s, tenant_agnostic():
                s.add(
                    LlmModel(
                        id=existing_model_id,
                        canonical_name="gemma-4-31b-it",
                        display_name="Existing Gemma 4",
                        capabilities=["chat"],
                        context_window=4096,
                        max_output_tokens=None,
                        thinking_level="disabled",
                        thinking_strategy="none",
                        is_active=True,
                        price_source="openrouter",
                        price_source_model_id="google/gemma-4-31b-it",
                        notes=None,
                        created_at=_PINNED,
                        updated_at=_PINNED,
                        updated_by_user_id=None,
                    )
                )
                s.flush()
                s.add(
                    LlmProviderModel(
                        id=existing_provider_model_id,
                        provider_id=seeded.provider_id,
                        model_id=existing_model_id,
                        api_model_id="google/gemma-4-31b-it",
                        input_cost_per_million=Decimal("1.0000"),
                        output_cost_per_million=Decimal("2.0000"),
                        fixed_cost_per_call_usd=None,
                        max_tokens_override=None,
                        temperature_override=None,
                        supports_system_prompt=True,
                        supports_temperature=True,
                        thinking_strategy_override=None,
                        extra_api_params={},
                        price_source_override="",
                        price_source_model_id_override=None,
                        price_last_synced_at=None,
                        is_enabled=True,
                        created_at=_PINNED,
                        updated_at=_PINNED,
                    )
                )
                s.commit()

            seen: list[str] = []

            def fake_lookup(model_id_or_url: str) -> OpenRouterModelMetadata:
                seen.append(model_id_or_url)
                return _gemma_4_metadata(
                    model_id=normalize_openrouter_model_id(model_id_or_url)
                )

            monkeypatch.setattr(
                "app.api.admin.llm.fetch_openrouter_model_metadata", fake_lookup
            )

            resp = client.post(
                "/admin/api/v1/llm/models/openrouter-preview",
                json={"model_id_or_url": "google/gemma-4-31b-it"},
            )

            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert seen == ["google/gemma-4-31b-it"]
            assert body["openrouter_model_id"] == "google/gemma-4-31b-it"
            assert body["existing_model_id"] == existing_model_id
            assert body["model_payload"]["canonical_name"] == "google/gemma-4-31b-it"
            assert body["model_payload"]["display_name"] == "Gemma 4 31B Instruct"
            assert body["model_payload"]["capabilities"] == [
                "chat",
                "vision",
                "audio_input",
                "reasoning",
                "function_calling",
                "json_mode",
                "streaming",
            ]
            assert body["model_payload"]["context_window"] == 131072
            assert body["model_payload"]["max_output_tokens"] == 8192
            assert body["model_payload"]["thinking_level"] == "disabled"
            assert body["model_payload"]["thinking_strategy"] == "openrouter_extra_body"
            assert body["model_payload"]["price_source"] == "openrouter"
            assert (
                body["model_payload"]["price_source_model_id"]
                == "google/gemma-4-31b-it"
            )

            provider_preview = body["provider_model_previews"][0]
            assert provider_preview["provider_id"] == seeded.provider_id
            assert provider_preview["provider_name"] == "OpenRouter"
            assert (
                provider_preview["existing_provider_model_id"]
                == existing_provider_model_id
            )
            assert provider_preview["payload"]["model_id"] == existing_model_id
            assert provider_preview["payload"]["api_model_id"] == (
                "google/gemma-4-31b-it"
            )
            assert provider_preview["payload"]["input_cost_per_million"] == 0.15
            assert provider_preview["payload"]["output_cost_per_million"] == 0.45
            assert provider_preview["payload"]["fixed_cost_per_call_usd"] == 0.0012
            assert provider_preview["payload"]["supports_system_prompt"] is True
            assert provider_preview["payload"]["supports_temperature"] is True
            assert provider_preview["payload"]["price_source_override"] == "openrouter"
            assert provider_preview["payload"]["price_source_model_id_override"] == (
                "google/gemma-4-31b-it"
            )
        finally:
            _wipe(session_factory)

    def test_openrouter_model_preview_accepts_url_and_maps_safe_errors(
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
            seen: list[str] = []

            def fake_lookup(model_id_or_url: str) -> OpenRouterModelMetadata:
                seen.append(model_id_or_url)
                if model_id_or_url == "bad id":
                    raise ValueError("bad id")
                if model_id_or_url == "missing/model":
                    raise LlmProviderError("not found")
                if model_id_or_url == "transport/model":
                    raise LlmTransportError(
                        "Authorization: Bearer sk-openrouter-secret"
                    )
                if model_id_or_url == "oversized/model":
                    return OpenRouterModelMetadata(
                        model_id="oversized/model",
                        display_name="x" * 241,
                        capabilities=["chat"],
                        context_window=None,
                        max_output_tokens=None,
                        input_cost_per_million=Decimal("0"),
                        output_cost_per_million=Decimal("0"),
                        fixed_cost_per_call_usd=None,
                        supports_system_prompt=True,
                        supports_temperature=True,
                        thinking_level="disabled",
                        thinking_strategy="none",
                    )
                return _gemma_4_metadata(
                    model_id=normalize_openrouter_model_id(model_id_or_url)
                )

            monkeypatch.setattr(
                "app.api.admin.llm.fetch_openrouter_model_metadata", fake_lookup
            )

            url_resp = client.post(
                "/admin/api/v1/llm/models/openrouter-preview",
                json={
                    "model_id_or_url": (
                        "https://openrouter.ai/models/google/gemma-4-31b-it"
                    )
                },
            )
            assert url_resp.status_code == 200, url_resp.text
            assert url_resp.json()["openrouter_model_id"] == "google/gemma-4-31b-it"
            assert seen[-1] == "https://openrouter.ai/models/google/gemma-4-31b-it"

            invalid = client.post(
                "/admin/api/v1/llm/models/openrouter-preview",
                json={"model_id_or_url": "bad id"},
            )
            assert invalid.status_code == 422, invalid.text
            assert invalid.json()["error"] == "invalid_openrouter_model_id"

            empty = client.post(
                "/admin/api/v1/llm/models/openrouter-preview",
                json={"model_id_or_url": ""},
            )
            assert empty.status_code == 422, empty.text
            assert empty.json()["error"] == "invalid_openrouter_model_id"

            missing = client.post(
                "/admin/api/v1/llm/models/openrouter-preview",
                json={"model_id_or_url": "missing/model"},
            )
            assert missing.status_code == 404, missing.text
            assert missing.json()["error"] == "openrouter_model_not_found"

            transport = client.post(
                "/admin/api/v1/llm/models/openrouter-preview",
                json={"model_id_or_url": "transport/model"},
            )
            assert transport.status_code == 502, transport.text
            body = transport.json()
            assert body["error"] == "openrouter_unavailable"
            assert body["upstream"] == "openrouter"
            assert "sk-openrouter-secret" not in transport.text

            oversized = client.post(
                "/admin/api/v1/llm/models/openrouter-preview",
                json={"model_id_or_url": "oversized/model"},
            )
            assert oversized.status_code == 502, oversized.text
            assert oversized.json()["error"] == "openrouter_unavailable"
            assert "x" * 241 not in oversized.text
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

            override = client.put(
                f"/admin/api/v1/llm/assignments/{seeded.assignment_id}",
                json={"thinking_level_override": "low"},
            )
            assert override.status_code == 200, override.text
            assert override.json()["thinking_level_override"] == "low"
            assert override.json()["effective_thinking_level"] == "low"

            inherited = client.put(
                f"/admin/api/v1/llm/assignments/{seeded.assignment_id}",
                json={"thinking_level_override": None},
            )
            assert inherited.status_code == 200, inherited.text
            assert inherited.json()["thinking_level_override"] is None
            assert inherited.json()["effective_thinking_level"] == "medium"

            invalid = client.put(
                f"/admin/api/v1/llm/assignments/{seeded.assignment_id}",
                json={"thinking_level_override": "turbo"},
            )
            assert invalid.status_code == 422, invalid.text

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
