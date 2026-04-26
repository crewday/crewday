"""Integration tests for the cd-jlms workspace-admin routes.

Boots :func:`app.api.factory.create_app` against the integration
harness's DB and drives each new admin family end-to-end through
the production middleware stack (CORS, security headers, tenancy
SKIP_PATHS, idempotency, CSRF). Verifies the admin tree at
``/admin/api/v1/...`` does NOT trip the workspace tenancy
middleware's slug resolver.

Sibling :mod:`tests.unit.api.admin` modules carry the
narrower per-route response-shape contracts; this module only
proves the production wiring (factory → middleware → router →
dep → handler) doesn't drop anything.
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
from app.adapters.db.capabilities.models import DeploymentSetting
from app.adapters.db.identity.models import ApiToken, User
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.main import create_app
from app.tenancy import tenant_agnostic
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

pytestmark = pytest.mark.integration


_TEST_UA = "pytest-admin-cd-jlms-integration"
_TEST_ACCEPT_LANGUAGE = "en"
_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def pinned_settings(db_url: str) -> Settings:
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-admin-cd-jlms-root-key"),
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
    email: str,
    display_name: str,
) -> str:
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


def _seed_workspace(session_factory: sessionmaker[Session], *, slug: str) -> str:
    workspace_id = new_ulid()
    with session_factory() as s, tenant_agnostic():
        s.add(
            Workspace(
                id=workspace_id,
                slug=slug,
                name=slug.title(),
                plan="free",
                quota_json={},
                created_at=_PINNED,
            )
        )
        s.commit()
    return workspace_id


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
    with session_factory() as s, tenant_agnostic():
        for model in (
            ApiToken,
            SessionRow,
            UserWorkspace,
            RoleGrant,
            AuditLog,
            DeploymentSetting,
            Workspace,
            User,
        ):
            for row in s.scalars(select(model)).all():
                s.delete(row)
        s.commit()


class TestWorkspaceListAndSummary:
    def test_admin_can_list_and_get_workspace_via_production_factory(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        try:
            user_id = _seed_admin(
                session_factory, email="ada@example.com", display_name="Ada"
            )
            ws = _seed_workspace(session_factory, slug="prod-ws")
            cookie = _issue_session(
                session_factory, user_id=user_id, settings=pinned_settings
            )
            client.cookies.set(SESSION_COOKIE_NAME, cookie)

            list_resp = client.get("/admin/api/v1/workspaces")
            assert list_resp.status_code == 200, list_resp.text
            assert ws in [row["id"] for row in list_resp.json()["workspaces"]]

            get_resp = client.get(f"/admin/api/v1/workspaces/{ws}")
            assert get_resp.status_code == 200, get_resp.text
            assert get_resp.json()["id"] == ws

            trust_resp = client.post(f"/admin/api/v1/workspaces/{ws}/trust")
            assert trust_resp.status_code == 200, trust_resp.text
            assert trust_resp.json()["verification_state"] == "trusted"

            archive_resp = client.post(f"/admin/api/v1/workspaces/{ws}/archive")
            # cd-zkr deferred → owner gate 404s.
            assert archive_resp.status_code == 404
        finally:
            _wipe(session_factory)


class TestSettingsAndSignup:
    def test_settings_and_signup_routes_reachable(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        try:
            user_id = _seed_admin(
                session_factory, email="ada@example.com", display_name="Ada"
            )
            cookie = _issue_session(
                session_factory, user_id=user_id, settings=pinned_settings
            )
            client.cookies.set(SESSION_COOKIE_NAME, cookie)

            settings_resp = client.get("/admin/api/v1/settings")
            assert settings_resp.status_code == 200
            assert any(
                row["key"] == "trusted_interfaces"
                for row in settings_resp.json()["settings"]
            )

            signup_resp = client.get("/admin/api/v1/signup/settings")
            assert signup_resp.status_code == 200
            assert signup_resp.json()["signup_enabled"] is True

            signup_put = client.put(
                "/admin/api/v1/signup/settings",
                json={"signup_enabled": False},
            )
            assert signup_put.status_code == 200, signup_put.text
            assert signup_put.json()["signup_enabled"] is False

            # Root-only key refuses with the typed envelope.
            root_only = client.put(
                "/admin/api/v1/settings/trusted_interfaces",
                json={"value": ["lo"]},
            )
            assert root_only.status_code == 422
            assert root_only.json().get("error") == "root_only_setting"
        finally:
            _wipe(session_factory)


class TestAdminsAndUsageAndAudit:
    def test_admins_grant_revoke_usage_audit_via_production_factory(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        try:
            user_id = _seed_admin(
                session_factory, email="ada@example.com", display_name="Ada"
            )
            ws = _seed_workspace(session_factory, slug="usage-ws")
            cookie = _issue_session(
                session_factory, user_id=user_id, settings=pinned_settings
            )
            client.cookies.set(SESSION_COOKIE_NAME, cookie)

            # Grant a new admin via email, then revoke them.
            with session_factory() as s:
                bootstrap_user(s, email="b@example.com", display_name="B")
                s.commit()
            grant_resp = client.post(
                "/admin/api/v1/admins",
                json={"email": "b@example.com"},
            )
            assert grant_resp.status_code == 200, grant_resp.text
            grant_id = grant_resp.json()["admin"]["id"]
            revoke_resp = client.post(f"/admin/api/v1/admins/{grant_id}/revoke")
            assert revoke_resp.status_code == 200

            # Usage feed reachable.
            summary_resp = client.get("/admin/api/v1/usage/summary")
            assert summary_resp.status_code == 200
            workspaces_resp = client.get("/admin/api/v1/usage/workspaces")
            assert workspaces_resp.status_code == 200
            assert ws in [
                row["workspace_id"] for row in workspaces_resp.json()["workspaces"]
            ]

            # Cap PUT.
            cap_resp = client.put(
                f"/admin/api/v1/usage/workspaces/{ws}/cap",
                json={"cap_cents_30d": 750},
            )
            assert cap_resp.status_code == 200

            # Audit feed surfaces every mutation we just made.
            audit_resp = client.get("/admin/api/v1/audit")
            assert audit_resp.status_code == 200
            actions = {row["action"] for row in audit_resp.json()["data"]}
            assert {
                "admin.granted",
                "admin.revoked",
                "usage.cap_updated",
            } <= actions
        finally:
            _wipe(session_factory)


class TestAuthGate:
    def test_no_auth_returns_404_on_every_new_route(
        self,
        client: TestClient,
    ) -> None:
        for path in (
            "/admin/api/v1/workspaces",
            "/admin/api/v1/workspaces/01HBOGUS00000000000000000",
            "/admin/api/v1/signup/settings",
            "/admin/api/v1/settings",
            "/admin/api/v1/admins",
            "/admin/api/v1/admins/groups",
            "/admin/api/v1/audit",
            "/admin/api/v1/audit/tail",
            "/admin/api/v1/usage/summary",
            "/admin/api/v1/usage/workspaces",
        ):
            resp = client.get(path)
            assert resp.status_code == 404, f"{path}: {resp.text}"
            assert resp.json().get("error") == "not_found"


class TestOpenApiContract:
    """Pin the spec invariant: admin operation ids stay unique.

    Cd-jlms acceptance criterion: ``operation_id`` and
    ``x-cli.group`` align with the existing ``cd-yj4k`` routes.
    A duplicate id (e.g. ``admin.admins.list`` shipped by both
    ``GET /admin/api/v1/me/admins`` and ``GET /admin/api/v1/admins``)
    triggers a FastAPI :class:`UserWarning` and breaks generated
    OpenAPI clients + the ``/update-openapi`` Schemathesis run.
    """

    def test_admin_tree_operation_ids_are_unique(
        self,
        client: TestClient,
    ) -> None:
        schema = client.app.openapi()  # type: ignore[attr-defined]
        seen: dict[str, str] = {}
        collisions: list[tuple[str, str, str]] = []
        for path, item in schema["paths"].items():
            if not path.startswith("/admin/api/v1"):
                continue
            for method, op in item.items():
                if not isinstance(op, dict):
                    continue
                op_id = op.get("operationId")
                if not isinstance(op_id, str):
                    continue
                if op_id in seen:
                    collisions.append((op_id, seen[op_id], f"{method} {path}"))
                else:
                    seen[op_id] = f"{method} {path}"
        assert collisions == [], f"Duplicate admin operation ids: {collisions}"
