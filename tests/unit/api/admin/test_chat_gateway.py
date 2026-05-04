"""Unit tests for :mod:`app.api.admin.chat_gateway`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.messaging.models import (
    ChatChannel,
    ChatGatewayBinding,
    ChatMessage,
)
from app.auth.session import SESSION_COOKIE_NAME
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.unit.api.admin._helpers import (
    PINNED,
    build_client,
    engine_fixture,
    grant_deployment_admin,
    issue_session,
    seed_user,
    seed_workspace,
    settings_fixture,
)


@pytest.fixture
def settings() -> Settings:
    return settings_fixture("chat-gateway").model_copy(
        update={
            "public_url": "https://app.example.test",
            "chat_gateway_workspace_id": "01HWORKSPACECHATGATEWAY01",
            "chat_gateway_meta_whatsapp_secret": SecretStr("whsec_test"),
        }
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    yield from engine_fixture()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    yield from build_client(settings, session_factory, monkeypatch)


def _admin_cookie(session_factory: sessionmaker[Session], settings: Settings) -> str:
    with session_factory() as s:
        user_id = seed_user(s, email="ada@example.com", display_name="Ada")
        grant_deployment_admin(s, user_id=user_id)
        s.commit()
    return issue_session(session_factory, user_id=user_id, settings=settings)


def _install_admin_cookie(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings))


def _seed_gateway_binding(
    session_factory: sessionmaker[Session],
    *,
    provider: str = "meta_whatsapp",
    last_message_at: datetime = PINNED,
) -> None:
    with session_factory() as s:
        workspace_id = seed_workspace(s, slug="gateway")
        channel_id = new_ulid()
        binding_id = new_ulid()
        with tenant_agnostic():
            s.add(
                ChatChannel(
                    id=channel_id,
                    workspace_id=workspace_id,
                    kind="chat_gateway",
                    source="whatsapp",
                    external_ref="+15551234567",
                    title="WhatsApp: Ada",
                    created_at=PINNED,
                )
            )
            s.add(
                ChatGatewayBinding(
                    id=binding_id,
                    workspace_id=workspace_id,
                    provider=provider,
                    external_contact="+15551234567",
                    channel_id=channel_id,
                    display_label="WhatsApp: Ada",
                    provider_metadata_json={},
                    created_at=PINNED,
                    last_message_at=last_message_at,
                )
            )
        s.commit()


class TestChatGatewayProviders:
    def test_lists_provider_rows_with_secret_stubs_only(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        _seed_gateway_binding(session_factory)
        _install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/chat/providers")

        assert resp.status_code == 200
        body = resp.json()
        assert [row["channel_kind"] for row in body] == [
            "offapp_whatsapp",
            "offapp_telegram",
        ]
        whatsapp = body[0]
        assert whatsapp["status"] == "connected"
        assert (
            whatsapp["webhook_url"]
            == "https://app.example.test/webhooks/chat/meta_whatsapp"
        )
        assert whatsapp["verify_token_stub"] == "***"
        assert whatsapp["last_webhook_at"] == "2026-04-25T12:00:00+00:00"
        assert {item["name"] for item in whatsapp["templates"]} == {
            "chat_agent_nudge",
            "chat_channel_link_code",
            "chat_workspace_pick",
        }
        assert "whsec_test" not in resp.text
        assert all("envelope" not in key for row in body for key in row)
        secret_cred = next(
            cred
            for cred in whatsapp["credentials"]
            if cred["field"] == "webhook_signature_secret"
        )
        assert secret_cred == {
            "field": "webhook_signature_secret",
            "label": "Webhook signature secret",
            "display_stub": "***",
            "set": True,
            "updated_at": None,
            "updated_by": None,
        }

    @pytest.mark.parametrize(
        "path",
        [
            "/admin/api/v1/chat/providers",
            "/admin/api/v1/chat/templates",
            "/admin/api/v1/chat/overrides",
            "/admin/api/v1/chat/health",
        ],
    )
    def test_hidden_from_non_admins(self, client: TestClient, path: str) -> None:
        resp = client.get(path)

        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"

    def test_test_inbound_hidden_from_non_admins(self, client: TestClient) -> None:
        resp = client.post("/admin/api/v1/chat/test-inbound", json={})

        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"

    def test_empty_configuration_is_not_configured(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        settings.chat_gateway_meta_whatsapp_secret = None
        settings.chat_gateway_workspace_id = None
        _install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/chat/providers")

        assert resp.status_code == 200
        body = resp.json()
        assert body[0]["channel_kind"] == "offapp_whatsapp"
        assert body[0]["status"] == "not_configured"
        assert body[0]["verify_token_stub"] == ""
        assert body[0]["credentials"][0]["display_stub"] == ""
        assert body[1]["channel_kind"] == "offapp_telegram"
        assert body[1]["status"] == "not_configured"


class TestChatGatewayTemplatesOverridesHealth:
    def test_templates_endpoint_returns_sync_rows(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        _install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/chat/templates")

        assert resp.status_code == 200
        assert resp.json() == [
            {
                "name": "chat_channel_link_code",
                "purpose": "Initial channel link verification code",
                "status": "pending",
                "last_sync_at": None,
                "rejection_reason": None,
            },
            {
                "name": "chat_agent_nudge",
                "purpose": "Agent follow-up outside the 24-hour session window",
                "status": "pending",
                "last_sync_at": None,
                "rejection_reason": None,
            },
            {
                "name": "chat_workspace_pick",
                "purpose": "Workspace-selection prompt for ambiguous inbound messages",
                "status": "pending",
                "last_sync_at": None,
                "rejection_reason": None,
            },
        ]

    def test_overrides_endpoint_returns_empty_until_override_table_exists(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        _install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/chat/overrides")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_overrides_openapi_documents_empty_placeholder(
        self,
        client: TestClient,
    ) -> None:
        schema = client.get("/openapi.json").json()

        operation = schema["paths"]["/admin/api/v1/chat/overrides"]["get"]
        assert (
            "empty list until workspace-specific chat provider"
            in operation["description"]
        )

    def test_health_endpoint_splits_provider_health(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        _seed_gateway_binding(session_factory)
        _install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/chat/health")

        assert resp.status_code == 200
        assert resp.json() == {
            "providers": [
                {
                    "channel_kind": "offapp_whatsapp",
                    "status": "connected",
                    "last_webhook_at": "2026-04-25T12:00:00+00:00",
                    "last_webhook_error": None,
                    "outbound_24h": 0,
                    "delivery_error_rate_pct": 0.0,
                },
                {
                    "channel_kind": "offapp_telegram",
                    "status": "not_configured",
                    "last_webhook_at": None,
                    "last_webhook_error": None,
                    "outbound_24h": 0,
                    "delivery_error_rate_pct": 0.0,
                },
            ]
        }

    def test_test_inbound_persists_and_dispatches_synthetic_message(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            settings.chat_gateway_workspace_id = seed_workspace(s, slug="test-inbound")
            s.commit()
        _install_admin_cookie(client, session_factory, settings)

        resp = client.post(
            "/admin/api/v1/chat/test-inbound",
            json={
                "external_contact": " +15551230000 ",
                "body_md": " Need help with check-in ",
                "language_hint": " en ",
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["correlation_id"]
        assert body["dispatch_status"] == "enqueued"
        assert body["agent_invoked"] is True
        assert body["failure_reason"] is None
        assert body["latency_ms"] >= 0
        with session_factory() as s:
            message = s.get(ChatMessage, body["message_id"])
            assert message is not None
            assert message.workspace_id == settings.chat_gateway_workspace_id
            assert message.body_md == "Need help with check-in"
            assert message.dispatched_to_agent_at is not None

    def test_test_inbound_openapi_documents_trimmed_required_text(
        self,
        client: TestClient,
    ) -> None:
        schema = client.get("/openapi.json").json()

        request_schema = schema["components"]["schemas"]["AdminChatTestInboundRequest"]
        assert (
            request_schema["properties"]["external_contact"]["pattern"]
            == r"[^\s\x00-\x20]"
        )
        assert request_schema["properties"]["body_md"]["pattern"] == r"[^\s\x00-\x20]"

    @pytest.mark.parametrize(
        "payload",
        [
            {"external_contact": "   ", "body_md": "Need help"},
            {"external_contact": "+15551230000", "body_md": "   "},
            {"external_contact": "\x01", "body_md": "Need help"},
            {"external_contact": "+15551230000", "body_md": "\x01"},
        ],
    )
    def test_test_inbound_rejects_blank_direct_payload(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        payload: dict[str, str],
    ) -> None:
        with session_factory() as s:
            settings.chat_gateway_workspace_id = seed_workspace(
                s,
                slug="test-inbound-validation",
            )
            s.commit()
        _install_admin_cookie(client, session_factory, settings)

        resp = client.post("/admin/api/v1/chat/test-inbound", json=payload)

        assert resp.status_code == 422

    def test_test_inbound_requires_configured_provider_workspace(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        _install_admin_cookie(client, session_factory, settings)

        resp = client.post("/admin/api/v1/chat/test-inbound", json={})

        assert resp.status_code == 409
        assert resp.json()["error"] == "chat_gateway_provider_not_configured"
