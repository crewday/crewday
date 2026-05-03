"""Unit tests for :mod:`app.api.admin.audit`.

Covers ``GET /audit`` + ``GET /audit/tail`` per spec §12 "Admin
surface" §"Deployment audit":

* Returns only ``scope_kind='deployment'`` rows; workspace rows
  must not leak.
* Newest-first ordering with cursor walk.
* Filters: ``actor_id`` / ``action`` / ``entity_kind`` /
  ``entity_id`` / ``since`` / ``until``.
* Tail emits NDJSON; ``follow=1`` keeps the stream open until
  client disconnect.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import ASGIApp, Message, Scope

from app.adapters.db.audit.models import AuditLog
from app.api.admin.audit import NDJSON_MEDIA_TYPE
from app.auth.session import SESSION_COOKIE_NAME
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.unit.api.admin._helpers import (
    PINNED,
    TEST_ACCEPT_LANGUAGE,
    TEST_UA,
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


def _admin_cookie(session_factory: sessionmaker[Session], settings: Settings) -> str:
    with session_factory() as s:
        user_id = seed_user(s, email="ada@example.com", display_name="Ada")
        grant_deployment_admin(s, user_id=user_id)
        s.commit()
    return issue_session(session_factory, user_id=user_id, settings=settings)


async def _collect_stream_bodies(
    app: ASGIApp,
    path: str,
    query_string: bytes,
    cookie: str,
    body_count: int,
    after_first_body: Callable[[], None] | None = None,
) -> tuple[int, list[bytes]]:
    enough_bodies = anyio.Event()
    request_sent = False
    status_code = 0
    bodies: list[bytes] = []

    async def receive() -> Message:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await enough_bodies.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = int(message["status"])
            return
        if message["type"] != "http.response.body":
            return
        chunk = message.get("body", b"")
        if not isinstance(chunk, bytes) or chunk == b"":
            return
        bodies.append(chunk)
        if len(bodies) == 1 and after_first_body is not None:
            after_first_body()
        if len(bodies) >= body_count:
            enough_bodies.set()

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query_string,
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"user-agent", TEST_UA.encode("ascii")),
            (b"accept-language", TEST_ACCEPT_LANGUAGE.encode("ascii")),
            (b"cookie", f"{SESSION_COOKIE_NAME}={cookie}".encode("ascii")),
        ],
        "client": ("127.0.0.1", 123),
        "server": ("testserver", 443),
    }
    with anyio.fail_after(1):
        await app(scope, receive, send)
    return status_code, bodies


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
        ids = [json.loads(line)["id"] for line in lines]
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

    def test_follow_streams_until_client_disconnect(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        row_id = _seed_audit(
            session_factory,
            scope_kind="deployment",
            action="signup_settings.updated",
        )
        cookie = _admin_cookie(session_factory, settings)

        status_code, bodies = anyio.run(
            _collect_stream_bodies,
            client.app,
            "/admin/api/v1/audit/tail",
            b"follow=1",
            cookie,
            1,
        )

        assert status_code == 200
        body = b"".join(bodies)
        assert json.loads(body.decode("utf-8"))["id"] == row_id

    def test_follow_polls_rows_created_after_empty_initial_feed(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        cookie = _admin_cookie(session_factory, settings)
        inserted_row: list[str] = []

        def _insert_after_keepalive() -> None:
            inserted_row.append(
                _seed_audit(
                    session_factory,
                    scope_kind="deployment",
                    action="signup_settings.updated",
                )
            )

        status_code, bodies = anyio.run(
            _collect_stream_bodies,
            client.app,
            "/admin/api/v1/audit/tail",
            b"follow=1",
            cookie,
            2,
            _insert_after_keepalive,
        )

        assert status_code == 200
        assert bodies[0] == b"\n"
        assert json.loads(bodies[1].decode("utf-8"))["id"] == inserted_row[0]

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
