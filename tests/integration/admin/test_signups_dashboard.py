"""Integration coverage for the deployment-admin signup-abuse feed."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.identity.models import MagicLinkNonce, SignupAttempt
from app.api.admin import admin_router
from app.api.deps import db_session as db_session_dep
from app.api.errors import add_exception_handlers
from app.api.v1.auth.signup import build_signup_router
from app.auth._throttle import _SIGNUP_IP_LIMIT, Throttle
from app.capabilities import Capabilities, DeploymentSettings, Features
from app.config import Settings
from app.tenancy import tenant_agnostic
from tests.unit.api.admin._helpers import (
    TEST_ACCEPT_LANGUAGE,
    TEST_UA,
    install_admin_cookie,
)


class _RecordingMailer:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    def send(
        self,
        *,
        to: Sequence[str],
        subject: str,
        body_text: str,
        body_html: str | None = None,
        headers: Mapping[str, str] | None = None,
        reply_to: str | None = None,
    ) -> str:
        self.sent.append(
            {
                "to": to,
                "subject": subject,
                "body_text": body_text,
                "body_html": body_html,
                "headers": headers,
                "reply_to": reply_to,
            }
        )
        return f"msg-{len(self.sent)}"


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-admin-signups-root-key"),
        public_url="https://crew.day",
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def client(
    engine: Engine,
    session_factory: sessionmaker[Session],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    import app.adapters.db.session as session_mod

    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    app = FastAPI()
    app.state.settings = settings
    app.include_router(
        build_signup_router(
            mailer=_RecordingMailer(),
            throttle=Throttle(),
            capabilities=Capabilities(
                features=Features(
                    rls=False,
                    fulltext_search=False,
                    concurrent_writers=False,
                    object_storage=False,
                    wildcard_subdomains=False,
                    email_bounce_webhooks=False,
                    llm_voice_input=False,
                    postgis=False,
                ),
                settings=DeploymentSettings(
                    signup_enabled=True,
                    captcha_required=False,
                )
            ),
            base_url="https://crew.day",
            settings=settings,
        ),
        prefix="/api/v1",
    )
    app.include_router(admin_router, prefix="/admin/api/v1")
    add_exception_handlers(app)

    def _session() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[db_session_dep] = _session

    original_engine = session_mod._default_engine
    original_factory = session_mod._default_sessionmaker_
    session_mod._default_engine = engine
    session_mod._default_sessionmaker_ = session_factory
    try:
        with TestClient(
            app,
            base_url="https://testserver",
            headers={
                "User-Agent": TEST_UA,
                "Accept-Language": TEST_ACCEPT_LANGUAGE,
            },
        ) as c:
            yield c
    finally:
        session_mod._default_engine = original_engine
        session_mod._default_sessionmaker_ = original_factory
        with session_factory() as s, tenant_agnostic():
            attempt_ids = list(
                s.scalars(
                    select(SignupAttempt.id).where(
                        SignupAttempt.email_lower.like("burst-%@example.com")
                    )
                ).all()
            )
            if attempt_ids:
                s.execute(
                    delete(MagicLinkNonce).where(
                        MagicLinkNonce.subject_id.in_(attempt_ids)
                    )
                )
            s.execute(
                delete(SignupAttempt).where(
                    SignupAttempt.email_lower.like("burst-%@example.com")
                )
            )
            s.execute(
                delete(AuditLog).where(
                    AuditLog.action.in_(
                        [
                            "audit.signup.suspicious",
                            "audit.signup.rate_limited",
                            "signup.requested",
                        ]
                    )
                )
            )
            s.commit()


def test_burst_rate_trip_lands_on_admin_signups_feed(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    install_admin_cookie(client, session_factory, settings)

    for idx in range(_SIGNUP_IP_LIMIT):
        resp = client.post(
            "/api/v1/signup/start",
            json={
                "email": f"burst-{idx}@example.com",
                "desired_slug": f"burst-{idx}",
            },
        )
        assert resp.status_code == 202, resp.text

    refused = client.post(
        "/api/v1/signup/start",
        json={
            "email": "burst-over@example.com",
            "desired_slug": "burst-over",
        },
    )
    assert refused.status_code == 429, refused.text

    with session_factory() as s:
        rows = s.scalars(
            select(AuditLog).where(AuditLog.action == "audit.signup.suspicious")
        ).all()
        assert len(rows) == 2
        assert {row.scope_kind for row in rows} == {"deployment"}

    feed = client.get("/admin/api/v1/signups")

    assert feed.status_code == 200, feed.text
    body = feed.json()
    assert len(body["data"]) == 2
    rate_limited = next(
        row for row in body["data"] if row["detail"]["reason"] == "rate_limited:ip"
    )
    assert rate_limited["kind"] == "distinct_emails_one_ip"
    assert rate_limited["detail"]["scope"] == "ip"
    blob = repr(body)
    assert "burst-over@example.com" not in blob
    assert "127.0.0.1" not in blob


def test_same_ip_three_distinct_emails_lands_before_rate_limit(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    install_admin_cookie(client, session_factory, settings)

    for idx in range(3):
        resp = client.post(
            "/api/v1/signup/start",
            json={
                "email": f"burst-{idx}@example.com",
                "desired_slug": f"distinct-{idx}",
            },
        )
        assert resp.status_code == 202, resp.text

    with session_factory() as s:
        rows = s.scalars(
            select(AuditLog).where(AuditLog.action == "audit.signup.suspicious")
        ).all()
        assert len(rows) == 1
        diff = rows[0].diff
        assert isinstance(diff, dict)
        assert diff["kind"] == "distinct_emails_one_ip"
        assert diff["reason"] == "distinct_emails_one_ip"
        assert diff["distinct_email_count"] == 3

    feed = client.get("/admin/api/v1/signups")

    assert feed.status_code == 200, feed.text
    body = feed.json()
    assert body["data"][0]["kind"] == "distinct_emails_one_ip"
    assert body["data"][0]["detail"]["distinct_email_count"] == 3
