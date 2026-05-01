"""Integration tests for :mod:`app.auth.signup_abuse` wired into ``POST /signup/start``.

Drives the real FastAPI router with a stub mailer, the real
:class:`app.auth._throttle.Throttle` in-memory backend, and the real
signup domain service against a SQLite engine. Each test exercises
one refusal path end-to-end:

* Rate limit → ``429 rate_limited`` with ``Retry-After``.
* Disposable email → ``422 disposable_email``; no signup_attempt
  row inserted.
* Missing CAPTCHA when required → ``422 captcha_required``.
* ``captcha_required=False`` capability → accepts no token.
* ``test-fail`` token → ``422 captcha_failed``.
* Happy path still threads through (sanity).

Covers the audit-row shape too: every refusal lands an
``audit.signup.*`` row with only hashes + reason symbol.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.identity.models import MagicLinkNonce, SignupAttempt
from app.api.deps import db_session as _db_session_dep
from app.api.v1.auth.signup import build_signup_router
from app.auth import signup_abuse
from app.auth._throttle import _SIGNUP_IP_LIMIT, Throttle
from app.capabilities import Capabilities, DeploymentSettings, Features
from app.config import Settings

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _RecordingMailer:
    sent: list[tuple[tuple[str, ...], str, str]] = field(default_factory=list)

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
        del body_html, headers, reply_to
        self.sent.append((tuple(to), subject, body_text))
        return "test-message-id"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    # No Turnstile secret → offline test mode; the abuse module accepts
    # the fixed "test-pass" / rejects "test-fail".
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-root-key"),
        public_url="https://crew.day",
        captcha_turnstile_secret=None,
    )


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def capabilities_captcha_required() -> Capabilities:
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
        settings=DeploymentSettings(signup_enabled=True, captcha_required=True),
    )


@pytest.fixture
def capabilities_captcha_optional() -> Capabilities:
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
def mailer() -> _RecordingMailer:
    return _RecordingMailer()


def _make_client(
    engine: Engine,
    *,
    mailer: _RecordingMailer,
    throttle: Throttle,
    capabilities: Capabilities,
    settings: Settings,
) -> Iterator[TestClient]:
    """Build a :class:`TestClient` mounted on the signup router.

    Mirrors the ``client`` fixture in
    :mod:`tests.integration.auth.test_signup_full_flow` so each HTTP
    request opens + commits its own session against
    ``engine``. Patches :func:`app.adapters.db.session._default_sessionmaker`
    to the same engine so refusal-path audit rows (written on a
    fresh :func:`make_uow`) land on the same DB the test reads from.
    """
    import app.adapters.db.session as _session_mod

    app_obj = FastAPI()
    router = build_signup_router(
        mailer=mailer,
        throttle=throttle,
        capabilities=capabilities,
        base_url="https://crew.day",
        settings=settings,
    )
    app_obj.include_router(router, prefix="/api/v1")

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

    app_obj.dependency_overrides[_db_session_dep] = _session

    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = factory
    try:
        with TestClient(app_obj) as c:
            yield c
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory

    # Clean up committed rows so sibling integration tests see a
    # clean slate. Strictly scoped to the tables this suite touches.
    with factory() as s:
        for nonce in s.scalars(select(MagicLinkNonce)).all():
            s.delete(nonce)
        for attempt in s.scalars(select(SignupAttempt)).all():
            s.delete(attempt)
        for audit in s.scalars(select(AuditLog)).all():
            s.delete(audit)
        s.commit()


@pytest.fixture
def client_captcha_required(
    engine: Engine,
    mailer: _RecordingMailer,
    throttle: Throttle,
    capabilities_captcha_required: Capabilities,
    settings: Settings,
) -> Iterator[TestClient]:
    yield from _make_client(
        engine,
        mailer=mailer,
        throttle=throttle,
        capabilities=capabilities_captcha_required,
        settings=settings,
    )


@pytest.fixture
def client_captcha_optional(
    engine: Engine,
    mailer: _RecordingMailer,
    throttle: Throttle,
    capabilities_captcha_optional: Capabilities,
    settings: Settings,
) -> Iterator[TestClient]:
    yield from _make_client(
        engine,
        mailer=mailer,
        throttle=throttle,
        capabilities=capabilities_captcha_optional,
        settings=settings,
    )


@pytest.fixture(autouse=True)
def _reset_disposable_cache() -> Iterator[None]:
    """Ensure each test sees the bundled blocklist via a fresh cache."""
    signup_abuse.reload_disposable_domains()
    yield
    signup_abuse.reload_disposable_domains()


# ---------------------------------------------------------------------------
# Rate limit (429 + Retry-After)
# ---------------------------------------------------------------------------


class TestRateLimitRefusal:
    def test_burst_ip_returns_429_with_retry_after(
        self,
        client_captcha_required: TestClient,
        engine: Engine,
    ) -> None:
        # Burn the per-IP budget with distinct emails + distinct slugs
        # so no other gate trips first. Each successful start also
        # advances the global bucket; the per-IP bucket trips first
        # because _SIGNUP_IP_LIMIT < _SIGNUP_GLOBAL_LIMIT.
        for idx in range(_SIGNUP_IP_LIMIT):
            r = client_captcha_required.post(
                "/api/v1/signup/start",
                json={
                    "email": f"rl-{idx}@example.com",
                    "desired_slug": f"rl-ws-{idx}",
                    "captcha_token": "test-pass",
                },
            )
            assert r.status_code == 202, r.text

        # Next request from the same IP trips the per-IP bucket.
        r = client_captcha_required.post(
            "/api/v1/signup/start",
            json={
                "email": "rl-over@example.com",
                "desired_slug": "rl-over-ws",
                "captcha_token": "test-pass",
            },
        )
        assert r.status_code == 429, r.text
        assert r.json()["detail"]["error"] == "rate_limited"
        retry_after = int(r.headers["retry-after"])
        assert retry_after > 0

        # Audit row landed with hashes only (no raw IP / email).
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            rows = s.scalars(
                select(AuditLog).where(AuditLog.action == "audit.signup.rate_limited")
            ).all()
            assert rows, "expected at least one rate_limited audit row"
            diff = rows[-1].diff
            assert isinstance(diff, dict)
            assert diff["reason"].startswith("rate_limited:")
            assert "ip_hash" in diff
            assert "email_hash" in diff
            # PII guard: no raw address or IP anywhere in the payload.
            blob = repr(diff)
            assert "rl-over@example.com" not in blob
            assert "127.0.0.1" not in blob


# ---------------------------------------------------------------------------
# Disposable email (422 disposable_email)
# ---------------------------------------------------------------------------


class TestDisposableRefusal:
    def test_disposable_email_returns_422(
        self,
        client_captcha_required: TestClient,
        engine: Engine,
    ) -> None:
        r = client_captcha_required.post(
            "/api/v1/signup/start",
            json={
                "email": "abuser@mailinator.com",
                "desired_slug": "burner-ws",
                "captcha_token": "test-pass",
            },
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "disposable_email"

        # No signup_attempt row inserted — refusal fired before the
        # domain service.
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            attempts = s.scalars(
                select(SignupAttempt).where(
                    SignupAttempt.email_lower == "abuser@mailinator.com"
                )
            ).all()
            assert attempts == []

            # Audit refusal with hashes only.
            rows = s.scalars(
                select(AuditLog).where(
                    AuditLog.action == "audit.signup.disposable_email"
                )
            ).all()
            assert rows
            diff = rows[0].diff
            assert isinstance(diff, dict)
            assert diff["reason"].startswith("disposable_email:")
            assert "mailinator.com" in diff["reason"]
            assert "abuser@" not in repr(diff)


# ---------------------------------------------------------------------------
# CAPTCHA gate
# ---------------------------------------------------------------------------


class TestCaptchaRefusal:
    def test_missing_captcha_token_when_required(
        self,
        client_captcha_required: TestClient,
    ) -> None:
        r = client_captcha_required.post(
            "/api/v1/signup/start",
            json={
                "email": "nocap@example.com",
                "desired_slug": "nocap-ws",
            },
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "captcha_required"

    def test_test_fail_token_rejected(
        self,
        client_captcha_required: TestClient,
        engine: Engine,
    ) -> None:
        r = client_captcha_required.post(
            "/api/v1/signup/start",
            json={
                "email": "bad-cap@example.com",
                "desired_slug": "bad-cap-ws",
                "captcha_token": "test-fail",
            },
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "captcha_failed"

        # Audit refusal reason = captcha_rejected (upstream rejection).
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            rows = s.scalars(
                select(AuditLog).where(AuditLog.action == "audit.signup.captcha_failed")
            ).all()
            assert rows
            assert rows[0].diff["reason"] == "captcha_rejected"

    def test_optional_captcha_accepts_no_token(
        self,
        client_captcha_optional: TestClient,
        mailer: _RecordingMailer,
    ) -> None:
        """``captcha_required=False`` self-host capability skips the gate entirely."""
        r = client_captcha_optional.post(
            "/api/v1/signup/start",
            json={
                "email": "selfhost@example.com",
                "desired_slug": "selfhost-ws",
            },
        )
        assert r.status_code == 202, r.text
        assert len(mailer.sent) == 1


# ---------------------------------------------------------------------------
# Happy path — sanity
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_valid_request_threads_through(
        self,
        client_captcha_required: TestClient,
        engine: Engine,
        mailer: _RecordingMailer,
    ) -> None:
        r = client_captcha_required.post(
            "/api/v1/signup/start",
            json={
                "email": "happy@example.com",
                "desired_slug": "happy-ws",
                "captcha_token": "test-pass",
            },
        )
        assert r.status_code == 202, r.text
        assert len(mailer.sent) == 1

        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as s:
            attempts = s.scalars(
                select(SignupAttempt).where(
                    SignupAttempt.email_lower == "happy@example.com"
                )
            ).all()
            assert len(attempts) == 1
            assert attempts[0].desired_slug == "happy-ws"
