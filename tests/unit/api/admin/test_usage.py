"""Unit tests for :mod:`app.api.admin.usage`.

Covers ``GET /usage/summary`` + ``GET /usage/workspaces`` +
``PUT /usage/workspaces/{id}/cap`` per spec §12 "Admin surface"
§"Usage aggregates":

* Summary aggregates over the rolling 30 d window.
* Workspaces table joins per-workspace cap to LLM spend.
* Cap PUT writes :attr:`Workspace.quota_json` and audits;
  idempotent re-call; 422 on negative cap.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.llm.models import LlmUsage
from app.adapters.db.workspace.models import Workspace
from app.auth.session import SESSION_COOKIE_NAME
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.unit.api.admin._helpers import (
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
    return settings_fixture("usage")


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


def _admin_cookie(
    session_factory: sessionmaker[Session], settings: Settings
) -> str:
    with session_factory() as s:
        user_id = seed_user(s, email="ada@example.com", display_name="Ada")
        grant_deployment_admin(s, user_id=user_id)
        s.commit()
    return issue_session(session_factory, user_id=user_id, settings=settings)


def _add_llm_usage(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    capability: str,
    cost_cents: int,
    created_at: datetime,
) -> None:
    with session_factory() as s, tenant_agnostic():
        s.add(
            LlmUsage(
                id=new_ulid(),
                workspace_id=workspace_id,
                capability=capability,
                model_id="01HW00000000000000000MD01",
                tokens_in=1000,
                tokens_out=500,
                cost_cents=cost_cents,
                latency_ms=100,
                status="ok",
                correlation_id=new_ulid(),
                attempt=0,
                created_at=created_at,
            )
        )
        s.commit()


class TestUsageSummary:
    def test_aggregates_inside_window(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws_a = seed_workspace(
                s, slug="ws-a", quota_json={"llm_budget_cents_30d": 1000}
            )
            ws_b = seed_workspace(
                s, slug="ws-b", quota_json={"llm_budget_cents_30d": 500}
            )
            s.commit()
        now = datetime.now(UTC)
        _add_llm_usage(
            session_factory,
            workspace_id=ws_a,
            capability="chat.manager",
            cost_cents=200,
            created_at=now - timedelta(days=2),
        )
        _add_llm_usage(
            session_factory,
            workspace_id=ws_b,
            capability="chat.manager",
            cost_cents=600,  # over cap → paused
            created_at=now - timedelta(days=1),
        )
        # Out-of-window — must NOT be counted.
        _add_llm_usage(
            session_factory,
            workspace_id=ws_a,
            capability="chat.manager",
            cost_cents=999,
            created_at=now - timedelta(days=45),
        )
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        body = client.get("/admin/api/v1/usage/summary").json()
        assert body["deployment_calls_30d"] == 2
        assert body["deployment_spend_cents_30d"] == 800
        assert body["workspace_count"] == 2
        # ws_b has spend 600 ≥ cap 500 → paused.
        assert body["paused_workspace_count"] == 1
        per_capability = {row["capability"]: row for row in body["per_capability"]}
        assert per_capability["chat.manager"]["calls_30d"] == 2
        assert per_capability["chat.manager"]["spend_cents_30d"] == 800


class TestUsageWorkspaces:
    def test_lists_each_workspace_with_resolved_cap(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws_capped = seed_workspace(
                s, slug="capped", quota_json={"llm_budget_cents_30d": 200}
            )
            ws_default = seed_workspace(s, slug="defaulted")
            s.commit()
        now = datetime.now(UTC)
        _add_llm_usage(
            session_factory,
            workspace_id=ws_capped,
            capability="chat.manager",
            cost_cents=199,
            created_at=now - timedelta(hours=1),
        )
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        body = client.get("/admin/api/v1/usage/workspaces").json()
        rows = {row["workspace_id"]: row for row in body["workspaces"]}
        assert rows[ws_capped]["cap_cents_30d"] == 200
        assert rows[ws_capped]["spent_cents_30d"] == 199
        assert rows[ws_capped]["percent"] == 99
        assert rows[ws_capped]["paused"] is False
        # Default cap from DeploymentSettings (500 cents).
        assert rows[ws_default]["cap_cents_30d"] == 500
        assert rows[ws_default]["spent_cents_30d"] == 0


class TestUpdateCap:
    def test_writes_cap_and_audits(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws = seed_workspace(s, slug="cap-ws", quota_json={})
            s.commit()
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.put(
            f"/admin/api/v1/usage/workspaces/{ws}/cap",
            json={"cap_cents_30d": 1500},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"workspace_id": ws, "cap_cents_30d": 1500}
        with session_factory() as s, tenant_agnostic():
            row = s.get(Workspace, ws)
            assert row is not None
            assert row.quota_json["llm_budget_cents_30d"] == 1500
            audits = s.scalars(
                select(AuditLog).where(AuditLog.action == "usage.cap_updated")
            ).all()
            assert len(audits) == 1

    def test_idempotent_no_extra_audit(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws = seed_workspace(
                s, slug="cap-ws", quota_json={"llm_budget_cents_30d": 1500}
            )
            s.commit()
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.put(
            f"/admin/api/v1/usage/workspaces/{ws}/cap",
            json={"cap_cents_30d": 1500},
        )
        assert resp.status_code == 200
        with session_factory() as s, tenant_agnostic():
            audits = s.scalars(
                select(AuditLog).where(AuditLog.action == "usage.cap_updated")
            ).all()
            assert audits == []

    def test_negative_cap_returns_typed_422(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws = seed_workspace(s, slug="cap-ws")
            s.commit()
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.put(
            f"/admin/api/v1/usage/workspaces/{ws}/cap",
            json={"cap_cents_30d": -1},
        )
        assert resp.status_code == 422
        assert resp.json().get("error") == "invalid_cap"

    def test_unknown_workspace_404s(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.put(
            "/admin/api/v1/usage/workspaces/01HBOGUS00000000000000000/cap",
            json={"cap_cents_30d": 100},
        )
        assert resp.status_code == 404
