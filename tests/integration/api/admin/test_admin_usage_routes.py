"""Integration tests for the cd-ccu9 ``GET /admin/api/v1/usage`` route.

Boots :func:`app.api.factory.create_app` against the integration
harness and drives the cd-ccu9 raw-usage feed end-to-end through
the production middleware stack. Sibling
:mod:`tests.unit.api.admin.test_usage` carries the narrower
per-filter response-shape contracts; this module proves the
production wiring (factory → middleware → router → dep → handler)
surfaces the cd-wjpl telemetry columns end-to-end.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import DeploymentOwner, RoleGrant
from app.adapters.db.capabilities.models import DeploymentSetting
from app.adapters.db.identity.models import ApiToken, User
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.llm.models import LlmUsage
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.main import create_app
from app.tenancy import tenant_agnostic
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

pytestmark = pytest.mark.integration


_TEST_UA = "pytest-admin-cd-ccu9-integration"
_TEST_ACCEPT_LANGUAGE = "en"
_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def pinned_settings(db_url: str) -> Settings:
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-admin-cd-ccu9-root-key"),
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
    owner: bool = False,
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
            if owner:
                s.add(
                    DeploymentOwner(
                        user_id=user.id,
                        added_at=_PINNED,
                        added_by_user_id=None,
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


def _add_llm_usage(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    capability: str,
    cost_cents: int,
    created_at: datetime,
    status: str = "ok",
    actor_user_id: str | None = None,
    token_id: str | None = None,
    agent_label: str | None = None,
    assignment_id: str | None = None,
    fallback_attempts: int = 0,
    finish_reason: str | None = None,
) -> str:
    inserted_id = new_ulid()
    with session_factory() as s, tenant_agnostic():
        s.add(
            LlmUsage(
                id=inserted_id,
                workspace_id=workspace_id,
                capability=capability,
                provider_model_id="01HW00000000000000000MD01",
                tokens_in=1000,
                tokens_out=500,
                cost_cents=cost_cents,
                latency_ms=100,
                status=status,
                correlation_id=new_ulid(),
                attempt=0,
                assignment_id=assignment_id,
                fallback_attempts=fallback_attempts,
                finish_reason=finish_reason,
                actor_user_id=actor_user_id,
                token_id=token_id,
                agent_label=agent_label,
                created_at=created_at,
            )
        )
        s.commit()
    return inserted_id


def _wipe(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s, tenant_agnostic():
        for model in (
            LlmUsage,
            ApiToken,
            SessionRow,
            UserWorkspace,
            DeploymentOwner,
            RoleGrant,
            AuditLog,
            DeploymentSetting,
            Workspace,
            User,
        ):
            for row in s.scalars(select(model)).all():
                s.delete(row)
        s.commit()


class TestUsageListRoute:
    def test_surfaces_cd_wjpl_columns_via_production_factory(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        try:
            user_id = _seed_admin(
                session_factory,
                email="ada@example.com",
                display_name="Ada",
            )
            ws = _seed_workspace(session_factory, slug="usage-list-prod")
            now = datetime.now(UTC)
            actor_id = "01HW0000000000000000ACTRP1"
            _add_llm_usage(
                session_factory,
                workspace_id=ws,
                capability="chat.manager",
                cost_cents=42,
                created_at=now - timedelta(minutes=5),
                actor_user_id=actor_id,
                token_id="01HW0000000000000000TOKNP1",
                agent_label="manager-chat",
                assignment_id="01HW0000000000000000ASSNP1",
                fallback_attempts=1,
                finish_reason="stop",
            )
            cookie = _issue_session(
                session_factory, user_id=user_id, settings=pinned_settings
            )
            client.cookies.set(SESSION_COOKIE_NAME, cookie)

            resp = client.get("/admin/api/v1/usage")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["has_more"] is False
            assert len(body["data"]) == 1
            row = body["data"][0]
            assert row["workspace_id"] == ws
            assert row["actor_user_id"] == actor_id
            assert row["agent_label"] == "manager-chat"
            assert row["fallback_attempts"] == 1
            assert row["finish_reason"] == "stop"
            assert row["provider_model_id"] == "01HW00000000000000000MD01"
            # cd-v6dj rename: surface provider_model_id, not legacy
            # ``model_id``.
            assert "model_id" not in row

            # Filter combinations exercise the composite indexes
            # end-to-end (actor_user_id rides
            # ix_llm_usage_workspace_actor_created; capability rides
            # ix_llm_usage_workspace_capability_created).
            actor_resp = client.get(
                "/admin/api/v1/usage",
                params={"actor_user_id": actor_id},
            )
            assert actor_resp.status_code == 200
            assert len(actor_resp.json()["data"]) == 1

            cap_resp = client.get(
                "/admin/api/v1/usage",
                params={"capability": "chat.manager"},
            )
            assert cap_resp.status_code == 200
            assert len(cap_resp.json()["data"]) == 1

            ws_resp = client.get(
                "/admin/api/v1/usage",
                params={"workspace_id": ws, "status": "success"},
            )
            assert ws_resp.status_code == 200
            assert len(ws_resp.json()["data"]) == 1
        finally:
            _wipe(session_factory)
