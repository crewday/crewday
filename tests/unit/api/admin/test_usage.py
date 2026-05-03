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
from app.api.transport import admin_sse
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


def _admin_cookie(session_factory: sessionmaker[Session], settings: Settings) -> str:
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
    status: str = "ok",
    actor_user_id: str | None = None,
    token_id: str | None = None,
    agent_label: str | None = None,
    assignment_id: str | None = None,
    fallback_attempts: int = 0,
    finish_reason: str | None = None,
    row_id: str | None = None,
) -> str:
    """Insert a fully-populated :class:`LlmUsage` row; return its id.

    Defaults match a happy-path ``ok`` call. Tests that need to vary
    the cd-wjpl telemetry columns can override per-keyword.
    """
    inserted_id = row_id or new_ulid()
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


class TestUsageList:
    """Cd-ccu9 ``GET /usage`` — paginated raw :class:`LlmUsage` feed."""

    def test_surfaces_every_column_including_cdwjpl_telemetry(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws = seed_workspace(s, slug="usage-list-ws")
            s.commit()
        actor_id = "01HW0000000000000000ACTR01"
        token_id = "01HW0000000000000000TOKN01"
        assignment_id = "01HW0000000000000000ASSN01"
        _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.manager",
            cost_cents=42,
            created_at=datetime.now(UTC) - timedelta(minutes=5),
            actor_user_id=actor_id,
            token_id=token_id,
            agent_label="manager-chat",
            assignment_id=assignment_id,
            fallback_attempts=2,
            finish_reason="stop",
        )
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        body = client.get("/admin/api/v1/usage").json()
        assert body["has_more"] is False
        assert body["next_cursor"] is None
        assert len(body["data"]) == 1
        row = body["data"][0]
        # Core columns.
        assert row["workspace_id"] == ws
        assert row["capability"] == "chat.manager"
        assert row["provider_model_id"] == "01HW00000000000000000MD01"
        assert row["tokens_in"] == 1000
        assert row["tokens_out"] == 500
        assert row["cost_cents"] == 42
        assert row["latency_ms"] == 100
        assert row["status"] == "ok"
        assert row["attempt"] == 0
        # cd-wjpl telemetry columns surface end-to-end.
        assert row["assignment_id"] == assignment_id
        assert row["fallback_attempts"] == 2
        assert row["finish_reason"] == "stop"
        assert row["actor_user_id"] == actor_id
        assert row["token_id"] == token_id
        assert row["agent_label"] == "manager-chat"
        # cd-v6dj rename: provider_model_id, never legacy "model_id".
        assert "model_id" not in row
        # ISO-8601 UTC; tz-aware.
        assert row["created_at"].endswith("+00:00")

    def test_filter_by_capability(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws = seed_workspace(s, slug="cap-filter-ws")
            s.commit()
        now = datetime.now(UTC)
        _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.manager",
            cost_cents=10,
            created_at=now - timedelta(minutes=5),
        )
        _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.employee",
            cost_cents=20,
            created_at=now - timedelta(minutes=3),
        )
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        body = client.get(
            "/admin/api/v1/usage",
            params={"capability": "chat.manager"},
        ).json()
        capabilities = {row["capability"] for row in body["data"]}
        assert capabilities == {"chat.manager"}

    def test_filter_by_actor_user_id(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws = seed_workspace(s, slug="actor-filter-ws")
            s.commit()
        now = datetime.now(UTC)
        actor_a = "01HW0000000000000000ACTR0A"
        actor_b = "01HW0000000000000000ACTR0B"
        _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.manager",
            cost_cents=10,
            created_at=now - timedelta(minutes=5),
            actor_user_id=actor_a,
        )
        _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.manager",
            cost_cents=20,
            created_at=now - timedelta(minutes=3),
            actor_user_id=actor_b,
        )
        # Service-initiated row (actor NULL) — must not appear.
        _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.manager",
            cost_cents=30,
            created_at=now - timedelta(minutes=1),
        )
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        body = client.get(
            "/admin/api/v1/usage",
            params={"actor_user_id": actor_a},
        ).json()
        actors = {row["actor_user_id"] for row in body["data"]}
        assert actors == {actor_a}

    def test_filter_by_status_success(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws = seed_workspace(s, slug="status-ok-ws")
            s.commit()
        now = datetime.now(UTC)
        _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.manager",
            cost_cents=10,
            created_at=now - timedelta(minutes=5),
            status="ok",
        )
        _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.manager",
            cost_cents=20,
            created_at=now - timedelta(minutes=3),
            status="error",
        )
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        body = client.get(
            "/admin/api/v1/usage",
            params={"status": "success"},
        ).json()
        assert {row["status"] for row in body["data"]} == {"ok"}

    def test_filter_by_status_error_covers_non_ok(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """``status=error`` matches every non-``ok`` value (error, refused, timeout)."""
        with session_factory() as s:
            ws = seed_workspace(s, slug="status-err-ws")
            s.commit()
        now = datetime.now(UTC)
        _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.manager",
            cost_cents=10,
            created_at=now - timedelta(minutes=5),
            status="ok",
        )
        for status_value in ("error", "refused", "timeout"):
            _add_llm_usage(
                session_factory,
                workspace_id=ws,
                capability="chat.manager",
                cost_cents=20,
                created_at=now - timedelta(minutes=3),
                status=status_value,
            )
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        body = client.get(
            "/admin/api/v1/usage",
            params={"status": "error"},
        ).json()
        statuses = {row["status"] for row in body["data"]}
        assert "ok" not in statuses
        assert statuses == {"error", "refused", "timeout"}

    def test_filter_by_status_invalid_value_returns_422(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.get(
            "/admin/api/v1/usage",
            params={"status": "ok"},  # raw enum value not allowed
        )
        assert resp.status_code == 422

    def test_filter_by_since(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws = seed_workspace(s, slug="since-filter-ws")
            s.commit()
        now = datetime.now(UTC)
        recent_id = _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.manager",
            cost_cents=10,
            created_at=now - timedelta(minutes=5),
        )
        # Older row — should be excluded by ``since``.
        _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.manager",
            cost_cents=20,
            created_at=now - timedelta(days=10),
        )
        cutoff = (now - timedelta(hours=1)).isoformat()
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        body = client.get(
            "/admin/api/v1/usage",
            params={"since": cutoff},
        ).json()
        assert [row["id"] for row in body["data"]] == [recent_id]

    def test_filter_by_since_rejects_malformed_iso(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.get(
            "/admin/api/v1/usage",
            params={"since": "not-a-date"},
        )
        assert resp.status_code == 422
        assert resp.json().get("error") == "invalid_iso8601"

    def test_filter_by_workspace_id_scopes_to_one_tenant(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws_a = seed_workspace(s, slug="ws-scope-a")
            ws_b = seed_workspace(s, slug="ws-scope-b")
            s.commit()
        now = datetime.now(UTC)
        _add_llm_usage(
            session_factory,
            workspace_id=ws_a,
            capability="chat.manager",
            cost_cents=10,
            created_at=now - timedelta(minutes=5),
        )
        _add_llm_usage(
            session_factory,
            workspace_id=ws_b,
            capability="chat.manager",
            cost_cents=20,
            created_at=now - timedelta(minutes=3),
        )
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        body = client.get(
            "/admin/api/v1/usage",
            params={"workspace_id": ws_a},
        ).json()
        assert {row["workspace_id"] for row in body["data"]} == {ws_a}
        # Absent workspace_id — both workspaces visible.
        body_all = client.get("/admin/api/v1/usage").json()
        assert {row["workspace_id"] for row in body_all["data"]} == {ws_a, ws_b}

    def test_pagination_walks_in_newest_first_order(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ws = seed_workspace(s, slug="page-ws")
            s.commit()
        now = datetime.now(UTC)
        ids: list[str] = []
        for i in range(5):
            ids.append(
                _add_llm_usage(
                    session_factory,
                    workspace_id=ws,
                    capability="chat.manager",
                    cost_cents=i,
                    created_at=now - timedelta(minutes=10 - i),
                )
            )
        # Newest-first: index 4 (created last) comes first.
        expected_order = list(reversed(ids))
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        page1 = client.get("/admin/api/v1/usage", params={"limit": 2}).json()
        assert page1["has_more"] is True
        assert page1["next_cursor"] is not None
        assert [r["id"] for r in page1["data"]] == expected_order[:2]
        page2 = client.get(
            "/admin/api/v1/usage",
            params={"limit": 2, "cursor": page1["next_cursor"]},
        ).json()
        assert [r["id"] for r in page2["data"]] == expected_order[2:4]
        # One row remains after page2 — has_more is True.
        assert page2["has_more"] is True
        page3 = client.get(
            "/admin/api/v1/usage",
            params={"limit": 2, "cursor": page2["next_cursor"]},
        ).json()
        assert [r["id"] for r in page3["data"]] == expected_order[4:5]
        assert page3["has_more"] is False
        assert page3["next_cursor"] is None
        # Page beyond the last row — pinning the cursor to the final
        # entry returns an empty page (no rows strictly older).
        page4 = client.get(
            "/admin/api/v1/usage",
            params={"limit": 2, "cursor": expected_order[-1]},
        ).json()
        assert page4["data"] == []
        assert page4["has_more"] is False
        assert page4["next_cursor"] is None

    def test_stale_cursor_returns_empty_page(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            seed_workspace(s, slug="stale-cursor-ws")
            s.commit()
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        body = client.get(
            "/admin/api/v1/usage",
            params={"cursor": "01HBOGUS00000000000000000"},
        ).json()
        assert body["data"] == []
        assert body["has_more"] is False
        assert body["next_cursor"] is None

    def test_pagination_breaks_ties_on_id(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """Cursor walk de-duplicates rows that share a ``created_at``.

        The recorder can fire in the same millisecond when a chain
        retries; the ``(created_at, id)`` tuple keeps the order
        total. A regression that dropped the trailing ``id`` clause
        would either drop a row or re-page it. Pin the
        tie-breaker explicitly: two rows with identical
        ``created_at`` must walk in id-descending order without
        repetition.
        """
        with session_factory() as s:
            ws = seed_workspace(s, slug="tie-break-ws")
            s.commit()
        shared = datetime.now(UTC) - timedelta(minutes=5)
        # Pin row ids so the descending-id ordering is deterministic.
        # Trailing letters chosen so id_high > id_low lexically
        # ('1' < '2' as ASCII / ULID-ordering).
        id_low = "01HW0000000000000000TIE001"
        id_high = "01HW0000000000000000TIE002"
        _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.manager",
            cost_cents=1,
            created_at=shared,
            row_id=id_low,
        )
        _add_llm_usage(
            session_factory,
            workspace_id=ws,
            capability="chat.manager",
            cost_cents=2,
            created_at=shared,
            row_id=id_high,
        )
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        page1 = client.get("/admin/api/v1/usage", params={"limit": 1}).json()
        # Newest-first with shared timestamps → larger id wins.
        assert [r["id"] for r in page1["data"]] == [id_high]
        assert page1["has_more"] is True
        page2 = client.get(
            "/admin/api/v1/usage",
            params={"limit": 1, "cursor": page1["next_cursor"]},
        ).json()
        # Tie-breaker walks to the strictly-lower id, no re-paging.
        assert [r["id"] for r in page2["data"]] == [id_low]
        assert page2["has_more"] is False
        assert page2["next_cursor"] is None

    def test_limit_above_max_returns_422(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.get(
            "/admin/api/v1/usage",
            params={"limit": 500},
        )
        # Cd-ccu9 cap is 200; values above must be rejected.
        assert resp.status_code == 422

    def test_unauthenticated_caller_receives_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        # No cookie installed — the admin tree's auth dep returns
        # 404 (spec §12: "the surface does not advertise its own
        # existence to tenants").
        resp = client.get("/admin/api/v1/usage")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"

    def test_non_admin_user_receives_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        # Logged-in user without a deployment-scope role grant —
        # `current_deployment_admin_principal` returns 404 per the
        # spec §12 admin envelope (no 403; the surface stays opaque
        # to non-admin tenants).
        with session_factory() as s:
            user_id = seed_user(s, email="rando@example.com", display_name="Rando")
            s.commit()
        client.cookies.set(
            SESSION_COOKIE_NAME,
            issue_session(session_factory, user_id=user_id, settings=settings),
        )
        resp = client.get("/admin/api/v1/usage")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"


class TestUpdateCap:
    def test_writes_cap_and_audits(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        published: list[dict[str, object]] = []
        monkeypatch.setattr(
            admin_sse.default_admin_fanout,
            "publish",
            lambda **kwargs: published.append(kwargs),
        )
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
        assert [event["kind"] for event in published] == [
            "admin.audit.appended",
            "admin.usage.updated",
        ]
        assert published[1]["payload"]["workspace_id"] == ws
        assert published[1]["payload"]["cap_cents_30d"] == 1500

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
