"""Demo-mode disabled integration guardrails."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.adapters.mail.null import NullMailer
from app.api.factory import create_app
from app.config import Settings
from app.domain.llm.router import resolve_model
from app.tenancy import WorkspaceContext


def test_demo_mailer_is_null_even_with_smtp_env() -> None:
    app = create_app(settings=_settings())

    assert isinstance(app.state.mailer, NullMailer)
    assert app.state.mailer.send(
        to=("person@example.test",),
        subject="Demo",
        body_text="Suppressed",
    ).startswith("demo:")


def test_disabled_auth_integration_returns_501() -> None:
    app = create_app(settings=_settings())
    client = TestClient(app, base_url="https://demo.crew.day")

    response = client.post("/api/v1/auth/passkey/login/start", json={})

    assert response.status_code == 501
    assert response.json()["error"] == "not_implemented_in_demo"
    assert response.json()["integration"] == "passkeys"


def test_demo_llm_allowed_capability_uses_free_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.domain.llm.router.get_settings", _settings)
    chain = resolve_model(
        session=Session(),
        ctx=_ctx(),
        capability="chat.manager",
    )

    assert len(chain) == 1
    assert chain[0].api_model_id.endswith(":free")


def test_demo_llm_disabled_capability_has_no_live_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.domain.llm.router.get_settings", _settings)
    assert (
        resolve_model(
            session=Session(),
            ctx=_ctx(),
            capability="expenses.autofill",
        )
        == []
    )


def _ctx() -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id="01HWA00000000000000000WSP",
        workspace_slug="demo",
        actor_id="01HWA00000000000000000USR",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000AUD",
        principal_kind="session",
    )


def _settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("demo-root-key"),
        demo_cookie_key=SecretStr("demo-cookie-key"),
        demo_mode=True,
        public_url="https://demo.crew.day",
        bind_host="127.0.0.1",
        worker="external",
        smtp_host="smtp.example.test",
        smtp_from="crew@example.test",
        openrouter_api_key=SecretStr("demo-openrouter-key"),
    )
