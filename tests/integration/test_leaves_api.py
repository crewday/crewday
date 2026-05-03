"""Integration tests for the leave routes in :mod:`app.api.v1.time` (cd-31c).

Exercises the leave-related routes through :class:`TestClient` against
a minimal FastAPI app wired with the same deps the factory uses.
Every test asserts on the HTTP boundary (status code, error
envelope, response shape) and on the side effects the domain
service emits (DB row, audit row).

Covers:

* ``POST /me/leaves`` — 201 + :class:`LeavePayload`; 422 on a bad
  window; 422 on a bad ``kind``.
* ``GET /me/leaves`` — returns the §12 cursor envelope
  ``{"data": [...], "next_cursor": "…", "has_more": …}``; filters by
  ``status``; a worker only sees their own leaves. Cursor pagination
  walks pages of size ``limit`` and the ``has_more`` flag flips
  correctly across page boundaries.
* ``PATCH /me/leaves/{id}`` — 200 while pending; 409 on non-pending.
* ``DELETE /me/leaves/{id}`` — 200 on pending cancel; 409 on
  already-cancelled.
* ``GET /leaves?status=pending`` — manager-only workspace queue; a
  worker gets 403.
* ``GET /leaves/{id}`` — read single leave; peer worker gets 403.
* ``DELETE /leaves/{id}`` — manager cancel of someone else's leave.
* ``GET /leaves?user_id=<other>`` — worker gets 403 on cross-user
  filter.
* OpenAPI: every leave route shows up with a pinned operation id.

The shift-side coverage lives in ``test_shifts_api.py``; this
module only exercises the leave surface.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.time.models import Leave, Shift
from app.adapters.db.workspace.models import Workspace
from app.api.deps import current_workspace_context, db_session
from app.api.v1.time import router as time_router
from app.events.bus import bus as default_event_bus
from app.events.types import LeaveDecided
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.ulid import new_ulid

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_FUTURE = _PINNED + timedelta(days=7)
_FUTURE_END = _FUTURE + timedelta(days=2)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    import importlib
    import pkgutil

    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def api_engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine.

    Named to avoid collision with the session-scoped ``engine``
    fixture from :mod:`tests.integration.conftest`. We don't need
    alembic here — the ORM surface is enough to exercise the router
    end-to-end.
    """
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(api_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=api_engine, expire_on_commit=False, class_=Session)


def _bootstrap_workspace(s: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"WS {slug}",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _bootstrap_user(s: Session, *, email: str, display_name: str) -> str:
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=display_name,
            created_at=_PINNED,
        )
    )
    s.flush()
    return user_id


def _grant(s: Session, *, workspace_id: str, user_id: str, grant_role: str) -> None:
    s.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    s.flush()


