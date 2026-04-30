"""Unit tests for the app-shell ``/api/v1/me`` profile router."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db import audit, authz, billing, identity, workspace  # noqa: F401
from app.adapters.db.authz.models import DeploymentOwner, RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.billing.models import Organization
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.session import make_engine
from app.api.deps import db_session as db_session_dep
from app.api.v1.auth import me as me_module
from app.auth.session import SESSION_COOKIE_NAME, hash_cookie_value, issue
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.clock import SystemClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

_TEST_UA = "pytest-me-profile"
_TEST_ACCEPT_LANGUAGE = "en"


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-me-profile-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    app = FastAPI()
    app.include_router(me_module.build_me_profile_router(), prefix="/api/v1")
    app.include_router(
        me_module.build_me_profile_router(operation_id="me.profile.scoped.get"),
        prefix="/w/{slug}/api/v1",
    )

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

    with TestClient(
        app,
        base_url="https://testserver",
        headers={
            "User-Agent": _TEST_UA,
            "Accept-Language": _TEST_ACCEPT_LANGUAGE,
        },
    ) as c:
        yield c


def _seed_user_workspace(
    session_factory: sessionmaker[Session],
) -> tuple[str, str, str]:
    with session_factory() as s:
        user = bootstrap_user(
            s,
            email="manager-me-profile@example.test",
            display_name="Maria Manager",
            clock=SystemClock(),
        )
        workspace = bootstrap_workspace(
            s,
            slug="smoke",
            name="Smoke",
            owner_user_id=user.id,
            clock=SystemClock(),
        )
        s.commit()
        return user.id, workspace.id, workspace.slug


def _issue_cookie(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    workspace_id: str,
    settings: Settings,
) -> str:
    with session_factory() as s:
        result = issue(
            s,
            user_id=user_id,
            workspace_id=workspace_id,
            has_owner_grant=True,
            ua=_TEST_UA,
            ip="127.0.0.1",
            accept_language=_TEST_ACCEPT_LANGUAGE,
            settings=settings,
        )
        s.commit()
        return result.cookie_value


def test_no_cookie_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/me")

    assert response.status_code == 401, response.text
    assert response.json()["detail"]["error"] == "session_required"


def test_bare_me_returns_shell_profile(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    user_id, workspace_id, _slug = _seed_user_workspace(session_factory)
    cookie = _issue_cookie(
        session_factory,
        user_id=user_id,
        workspace_id=workspace_id,
        settings=settings,
    )
    client.cookies.set(SESSION_COOKIE_NAME, cookie)

    response = client.get("/api/v1/me")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_id"] == user_id
    assert body["role"] == "manager"
    assert body["manager_name"] == "Maria Manager"
    assert body["employee"]["name"] == "Maria Manager"
    assert body["employee"]["avatar_initials"] == "MM"
    assert body["current_workspace_id"] == workspace_id
    assert body["available_workspaces"][0]["workspace"]["id"] == "smoke"
    assert body["is_deployment_admin"] is False
    assert body["is_deployment_owner"] is False


def test_me_profile_reports_deployment_owner(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    user_id, workspace_id, _slug = _seed_user_workspace(session_factory)
    with session_factory() as s, tenant_agnostic():
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=None,
                user_id=user_id,
                grant_role="manager",
                scope_kind="deployment",
                created_at=SystemClock().now(),
                created_by_user_id=None,
            )
        )
        s.add(
            DeploymentOwner(
                user_id=user_id,
                added_at=SystemClock().now(),
                added_by_user_id=None,
            )
        )
        s.commit()
    cookie = _issue_cookie(
        session_factory,
        user_id=user_id,
        workspace_id=workspace_id,
        settings=settings,
    )
    client.cookies.set(SESSION_COOKIE_NAME, cookie)

    response = client.get("/api/v1/me")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_deployment_admin"] is True
    assert body["is_deployment_owner"] is True


def test_scoped_me_alias_returns_same_profile(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    user_id, workspace_id, slug = _seed_user_workspace(session_factory)
    cookie = _issue_cookie(
        session_factory,
        user_id=user_id,
        workspace_id=workspace_id,
        settings=settings,
    )
    client.cookies.set(SESSION_COOKIE_NAME, cookie)

    response = client.get(f"/w/{slug}/api/v1/me")

    assert response.status_code == 200, response.text
    assert response.json()["current_workspace_id"] == workspace_id


def test_me_profile_returns_client_binding_org_ids(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    user_id, workspace_id, _slug = _seed_user_workspace(session_factory)
    org_id = new_ulid()
    with session_factory() as s:
        s.add(
            Organization(
                id=org_id,
                workspace_id=workspace_id,
                kind="client",
                display_name="Client Org",
                billing_address={},
                tax_id=None,
                default_currency="EUR",
                contact_email=None,
                contact_phone=None,
                notes_md=None,
                created_at=SystemClock().now(),
                archived_at=None,
            )
        )
        s.flush()
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user_id,
                grant_role="client",
                scope_kind="workspace",
                scope_property_id=None,
                binding_org_id=org_id,
                created_at=SystemClock().now(),
                created_by_user_id=None,
            )
        )
        s.commit()
    cookie = _issue_cookie(
        session_factory,
        user_id=user_id,
        workspace_id=workspace_id,
        settings=settings,
    )
    client.cookies.set(SESSION_COOKIE_NAME, cookie)

    response = client.get("/api/v1/me")

    assert response.status_code == 200, response.text
    assert response.json()["client_binding_org_ids"] == [org_id]


def test_me_profile_does_not_touch_session_last_seen(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> None:
    user_id, workspace_id, _slug = _seed_user_workspace(session_factory)
    cookie = _issue_cookie(
        session_factory,
        user_id=user_id,
        workspace_id=workspace_id,
        settings=settings,
    )
    session_id = hash_cookie_value(cookie)
    with session_factory() as s, tenant_agnostic():
        before = s.get(SessionRow, session_id)
        assert before is not None
        before_seen = before.last_seen_at

    client.cookies.set(SESSION_COOKIE_NAME, cookie)
    response = client.get("/api/v1/me")

    assert response.status_code == 200, response.text
    with session_factory() as s, tenant_agnostic():
        after = s.get(SessionRow, session_id)
        assert after is not None
        assert after.last_seen_at == before_seen
