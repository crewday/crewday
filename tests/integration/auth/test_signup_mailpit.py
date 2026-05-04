"""Mailpit-backed self-serve signup coverage."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker
from webauthn.helpers.structs import (
    AttestationFormat,
    CredentialDeviceType,
    PublicKeyCredentialType,
)

from app.adapters.db.authz.models import (
    RoleGrant,
)
from app.adapters.db.identity.models import (
    MagicLinkNonce,
    PasskeyCredential,
    SignupAttempt,
    User,
)
from app.adapters.db.workspace.models import Workspace
from app.adapters.mail.smtp import SMTPMailer
from app.api.deps import db_session as _db_session_dep
from app.api.errors import add_exception_handlers
from app.api.v1.auth.signup import build_signup_router
from app.auth import passkey
from app.auth._throttle import Throttle
from app.auth.webauthn import VerifiedRegistration
from app.capabilities import Capabilities, DeploymentSettings, Features
from app.config import Settings
from tests.integration.auth._signup_cleanup import delete_signup_rows
from tests.integration.mail import (
    fetch_message_detail,
    is_reachable,
    mailpit_test_lock,
    purge_inbox,
    wait_for_message,
)

pytestmark = pytest.mark.integration

_DEFAULT_MAILPIT_URL = "http://127.0.0.1:8026"
_DEFAULT_MAILPIT_SMTP_HOST = "127.0.0.1"
_DEFAULT_MAILPIT_SMTP_PORT = 1026
_SIGNUP_SUBJECT = "crew.day — verify your email and finish signing up"
_SIGNUP_PURPOSE = "signup_verify"


def _mailpit_url() -> str:
    return os.environ.get("CREWDAY_TEST_MAILPIT_URL", _DEFAULT_MAILPIT_URL)


def _mailpit_smtp_host() -> str:
    return os.environ.get("CREWDAY_TEST_MAILPIT_SMTP_HOST", _DEFAULT_MAILPIT_SMTP_HOST)


def _mailpit_smtp_port() -> int:
    return int(
        os.environ.get("CREWDAY_TEST_MAILPIT_SMTP_PORT", _DEFAULT_MAILPIT_SMTP_PORT)
    )


@pytest.fixture
def clean_mailpit() -> Iterator[str]:
    mailpit_url = _mailpit_url()
    with mailpit_test_lock():
        if not is_reachable(mailpit_url):
            pytest.skip(
                f"Mailpit not reachable at {mailpit_url}; start the dev stack with "
                "`docker compose -f mocks/docker-compose.yml up -d --build`"
            )
        purge_inbox(mailpit_url)
        yield mailpit_url


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-root-key"),
        public_url="https://crew.day",
    )


@pytest.fixture
def capabilities() -> Capabilities:
    return Capabilities(
        features=Features(
            rls=False,
            fulltext_search=False,
            concurrent_writers=False,
            object_storage=False,
            wildcard_subdomains=False,
            email_bounce_webhooks=False,
            llm_voice_input=False,
            postgis=False,
            webauthn_configured=False,
        ),
        settings=DeploymentSettings(signup_enabled=True, captcha_required=False),
    )


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def mailer() -> SMTPMailer:
    return SMTPMailer(
        host=_mailpit_smtp_host(),
        port=_mailpit_smtp_port(),
        from_addr="crew.day dev <no-reply@dev.crew.day>",
        use_tls=False,
        timeout=5,
    )


@pytest.fixture
def client(
    engine: Engine,
    mailer: SMTPMailer,
    throttle: Throttle,
    capabilities: Capabilities,
    settings: Settings,
) -> Iterator[TestClient]:
    import app.adapters.db.session as _session_mod

    app = FastAPI()
    add_exception_handlers(app)
    app.include_router(
        build_signup_router(
            mailer=mailer,
            throttle=throttle,
            capabilities=capabilities,
            base_url="https://crew.day",
            settings=settings,
        ),
        prefix="/api/v1",
    )

    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    def _session() -> Iterator[Session]:
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[_db_session_dep] = _session

    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = factory
    try:
        with TestClient(app) as c:
            yield c
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory
        _delete_signup_rows(factory)


def _delete_signup_rows(factory: sessionmaker[Session]) -> None:
    delete_signup_rows(
        factory,
        email_like="signup-mailpit-%@dev.local",
        slug_like="signup-mailpit-%",
    )


def _stub_passkey_verifier(monkeypatch: pytest.MonkeyPatch) -> bytes:
    credential_id = b"mailpit-signup-cred-" + b"x" * 12
    verified = VerifiedRegistration(
        credential_id=credential_id,
        credential_public_key=b"pub-" + b"\x00" * 60,
        sign_count=0,
        aaguid="00000000-0000-0000-0000-000000000000",
        fmt=AttestationFormat.NONE,
        credential_type=PublicKeyCredentialType.PUBLIC_KEY,
        user_verified=True,
        attestation_object=b"",
        credential_device_type=CredentialDeviceType.SINGLE_DEVICE,
        credential_backed_up=False,
    )

    def _fake_verify(**_: Any) -> VerifiedRegistration:
        return verified

    monkeypatch.setattr(passkey, "_verify_or_raise", _fake_verify)
    return credential_id


def _unique_email() -> str:
    return f"signup-mailpit-{uuid.uuid4()}@dev.local"


def _unique_slug() -> str:
    return f"signup-mailpit-{uuid.uuid4().hex[:12]}"


def _extract_magic_url(body_text: str) -> str:
    for raw_line in body_text.splitlines():
        line = raw_line.strip()
        if line.startswith("http") and "/auth/magic/" in line:
            return line
    raise AssertionError(f"no /auth/magic/ URL found in body:\n{body_text!r}")


def _token_from_url(magic_url: str) -> str:
    parsed = urlparse(magic_url)
    parts = parsed.path.split("/")
    if len(parts) != 4 or parts[1] != "auth" or parts[2] != "magic" or not parts[3]:
        raise AssertionError(f"unexpected magic-link path: {parsed.path!r}")
    return parts[3]


def _start_signup(client: TestClient, *, email: str, slug: str) -> None:
    response = client.post(
        "/api/v1/signup/start",
        json={"email": email, "desired_slug": slug},
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"status": "accepted"}


def _token_from_mailpit(mailpit_url: str, *, email: str) -> str:
    envelope = wait_for_message(mailpit_url, to=email)
    assert envelope["Subject"] == _SIGNUP_SUBJECT
    detail = fetch_message_detail(mailpit_url, envelope["ID"])
    text_body = detail.get("Text")
    assert isinstance(text_body, str) and text_body
    return _token_from_url(_extract_magic_url(text_body))


def _backdate_signup_nonce(engine: Engine, *, email: str) -> None:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        attempt = s.scalars(
            select(SignupAttempt).where(SignupAttempt.email_lower == email)
        ).one()
        nonce = s.scalars(
            select(MagicLinkNonce).where(
                MagicLinkNonce.purpose == _SIGNUP_PURPOSE,
                MagicLinkNonce.subject_id == attempt.id,
                MagicLinkNonce.consumed_at.is_(None),
            )
        ).one()
        nonce.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        s.commit()


def test_signup_round_trip_uses_mailpit_email(
    client: TestClient,
    clean_mailpit: str,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_passkey_verifier(monkeypatch)
    email = _unique_email()
    slug = _unique_slug()

    _start_signup(client, email=email, slug=slug)
    token = _token_from_mailpit(clean_mailpit, email=email)

    verify_response = client.post("/api/v1/signup/verify", json={"token": token})
    assert verify_response.status_code == 200, verify_response.text
    verify_body = verify_response.json()
    assert verify_body["desired_slug"] == slug
    signup_session_id = verify_body["signup_session_id"]

    start_response = client.post(
        "/api/v1/signup/passkey/start",
        json={
            "signup_session_id": signup_session_id,
            "display_name": "Mailpit Signup Owner",
        },
    )
    assert start_response.status_code == 200, start_response.text
    challenge_id = start_response.json()["challenge_id"]

    finish_response = client.post(
        "/api/v1/signup/passkey/finish",
        json={
            "signup_session_id": signup_session_id,
            "challenge_id": challenge_id,
            "display_name": "Mailpit Signup Owner",
            "timezone": "Pacific/Auckland",
            "credential": {
                "id": "stub",
                "rawId": "stub",
                "response": {},
                "type": "public-key",
            },
        },
    )
    assert finish_response.status_code == 200, finish_response.text
    assert finish_response.json() == {
        "workspace_slug": slug,
        "redirect": f"/w/{slug}/today",
    }

    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        workspace = s.scalars(select(Workspace).where(Workspace.slug == slug)).one()
        user = s.scalars(select(User).where(User.email_lower == email)).one()
        passkey_credential = s.scalars(
            select(PasskeyCredential).where(PasskeyCredential.user_id == user.id)
        ).one()
        role_grant = s.scalars(
            select(RoleGrant).where(RoleGrant.user_id == user.id)
        ).one()
        attempt = s.scalars(
            select(SignupAttempt).where(SignupAttempt.email_lower == email)
        ).one()

        assert workspace.plan == "free"
        assert user.display_name == "Mailpit Signup Owner"
        assert passkey_credential.user_id == user.id
        assert role_grant.workspace_id == workspace.id
        assert attempt.completed_at is not None
        assert attempt.workspace_id == workspace.id


def test_expired_signup_nonce_returns_410(
    client: TestClient,
    clean_mailpit: str,
    engine: Engine,
) -> None:
    email = _unique_email()
    slug = _unique_slug()

    _start_signup(client, email=email, slug=slug)
    token = _token_from_mailpit(clean_mailpit, email=email)
    _backdate_signup_nonce(engine, email=email)

    response = client.post("/api/v1/signup/verify", json={"token": token})
    assert response.status_code == 410, response.text
    assert response.json()["error"] == "expired"
