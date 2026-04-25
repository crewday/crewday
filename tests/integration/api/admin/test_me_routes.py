"""Integration tests for the cd-yj4k admin-me routes.

Boots the full :func:`app.api.factory.create_app` against the
integration harness's DB and drives ``GET /admin/api/v1/me`` and
``GET /admin/api/v1/me/admins`` end-to-end. Verifies the routes
ride through every middleware (CORS, security headers, workspace
tenancy skip-paths, idempotency, CSRF) and that the SKIP_PATHS
contract holds — ``/admin/api/v1/...`` does NOT try to resolve a
workspace slug.

The sibling :mod:`tests.unit.api.admin.test_me` exercises the
response shape and filtering invariants. This module's job is to
prove the production wiring (factory → middleware → router → dep
→ handler) doesn't drop anything along the way.

See ``docs/specs/12-rest-api.md`` §"Admin surface".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.identity.models import ApiToken, User
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.main import create_app
from app.tenancy import (
    DEPLOYMENT_SCOPE_CATALOG,
    tenant_agnostic,
)
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

pytestmark = pytest.mark.integration


_TEST_UA = "pytest-admin-me-integration"
_TEST_ACCEPT_LANGUAGE = "en"
_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pinned_settings(db_url: str) -> Settings:
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-admin-me-root-key"),
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
    """Point the process-wide UoW at the integration engine."""
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_admin(
    session_factory: sessionmaker[Session],
    *,
    email: str,
    display_name: str,
) -> str:
    """Seed a user + active deployment grant; return the user id."""
    with session_factory() as s:
        user = bootstrap_user(s, email=email, display_name=display_name)
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
        s.commit()
        return user.id


def _seed_user(
    session_factory: sessionmaker[Session],
    *,
    email: str,
    display_name: str,
) -> str:
    with session_factory() as s:
        user = bootstrap_user(s, email=email, display_name=display_name)
        s.commit()
        return user.id


def _issue_session(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    settings: Settings,
) -> str:
    with session_factory() as s:
        result = issue(
            s,
            user_id=user_id,
            has_owner_grant=False,
            ua=_TEST_UA,
            ip="127.0.0.1",
            accept_language=_TEST_ACCEPT_LANGUAGE,
            settings=settings,
        )
        s.commit()
        return result.cookie_value


def _wipe(session_factory: sessionmaker[Session]) -> None:
    """Sweep rows committed during a test so siblings see a clean slate."""
    with session_factory() as s, tenant_agnostic():
        for model in (
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAdminMeRoute:
    """``GET /admin/api/v1/me`` end-to-end through the production factory."""

    def test_session_admin_returns_payload(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        try:
            user_id = _seed_admin(
                session_factory,
                email="ada@example.com",
                display_name="Ada Lovelace",
            )
            cookie_value = _issue_session(
                session_factory, user_id=user_id, settings=pinned_settings
            )
            client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

            resp = client.get("/admin/api/v1/me")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["user_id"] == user_id
            assert body["display_name"] == "Ada Lovelace"
            assert body["email"] == "ada@example.com"
            assert body["is_owner"] is False
            assert set(body["capabilities"].keys()) == DEPLOYMENT_SCOPE_CATALOG
        finally:
            _wipe(session_factory)

    def test_no_auth_returns_404(self, client: TestClient) -> None:
        resp = client.get("/admin/api/v1/me")
        assert resp.status_code == 404, resp.text
        assert resp.json().get("error") == "not_found"

    def test_non_admin_session_returns_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        try:
            user_id = _seed_user(
                session_factory,
                email="plain@example.com",
                display_name="Plain User",
            )
            cookie_value = _issue_session(
                session_factory, user_id=user_id, settings=pinned_settings
            )
            client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

            resp = client.get("/admin/api/v1/me")
            assert resp.status_code == 404, resp.text
            assert resp.json().get("error") == "not_found"
        finally:
            _wipe(session_factory)


class TestAdminMeAdminsRoute:
    """``GET /admin/api/v1/me/admins`` end-to-end through the production factory."""

    def test_session_admin_lists_team(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        try:
            user_id = _seed_admin(
                session_factory,
                email="ada@example.com",
                display_name="Ada Lovelace",
            )
            cookie_value = _issue_session(
                session_factory, user_id=user_id, settings=pinned_settings
            )
            client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

            resp = client.get("/admin/api/v1/me/admins")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert len(body["admins"]) == 1
            row = body["admins"][0]
            assert row["user_id"] == user_id
            assert row["display_name"] == "Ada Lovelace"
            assert row["email"] == "ada@example.com"
            assert row["is_owner"] is False
            assert row["granted_by"] == "system"
            assert row["granted_at"].endswith("+00:00")
            assert body["groups"] == []
        finally:
            _wipe(session_factory)

    def test_non_admin_session_returns_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        try:
            user_id = _seed_user(
                session_factory,
                email="plain@example.com",
                display_name="Plain User",
            )
            cookie_value = _issue_session(
                session_factory, user_id=user_id, settings=pinned_settings
            )
            client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

            resp = client.get("/admin/api/v1/me/admins")
            assert resp.status_code == 404, resp.text
            assert resp.json().get("error") == "not_found"
        finally:
            _wipe(session_factory)
