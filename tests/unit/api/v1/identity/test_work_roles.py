"""HTTP-level tests for ``/work_roles`` (cd-dcfw).

Covers:

* Owner can list / create / patch.
* Worker (not in owners, not in managers grant) can LIST (via
  ``scope.view`` default-allow ``all_workers``) but cannot CREATE or
  PATCH (``work_roles.manage`` default-allow is owners + managers).
* Key uniqueness is enforced — duplicate key on create surfaces 422
  ``work_role_key_conflict``.
* PATCH on unknown id returns 404.
* Pagination envelope shape matches §12.

The unit-level tests exercise every handler against an in-memory
SQLite engine with the router mounted at root. Tenancy + UoW deps
are overridden to pin the ctx + session, matching the
shared v1 router harness.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.workspace.models import UserWorkRole, WorkRole
from app.api.v1.work_roles import build_work_roles_router
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client


def _owner_client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", build_work_roles_router())], factory, ctx)


def _assert_problem_error(resp: Response, *, error: str) -> dict[str, object]:
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert isinstance(body, dict)
    assert body["error"] == error
    return body


class TestList:
    def test_owner_gets_empty_envelope(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        resp = client.get("/work_roles")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"data": [], "next_cursor": None, "has_more": False}

    def test_worker_can_read_catalogue(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """``scope.view`` default-allow includes ``all_workers``."""
        ctx, factory, _, _ = worker_ctx
        client = build_client([("", build_work_roles_router())], factory, ctx)
        resp = client.get("/work_roles")
        assert resp.status_code == 200, resp.text

    def test_limit_above_cap_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        resp = client.get("/work_roles?limit=501")
        assert resp.status_code == 422

    def test_malformed_cursor_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        resp = client.get("/work_roles?cursor=%20%20%20invalid%20%20%20")
        assert resp.status_code == 422
        assert resp.json()["type"].endswith("/invalid_cursor")


class TestCreate:
    def test_owner_creates_role(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        client = _owner_client(ctx, factory)
        resp = client.post(
            "/work_roles",
            json={"key": "maid", "name": "Maid", "icon_name": "BrushCleaning"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["key"] == "maid"
        assert body["name"] == "Maid"
        assert body["icon_name"] == "BrushCleaning"
        assert body["workspace_id"] == ws_id

    def test_duplicate_key_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        client.post("/work_roles", json={"key": "maid", "name": "Maid"})
        resp = client.post("/work_roles", json={"key": "maid", "name": "Dup Maid"})
        assert resp.status_code == 422
        body = _assert_problem_error(resp, error="work_role_key_conflict")
        assert body["message"] == body["detail"]

    def test_worker_create_returns_403(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        ctx, factory, _, _ = worker_ctx
        client = build_client([("", build_work_roles_router())], factory, ctx)
        resp = client.post("/work_roles", json={"key": "maid", "name": "Maid"})
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "permission_denied"
        assert resp.json()["detail"]["action_key"] == "work_roles.manage"

    def test_missing_key_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        resp = client.post("/work_roles", json={"name": "Maid"})
        assert resp.status_code == 422

    def test_unknown_field_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """``extra='forbid'`` on the DTO means typos fail loud."""
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        resp = client.post(
            "/work_roles", json={"key": "maid", "name": "Maid", "flavour": "oops"}
        )
        assert resp.status_code == 422


class TestPatch:
    def test_owner_patches_name(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        created = client.post(
            "/work_roles", json={"key": "maid", "name": "Maid"}
        ).json()
        resp = client.patch(
            f"/work_roles/{created['id']}", json={"name": "Housekeeper"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Housekeeper"

    def test_unknown_id_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        resp = client.patch("/work_roles/does-not-exist", json={"name": "X"})
        assert resp.status_code == 404
        _assert_problem_error(resp, error="work_role_not_found")

    def test_patch_to_duplicate_key_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        client.post("/work_roles", json={"key": "maid", "name": "Maid"})
        driver = client.post(
            "/work_roles", json={"key": "driver", "name": "Driver"}
        ).json()
        resp = client.patch(f"/work_roles/{driver['id']}", json={"key": "maid"})
        assert resp.status_code == 422
        body = _assert_problem_error(resp, error="work_role_key_conflict")
        assert body["message"] == body["detail"]

    def test_worker_patch_returns_403(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        ctx, factory, _, _ = worker_ctx
        client = build_client([("", build_work_roles_router())], factory, ctx)
        resp = client.patch("/work_roles/does-not-matter", json={"name": "X"})
        # The permission gate fires BEFORE the handler; 403, not 404.
        assert resp.status_code == 403


class TestDelete:
    def test_owner_soft_deletes_role_and_hides_it_from_list(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        created = client.post(
            "/work_roles", json={"key": "maid", "name": "Maid"}
        ).json()

        resp = client.delete(f"/work_roles/{created['id']}")
        assert resp.status_code == 204, resp.text
        assert not resp.content

        listed = client.get("/work_roles")
        assert listed.status_code == 200, listed.text
        assert listed.json()["data"] == []

        with factory() as s:
            row = s.get(WorkRole, created["id"])
            assert row is not None
            assert row.deleted_at is not None
            audit = s.scalar(
                select(AuditLog).where(
                    AuditLog.entity_kind == "work_role",
                    AuditLog.entity_id == created["id"],
                    AuditLog.action == "work_role.deleted",
                )
            )
            assert audit is not None
            assert audit.diff == {"deleted_at": row.deleted_at.isoformat()}

    def test_delete_keeps_user_work_role_rows_intact(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        client = _owner_client(ctx, factory)
        created = client.post(
            "/work_roles", json={"key": "driver", "name": "Driver"}
        ).json()
        link_id = new_ulid()
        with factory() as s:
            s.add(
                UserWorkRole(
                    id=link_id,
                    user_id=ctx.actor_id,
                    workspace_id=ws_id,
                    work_role_id=created["id"],
                    started_on=date(2026, 4, 1),
                    ended_on=None,
                    pay_rule_id=None,
                    created_at=datetime.now(tz=UTC),
                    deleted_at=None,
                )
            )
            s.commit()

        resp = client.delete(f"/work_roles/{created['id']}")
        assert resp.status_code == 204, resp.text

        with factory() as s:
            link = s.get(UserWorkRole, link_id)
            assert link is not None
            assert link.work_role_id == created["id"]
            assert link.deleted_at is None

    def test_unknown_id_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        resp = client.delete("/work_roles/does-not-exist")
        assert resp.status_code == 404
        _assert_problem_error(resp, error="work_role_not_found")

    def test_already_deleted_role_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        created = client.post(
            "/work_roles", json={"key": "maid", "name": "Maid"}
        ).json()
        first = client.delete(f"/work_roles/{created['id']}")
        assert first.status_code == 204, first.text

        second = client.delete(f"/work_roles/{created['id']}")
        assert second.status_code == 404
        _assert_problem_error(second, error="work_role_not_found")

    def test_cross_workspace_role_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        with factory() as s:
            owner = bootstrap_user(
                s, email="other-owner@example.com", display_name="Other Owner"
            )
            other_ws = bootstrap_workspace(
                s,
                slug="other-workspace",
                name="Other Workspace",
                owner_user_id=owner.id,
            )
            role = WorkRole(
                id=new_ulid(),
                workspace_id=other_ws.id,
                key="maid",
                name="Maid",
                description_md="",
                default_settings_json={},
                icon_name="",
                created_at=datetime.now(tz=UTC),
                deleted_at=None,
            )
            s.add(role)
            s.commit()
            other_role_id = role.id

        client = _owner_client(ctx, factory)
        resp = client.delete(f"/work_roles/{other_role_id}")
        assert resp.status_code == 404
        _assert_problem_error(resp, error="work_role_not_found")

        with factory() as s:
            other_role = s.scalar(select(WorkRole).where(WorkRole.id == other_role_id))
            assert other_role is not None
            assert other_role.deleted_at is None

    def test_worker_delete_returns_403(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        ctx, factory, _, _ = worker_ctx
        client = build_client([("", build_work_roles_router())], factory, ctx)
        resp = client.delete("/work_roles/does-not-matter")
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "permission_denied"
        assert resp.json()["detail"]["action_key"] == "work_roles.manage"


class TestOpenApiShape:
    """Spec §12 + §01 — tags must include ``identity`` + resource tag."""

    def test_list_operation_has_identity_tag(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        schema = client.get("/openapi.json").json()
        op = schema["paths"]["/work_roles"]["get"]
        assert "identity" in op["tags"]
        assert "work_roles" in op["tags"]
        assert op["operationId"] == "work_roles.list"

    def test_delete_operation_has_identity_tag(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _owner_client(ctx, factory)
        schema = client.get("/openapi.json").json()
        op = schema["paths"]["/work_roles/{work_role_id}"]["delete"]
        assert "identity" in op["tags"]
        assert "work_roles" in op["tags"]
        assert op["operationId"] == "work_roles.delete"