def _ctx(
    *,
    workspace_id: str,
    actor_id: str,
    slug: str = "api-ws",
    grant_role: ActorGrantRole = "worker",
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _build_app(
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
) -> FastAPI:
    """Mount :data:`time_router` behind pinned ctx + db overrides."""
    app = FastAPI()
    app.include_router(time_router)

    def _override_ctx() -> WorkspaceContext:
        return ctx

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db
    return app


@pytest.fixture
def worker_client(
    factory: sessionmaker[Session],
) -> tuple[TestClient, WorkspaceContext, str]:
    with factory() as s:
        ws_id = _bootstrap_workspace(s, slug="api-worker")
        user_id = _bootstrap_user(s, email="w@example.com", display_name="W")
        _grant(s, workspace_id=ws_id, user_id=user_id, grant_role="worker")
        s.commit()
    ctx = _ctx(workspace_id=ws_id, actor_id=user_id, grant_role="worker")
    client = TestClient(_build_app(factory, ctx), raise_server_exceptions=False)
    return client, ctx, user_id


@pytest.fixture
def manager_client(
    factory: sessionmaker[Session],
) -> tuple[TestClient, WorkspaceContext, str]:
    with factory() as s:
        ws_id = _bootstrap_workspace(s, slug="api-mgr")
        user_id = _bootstrap_user(s, email="m@example.com", display_name="M")
        _grant(s, workspace_id=ws_id, user_id=user_id, grant_role="manager")
        s.commit()
    ctx = _ctx(workspace_id=ws_id, actor_id=user_id, grant_role="manager")
    client = TestClient(_build_app(factory, ctx), raise_server_exceptions=False)
    return client, ctx, user_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _leave_body(
    *,
    kind: str = "vacation",
    starts_at: datetime = _FUTURE,
    ends_at: datetime = _FUTURE_END,
    reason_md: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": kind,
        "starts_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat(),
    }
    if reason_md is not None:
        body["reason_md"] = reason_md
    return body


def _audit_actions(
    factory: sessionmaker[Session], *, workspace_id: str, entity_kind: str
) -> list[str]:
    with factory() as s:
        rows = s.scalars(
            select(AuditLog)
            .where(
                AuditLog.workspace_id == workspace_id,
                AuditLog.entity_kind == entity_kind,
            )
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
    return [r.action for r in rows]


def _manager_for_workspace(
    factory: sessionmaker[Session], *, workspace_id: str
) -> tuple[TestClient, WorkspaceContext, str]:
    with factory() as s:
        manager_id = _bootstrap_user(
            s, email=f"m-{new_ulid()}@example.com", display_name="Manager"
        )
        _grant(s, workspace_id=workspace_id, user_id=manager_id, grant_role="manager")
        s.commit()
    ctx = _ctx(workspace_id=workspace_id, actor_id=manager_id, grant_role="manager")
    return (
        TestClient(_build_app(factory, ctx), raise_server_exceptions=False),
        ctx,
        manager_id,
    )


# ---------------------------------------------------------------------------
# POST /me/leaves
# ---------------------------------------------------------------------------


class TestCreateMyLeave:
    def test_worker_creates_leave_201(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, ctx, user_id = worker_client
        resp = client.post("/me/leaves", json=_leave_body())
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["user_id"] == user_id
        assert body["status"] == "pending"
        assert body["kind"] == "vacation"
        assert body["workspace_id"] == ctx.workspace_id

    def test_create_writes_audit_row(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
        factory: sessionmaker[Session],
    ) -> None:
        client, ctx, _uid = worker_client
        client.post("/me/leaves", json=_leave_body())
        actions = _audit_actions(
            factory, workspace_id=ctx.workspace_id, entity_kind="leave"
        )
        assert actions == ["leave.created"]

    def test_create_with_bad_window_422(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        resp = client.post(
            "/me/leaves",
            json=_leave_body(starts_at=_FUTURE_END, ends_at=_FUTURE),
        )
        assert resp.status_code == 422, resp.text

    def test_create_with_bad_kind_422(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        resp = client.post("/me/leaves", json=_leave_body(kind="nope"))
        assert resp.status_code == 422, resp.text

    def test_create_with_unknown_field_422(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        body = _leave_body()
        body["bogus"] = "yes"
        resp = client.post("/me/leaves", json=body)
        assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# GET /me/leaves
# ---------------------------------------------------------------------------


class TestListMyLeaves:
    def test_returns_data_envelope(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        client.post("/me/leaves", json=_leave_body())
        resp = client.get("/me/leaves")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # §12 "Pagination": every collection response carries the
        # ``data / next_cursor / has_more`` envelope, never ``items``.
        assert "data" in body
        assert "items" not in body
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        assert len(body["data"]) == 1

    def test_filter_by_status(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        a = client.post("/me/leaves", json=_leave_body()).json()
        client.post(
            "/me/leaves",
            json=_leave_body(
                starts_at=_FUTURE + timedelta(days=10),
                ends_at=_FUTURE_END + timedelta(days=10),
            ),
        )
        # Cancel the first one so we have one pending + one cancelled.
        client.delete(f"/me/leaves/{a['id']}")

        pending = client.get("/me/leaves", params={"status": "pending"}).json()
        cancelled = client.get("/me/leaves", params={"status": "cancelled"}).json()
        assert len(pending["data"]) == 1
        assert pending["data"][0]["status"] == "pending"
        assert len(cancelled["data"]) == 1
        assert cancelled["data"][0]["id"] == a["id"]

    def test_worker_only_sees_own(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        with factory() as s:
            ws_id = _bootstrap_workspace(s, slug="own-only")
            a_id = _bootstrap_user(s, email="a@o.com", display_name="A")
            b_id = _bootstrap_user(s, email="b@o.com", display_name="B")
            _grant(s, workspace_id=ws_id, user_id=a_id, grant_role="worker")
            _grant(s, workspace_id=ws_id, user_id=b_id, grant_role="worker")
            s.commit()
        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_id, actor_id=b_id, grant_role="worker")
        client_a = TestClient(_build_app(factory, ctx_a), raise_server_exceptions=False)
        client_b = TestClient(_build_app(factory, ctx_b), raise_server_exceptions=False)

        client_a.post("/me/leaves", json=_leave_body())

        a_list = client_a.get("/me/leaves").json()["data"]
        b_list = client_b.get("/me/leaves").json()["data"]
        assert len(a_list) == 1 and a_list[0]["user_id"] == a_id
        assert b_list == []

    def test_cursor_pagination(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        """Cursor walks every leave once; ``has_more`` flips on the last page."""
        client, *_ = worker_client
        ids: list[str] = []
        # Three leaves with strictly distinct ``starts_at`` so the
        # ordering is unambiguous and the cursor seek is exercised.
        for offset_days in (0, 14, 28):
            body = _leave_body(
                starts_at=_FUTURE + timedelta(days=offset_days),
                ends_at=_FUTURE_END + timedelta(days=offset_days),
            )
            ids.append(client.post("/me/leaves", json=body).json()["id"])

        page1 = client.get("/me/leaves", params={"limit": 2}).json()
        assert len(page1["data"]) == 2
        assert page1["has_more"] is True
        assert page1["next_cursor"] is not None

        page2 = client.get(
            "/me/leaves", params={"limit": 2, "cursor": page1["next_cursor"]}
        ).json()
        assert len(page2["data"]) == 1
        assert page2["has_more"] is False
        assert page2["next_cursor"] is None

        # Walked rows are chronological (starts_at ASC) — and together
        # the two pages cover every id with no overlap.
        seen = [row["id"] for row in page1["data"]] + [
            row["id"] for row in page2["data"]
        ]
        assert seen == ids


# ---------------------------------------------------------------------------
# PATCH /me/leaves/{id}
# ---------------------------------------------------------------------------


class TestUpdateMyLeave:
    def test_update_pending_200(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        created = client.post("/me/leaves", json=_leave_body()).json()
        new_start = (_FUTURE + timedelta(days=1)).isoformat()
        new_end = (_FUTURE_END + timedelta(days=1)).isoformat()
        resp = client.patch(
            f"/me/leaves/{created['id']}",
            json={"starts_at": new_start, "ends_at": new_end},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["starts_at"].startswith(
            (_FUTURE + timedelta(days=1)).isoformat()[:16]
        )

    def test_update_bad_window_422(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        created = client.post("/me/leaves", json=_leave_body()).json()
        resp = client.patch(
            f"/me/leaves/{created['id']}",
            json={
                "starts_at": _FUTURE_END.isoformat(),
                "ends_at": _FUTURE.isoformat(),
            },
        )
        assert resp.status_code == 422, resp.text

    def test_update_missing_404(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        resp = client.patch(
            "/me/leaves/nope",
            json={
                "starts_at": _FUTURE.isoformat(),
                "ends_at": _FUTURE_END.isoformat(),
            },
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "not_found"

    def test_update_non_pending_409(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
        factory: sessionmaker[Session],
    ) -> None:
        """A cancelled leave rejects a date PATCH with 409 ``invalid_transition``."""
        client, _ctx, _uid = worker_client
        created = client.post("/me/leaves", json=_leave_body()).json()
        client.delete(f"/me/leaves/{created['id']}")

        resp = client.patch(
            f"/me/leaves/{created['id']}",
            json={
                "starts_at": (_FUTURE + timedelta(days=2)).isoformat(),
                "ends_at": (_FUTURE_END + timedelta(days=2)).isoformat(),
            },
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "invalid_transition"


# ---------------------------------------------------------------------------
# DELETE /me/leaves/{id}
# ---------------------------------------------------------------------------


class TestCancelMyLeave:
    def test_cancel_pending_200(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        created = client.post("/me/leaves", json=_leave_body()).json()
        resp = client.delete(f"/me/leaves/{created['id']}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "cancelled"

    def test_cancel_missing_404(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        resp = client.delete("/me/leaves/nope")
        assert resp.status_code == 404, resp.text

    def test_cancel_already_cancelled_409(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        created = client.post("/me/leaves", json=_leave_body()).json()
        client.delete(f"/me/leaves/{created['id']}")
        resp = client.delete(f"/me/leaves/{created['id']}")
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "invalid_transition"


# ---------------------------------------------------------------------------
# GET /leaves
# ---------------------------------------------------------------------------


class TestListWorkspaceLeaves:
    def test_manager_sees_pending_queue(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        with factory() as s:
            ws_id = _bootstrap_workspace(s, slug="queue-q")
            worker_id = _bootstrap_user(s, email="w@q.com", display_name="W")
            mgr_id = _bootstrap_user(s, email="m@q.com", display_name="M")
            _grant(s, workspace_id=ws_id, user_id=worker_id, grant_role="worker")
            _grant(s, workspace_id=ws_id, user_id=mgr_id, grant_role="manager")
            s.commit()
        ctx_worker = _ctx(workspace_id=ws_id, actor_id=worker_id, grant_role="worker")
        ctx_mgr = _ctx(workspace_id=ws_id, actor_id=mgr_id, grant_role="manager")
        client_worker = TestClient(
            _build_app(factory, ctx_worker), raise_server_exceptions=False
        )
        client_mgr = TestClient(
            _build_app(factory, ctx_mgr), raise_server_exceptions=False
        )
        client_worker.post("/me/leaves", json=_leave_body())

        resp = client_mgr.get("/leaves", params={"status": "pending"})
        assert resp.status_code == 200, resp.text
        rows = resp.json()["data"]
        assert len(rows) == 1
        assert rows[0]["user_id"] == worker_id

    def test_worker_gets_403_on_workspace_queue(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        """A worker listing ``/leaves`` (no user_id filter) hits the
        workspace-wide queue and must be rejected."""
        client, *_ = worker_client
        resp = client.get("/leaves")
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "forbidden"

    def test_worker_gets_403_on_cross_user_filter(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        """A worker filtering by another user's id must be rejected."""
        with factory() as s:
            ws_id = _bootstrap_workspace(s, slug="cross-user")
            a_id = _bootstrap_user(s, email="a@x.com", display_name="A")
            b_id = _bootstrap_user(s, email="b@x.com", display_name="B")
            _grant(s, workspace_id=ws_id, user_id=a_id, grant_role="worker")
            _grant(s, workspace_id=ws_id, user_id=b_id, grant_role="worker")
            s.commit()
        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        client_a = TestClient(_build_app(factory, ctx_a), raise_server_exceptions=False)

        resp = client_a.get("/leaves", params={"user_id": b_id})
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "forbidden"

    def test_worker_can_read_own_via_list_leaves(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        """``/leaves?user_id=me`` is legit — same as ``/me/leaves``."""
        client, _ctx, user_id = worker_client
        client.post("/me/leaves", json=_leave_body())
        resp = client.get("/leaves", params={"user_id": user_id})
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["data"]) == 1


# ---------------------------------------------------------------------------
# GET /leaves/{id}
# ---------------------------------------------------------------------------


class TestGetLeave:
    def test_owner_reads_own(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        created = client.post("/me/leaves", json=_leave_body()).json()
        resp = client.get(f"/leaves/{created['id']}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == created["id"]

    def test_missing_404(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        resp = client.get("/leaves/nope")
        assert resp.status_code == 404, resp.text

    def test_peer_worker_403(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        with factory() as s:
            ws_id = _bootstrap_workspace(s, slug="peer-get")
            a_id = _bootstrap_user(s, email="a@g.com", display_name="A")
            b_id = _bootstrap_user(s, email="b@g.com", display_name="B")
            _grant(s, workspace_id=ws_id, user_id=a_id, grant_role="worker")
            _grant(s, workspace_id=ws_id, user_id=b_id, grant_role="worker")
            s.commit()
        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_id, actor_id=b_id, grant_role="worker")
        client_a = TestClient(_build_app(factory, ctx_a), raise_server_exceptions=False)
        client_b = TestClient(_build_app(factory, ctx_b), raise_server_exceptions=False)

        created = client_a.post("/me/leaves", json=_leave_body()).json()
        resp = client_b.get(f"/leaves/{created['id']}")
        assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# DELETE /leaves/{id} — manager cancel
# ---------------------------------------------------------------------------


class TestManagerCancel:
    def test_manager_cancels_worker_leave(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        with factory() as s:
            ws_id = _bootstrap_workspace(s, slug="mgr-c")
            worker_id = _bootstrap_user(s, email="w@c.com", display_name="W")
            mgr_id = _bootstrap_user(s, email="m@c.com", display_name="M")
            _grant(s, workspace_id=ws_id, user_id=worker_id, grant_role="worker")
            _grant(s, workspace_id=ws_id, user_id=mgr_id, grant_role="manager")
            s.commit()
        ctx_worker = _ctx(workspace_id=ws_id, actor_id=worker_id, grant_role="worker")
        ctx_mgr = _ctx(workspace_id=ws_id, actor_id=mgr_id, grant_role="manager")
        client_worker = TestClient(
            _build_app(factory, ctx_worker), raise_server_exceptions=False
        )
        client_mgr = TestClient(
            _build_app(factory, ctx_mgr), raise_server_exceptions=False
        )
        created = client_worker.post("/me/leaves", json=_leave_body()).json()
        resp = client_mgr.delete(f"/leaves/{created['id']}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "cancelled"

    def test_worker_cannot_cancel_another_via_leaves(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        with factory() as s:
            ws_id = _bootstrap_workspace(s, slug="peer-cancel-api")
            a_id = _bootstrap_user(s, email="a@c.com", display_name="A")
            b_id = _bootstrap_user(s, email="b@c.com", display_name="B")
            _grant(s, workspace_id=ws_id, user_id=a_id, grant_role="worker")
            _grant(s, workspace_id=ws_id, user_id=b_id, grant_role="worker")
            s.commit()
        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_id, actor_id=b_id, grant_role="worker")
        client_a = TestClient(_build_app(factory, ctx_a), raise_server_exceptions=False)
        client_b = TestClient(_build_app(factory, ctx_b), raise_server_exceptions=False)

        created = client_a.post("/me/leaves", json=_leave_body()).json()
        resp = client_b.delete(f"/leaves/{created['id']}")
        assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# GET /leaves/{id}/conflicts + POST /leaves/{id}/decision
# ---------------------------------------------------------------------------


class TestManagerDecision:
    def test_conflicts_and_decision_publish_event_once(
        self,
        factory: sessionmaker[Session],
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        worker, worker_ctx, worker_id = worker_client
        created = worker.post("/me/leaves", json=_leave_body()).json()
        manager, manager_ctx, _manager_id = _manager_for_workspace(
            factory, workspace_id=worker_ctx.workspace_id
        )
        with factory() as s:
            shift = Shift(
                id=new_ulid(),
                workspace_id=worker_ctx.workspace_id,
                user_id=worker_id,
                starts_at=_FUTURE + timedelta(hours=2),
                ends_at=_FUTURE + timedelta(hours=4),
                property_id=None,
                source="manual",
                notes_md=None,
                approved_by=None,
                approved_at=None,
            )
            occurrence = Occurrence(
                id=new_ulid(),
                workspace_id=worker_ctx.workspace_id,
                schedule_id=None,
                template_id=None,
                property_id=None,
                assignee_user_id=worker_id,
                starts_at=_FUTURE + timedelta(hours=5),
                ends_at=_FUTURE + timedelta(hours=6),
                scheduled_for_local="2026-04-26T17:00:00",
                originally_scheduled_for="2026-04-26T17:00:00",
                state="scheduled",
                title="linen reset",
                created_at=_PINNED,
            )
            s.add_all([shift, occurrence])
            s.commit()
            shift_id = shift.id
            occurrence_id = occurrence.id

        conflicts = manager.get(f"/leaves/{created['id']}/conflicts")
        assert conflicts.status_code == 200, conflicts.text
        assert conflicts.json() == {
            "leave_id": created["id"],
            "shift_ids": [shift_id],
            "occurrence_ids": [occurrence_id],
        }

        events: list[LeaveDecided] = []
        default_event_bus.subscribe(LeaveDecided)(events.append)
        try:
            decision = manager.post(
                f"/leaves/{created['id']}/decision",
                json={"decision": "approved", "rationale_md": "covered"},
            )
            replay = manager.post(
                f"/leaves/{created['id']}/decision",
                json={"decision": "approved", "rationale_md": "covered"},
            )
        finally:
            default_event_bus._reset_for_tests()

        assert decision.status_code == 200, decision.text
        assert decision.json()["status"] == "approved"
        assert decision.json()["decided_by"] == manager_ctx.actor_id
        assert replay.status_code == 200, replay.text
        assert len(events) == 1
        assert events[0].leave_id == created["id"]
        assert events[0].decision == "approved"
        assert events[0].conflicting_shift_ids == (shift_id,)
        assert events[0].conflicting_occurrence_ids == (occurrence_id,)
        assert _audit_actions(
            factory, workspace_id=worker_ctx.workspace_id, entity_kind="leave"
        ) == ["leave.created", "leave.decided"]

    def test_worker_cannot_decide_leave(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        worker, _ctx, _worker_id = worker_client
        created = worker.post("/me/leaves", json=_leave_body()).json()

        resp = worker.post(
            f"/leaves/{created['id']}/decision",
            json={"decision": "approved"},
        )

        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /me/ scope enforcement (a manager's /me/ path cannot touch worker leaves)
# ---------------------------------------------------------------------------


class TestMeScope:
    """``/me/leaves/{id}`` mutations are caller-scoped.

    Even a manager with ``leaves.edit_others`` must use
    ``/leaves/{id}`` (cross-user) rather than ``/me/leaves/{id}`` to
    operate on someone else's leave — the ``/me/`` URL is strictly
    "the caller's own". Collapsed to 404 rather than 403 so the
    ``/me/`` surface does not enumerate other users' leave ids
    (§01 "tenant surface is not enumerable").
    """

    def test_manager_me_patch_on_worker_leave_is_404(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        with factory() as s:
            ws_id = _bootstrap_workspace(s, slug="me-patch-mgr")
            worker_id = _bootstrap_user(s, email="w@me.com", display_name="W")
            mgr_id = _bootstrap_user(s, email="m@me.com", display_name="M")
            _grant(s, workspace_id=ws_id, user_id=worker_id, grant_role="worker")
            _grant(s, workspace_id=ws_id, user_id=mgr_id, grant_role="manager")
            s.commit()
        ctx_worker = _ctx(workspace_id=ws_id, actor_id=worker_id, grant_role="worker")
        ctx_mgr = _ctx(workspace_id=ws_id, actor_id=mgr_id, grant_role="manager")
        client_worker = TestClient(
            _build_app(factory, ctx_worker), raise_server_exceptions=False
        )
        client_mgr = TestClient(
            _build_app(factory, ctx_mgr), raise_server_exceptions=False
        )
        created = client_worker.post("/me/leaves", json=_leave_body()).json()

        resp = client_mgr.patch(
            f"/me/leaves/{created['id']}",
            json={
                "starts_at": (_FUTURE + timedelta(days=3)).isoformat(),
                "ends_at": (_FUTURE_END + timedelta(days=3)).isoformat(),
            },
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "not_found"

    def test_manager_me_delete_on_worker_leave_is_404(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        with factory() as s:
            ws_id = _bootstrap_workspace(s, slug="me-del-mgr")
            worker_id = _bootstrap_user(s, email="w@md.com", display_name="W")
            mgr_id = _bootstrap_user(s, email="m@md.com", display_name="M")
            _grant(s, workspace_id=ws_id, user_id=worker_id, grant_role="worker")
            _grant(s, workspace_id=ws_id, user_id=mgr_id, grant_role="manager")
            s.commit()
        ctx_worker = _ctx(workspace_id=ws_id, actor_id=worker_id, grant_role="worker")
        ctx_mgr = _ctx(workspace_id=ws_id, actor_id=mgr_id, grant_role="manager")
        client_worker = TestClient(
            _build_app(factory, ctx_worker), raise_server_exceptions=False
        )
        client_mgr = TestClient(
            _build_app(factory, ctx_mgr), raise_server_exceptions=False
        )
        created = client_worker.post("/me/leaves", json=_leave_body()).json()

        resp = client_mgr.delete(f"/me/leaves/{created['id']}")
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "not_found"

        # But the manager can use the cross-user URL and succeed.
        ok = client_mgr.delete(f"/leaves/{created['id']}")
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def test_peer_workspace_leave_invisible(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        with factory() as s:
            ws_a = _bootstrap_workspace(s, slug="lv-a")
            ws_b = _bootstrap_workspace(s, slug="lv-b")
            u_a = _bootstrap_user(s, email="a@lv.com", display_name="A")
            u_b = _bootstrap_user(s, email="b@lv.com", display_name="B")
            _grant(s, workspace_id=ws_a, user_id=u_a, grant_role="worker")
            _grant(s, workspace_id=ws_b, user_id=u_b, grant_role="worker")
            s.commit()
        ctx_a = _ctx(workspace_id=ws_a, actor_id=u_a, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_b, actor_id=u_b, grant_role="worker")
        client_a = TestClient(_build_app(factory, ctx_a), raise_server_exceptions=False)
        client_b = TestClient(_build_app(factory, ctx_b), raise_server_exceptions=False)
        created = client_a.post("/me/leaves", json=_leave_body()).json()

        resp = client_b.get(f"/leaves/{created['id']}")
        assert resp.status_code == 404, resp.text
        assert client_b.get("/me/leaves").json()["data"] == []


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_create_persists_row(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
        factory: sessionmaker[Session],
    ) -> None:
        client, ctx, user_id = worker_client
        body = client.post("/me/leaves", json=_leave_body()).json()
        with factory() as s:
            row = s.get(Leave, body["id"])
            assert row is not None
            assert row.workspace_id == ctx.workspace_id
            assert row.user_id == user_id
            assert row.status == "pending"

    def test_cancel_persists_status(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
        factory: sessionmaker[Session],
    ) -> None:
        client, *_ = worker_client
        created = client.post("/me/leaves", json=_leave_body()).json()
        client.delete(f"/me/leaves/{created['id']}")
        with factory() as s:
            row = s.get(Leave, created["id"])
            assert row is not None
            assert row.status == "cancelled"


# ---------------------------------------------------------------------------
# OpenAPI surface
# ---------------------------------------------------------------------------


class TestOpenapiExposure:
    def test_leave_routes_in_openapi(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        schema = client.get("/openapi.json").json()
        paths = schema.get("paths", {})
        assert "/me/leaves" in paths
        assert "/me/leaves/{leave_id}" in paths
        assert "/leaves" in paths
        assert "/leaves/{leave_id}" in paths
        assert "/leaves/{leave_id}/conflicts" in paths
        assert "/leaves/{leave_id}/decision" in paths
        op_ids: set[str] = set()
        for path in paths.values():
            for op in path.values():
                if isinstance(op, dict) and "operationId" in op:
                    op_ids.add(op["operationId"])
        expected = {
            "time.create_my_leave",
            "time.list_my_leaves",
            "time.update_my_leave_dates",
            "time.cancel_my_leave",
            "time.list_leaves",
            "time.get_leave",
            "time.get_leave_conflicts",
            "time.decide_leave",
            "time.cancel_leave",
        }
        assert expected.issubset(op_ids), f"missing: {expected - op_ids}"

    def test_leave_payload_component_emitted(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        """FastAPI emits a named component schema for ``LeavePayload``
        so the SPA generator can discriminate on it."""
        client, *_ = worker_client
        schema = client.get("/openapi.json").json()
        components = schema.get("components", {}).get("schemas", {})
        assert "LeavePayload" in components
