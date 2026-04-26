"""Unit tests for :mod:`app.api.admin.audit`.

Covers ``GET /audit`` + ``GET /audit/tail`` per spec §12 "Admin
surface" §"Deployment audit":

* Returns only ``scope_kind='deployment'`` rows; workspace rows
  must not leak.
* Newest-first ordering with cursor walk.
* Filters: ``actor_id`` / ``action`` / ``entity_kind`` /
  ``entity_id`` / ``since`` / ``until``.
* Tail emits NDJSON; ``follow=1`` is accepted (cd-7xth wires
  the streaming path later).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.api.admin.audit import NDJSON_MEDIA_TYPE
from app.auth.session import SESSION_COOKIE_NAME
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.unit.api.admin._helpers import (
    PINNED,
    build_client,
    engine_fixture,
    grant_deployment_admin,
    issue_session,
    seed_user,
    settings_fixture,
)


@pytest.fixture
def settings() -> Settings:
    return settings_fixture("audit")


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


def _seed_audit(
    session_factory: sessionmaker[Session],
    *,
    scope_kind: str,
    action: str,
    entity_kind: str = "deployment_setting",
    entity_id: str | None = None,
    actor_id: str = "01HW0000000000000000000ACT",
    workspace_id: str | None = None,
    created_at: datetime | None = None,
) -> str:
    audit_id = new_ulid()
    with session_factory() as s, tenant_agnostic():
        s.add(
            AuditLog(
                id=audit_id,
                workspace_id=workspace_id,
                actor_id=actor_id,
                actor_kind="user",
                actor_grant_role="manager",
                actor_was_owner_member=False,
                entity_kind=entity_kind,
                entity_id=entity_id or new_ulid(),
                action=action,
                diff={},
                correlation_id=new_ulid(),
                scope_kind=scope_kind,
                created_at=created_at or PINNED,
            )
        )
        s.commit()
    return audit_id


class TestListAudit:
    def test_only_deployment_scoped_rows_visible(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        from app.adapters.db.workspace.models import Workspace

        with session_factory() as s, tenant_agnostic():
            ws_id = new_ulid()
            s.add(
                Workspace(
                    id=ws_id,
                    slug="ws",
                    name="WS",
                    plan="free",
                    quota_json={},
                    created_at=PINNED,
                )
            )
            s.commit()
        deployment_id = _seed_audit(
            session_factory,
            scope_kind="deployment",
            action="signup_settings.updated",
        )
        _seed_audit(
            session_factory,
            scope_kind="workspace",
            action="task.created",
            workspace_id=ws_id,
        )
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        body = client.get("/admin/api/v1/audit").json()
        ids = [row["id"] for row in body["data"]]
        assert deployment_id in ids
        # Workspace-scoped rows are filtered out.
        assert all(row["id"] != "task.created" for row in body["data"])
        assert len(ids) == 1

    def test_newest_first_with_cursor_walk(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        a = _seed_audit(
            session_factory,
            scope_kind="deployment",
            action="signup_settings.updated",
            created_at=PINNED,
        )
        b = _seed_audit(
            session_factory,
            scope_kind="deployment",
            action="signup_settings.updated",
            created_at=PINNED + timedelta(hours=1),
        )
        c = _seed_audit(
            session_factory,
            scope_kind="deployment",
            action="signup_settings.updated",
            created_at=PINNED + timedelta(hours=2),
        )
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        page1 = client.get("/admin/api/v1/audit", params={"limit": 2}).json()
        ids1 = [row["id"] for row in page1["data"]]
        assert ids1 == [c, b]
        assert page1["has_more"] is True
        page2 = client.get(
            "/admin/api/v1/audit",
            params={"limit": 2, "cursor": page1["next_cursor"]},
        ).json()
        assert [row["id"] for row in page2["data"]] == [a]
        assert page2["has_more"] is False

    def test_action_filter_narrows_rows(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        target = _seed_audit(
            session_factory,
            scope_kind="deployment",
            action="admin.granted",
        )
        _seed_audit(
            session_factory,
            scope_kind="deployment",
            action="signup_settings.updated",
        )
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        body = client.get(
            "/admin/api/v1/audit",
            params={"action": "admin.granted"},
        ).json()
        assert [row["id"] for row in body["data"]] == [target]

    def test_unknown_cursor_returns_empty_page(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        # A stale or fabricated cursor (row deleted by retention
        # rotation, or the caller forged the id) must not silently
        # collapse to a full re-page — that traps the client in an
        # infinite cursor walk. Returning an empty page with
        # ``has_more=False`` signals "exhausted; restart" cleanly.
        _seed_audit(
            session_factory,
            scope_kind="deployment",
            action="signup_settings.updated",
        )
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        body = client.get(
            "/admin/api/v1/audit",
            params={"limit": 5, "cursor": "01HBOGUS00000000000000000"},
        ).json()
        assert body["data"] == []
        assert body["has_more"] is False
        assert body["next_cursor"] is None

    def test_invalid_iso_since_returns_typed_422(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        resp = client.get(
            "/admin/api/v1/audit",
            params={"since": "not-a-timestamp"},
        )
        assert resp.status_code == 422
        assert resp.json().get("error") == "invalid_iso8601"

    def test_404_for_non_admin(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            stranger = seed_user(s, email="x@example.com", display_name="X")
            s.commit()
        cookie = issue_session(session_factory, user_id=stranger, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.get("/admin/api/v1/audit")
        assert resp.status_code == 404


class TestTailAudit:
    def test_tail_emits_ndjson_with_correct_media_type(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        a = _seed_audit(
            session_factory,
            scope_kind="deployment",
            action="signup_settings.updated",
            created_at=PINNED,
        )
        b = _seed_audit(
            session_factory,
            scope_kind="deployment",
            action="signup_settings.updated",
            created_at=PINNED + timedelta(hours=1),
        )
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        with client.stream("GET", "/admin/api/v1/audit/tail") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith(NDJSON_MEDIA_TYPE)
            body = b"".join(resp.iter_bytes()).decode("utf-8")
        # Newest-first.
        lines = [line for line in body.split("\n") if line]
        import json as _json

        ids = [_json.loads(line)["id"] for line in lines]
        assert ids == [b, a]

    def test_empty_feed_emits_keepalive_newline(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        # Empty result set still flushes a single newline so
        # intermediaries (Pangolin, nginx) and curl observe a
        # clean end-of-stream rather than a zero-length body
        # they may coalesce away.
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        with client.stream("GET", "/admin/api/v1/audit/tail") as resp:
            assert resp.status_code == 200
            body = b"".join(resp.iter_bytes())
        assert body == b"\n"

    def test_follow_param_accepted(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        client.cookies.set(
            SESSION_COOKIE_NAME, _admin_cookie(session_factory, settings)
        )
        with client.stream(
            "GET", "/admin/api/v1/audit/tail", params={"follow": 1}
        ) as resp:
            assert resp.status_code == 200
            # Empty result returns a single empty chunk; the route
            # accepts ``follow=1`` today (cd-7xth wires the
            # long-poll path later).

    def test_404_for_non_admin(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            stranger = seed_user(s, email="x@example.com", display_name="X")
            s.commit()
        cookie = issue_session(session_factory, user_id=stranger, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.get("/admin/api/v1/audit/tail")
        assert resp.status_code == 404
