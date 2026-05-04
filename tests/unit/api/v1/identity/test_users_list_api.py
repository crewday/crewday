"""HTTP-level tests for ``GET /users`` workspace user index (cd-8y5aa)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.workspace.models import UserWorkspace
from app.api.v1.users import build_users_router
from app.auth._throttle import Throttle
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests._fakes.mailer import InMemoryMailer
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client

_PINNED = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client(
        [
            (
                "",
                build_users_router(mailer=InMemoryMailer(), throttle=Throttle()),
            )
        ],
        factory,
        ctx,
    )


def _seed_member(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    email: str,
    display_name: str,
) -> str:
    with factory() as s:
        user = bootstrap_user(s, email=email, display_name=display_name)
        s.add(
            UserWorkspace(
                user_id=user.id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=_PINNED,
            )
        )
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        s.commit()
        return user.id


class TestListUsers:
    def test_returns_standard_envelope_with_slim_user_rows(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, workspace_id = owner_ctx
        user_id = _seed_member(
            factory,
            workspace_id=workspace_id,
            email="alice@example.com",
            display_name="Alice",
        )

        resp = _client(ctx, factory).get("/users")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {"data", "next_cursor", "has_more"}
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        row = next(r for r in body["data"] if r["id"] == user_id)
        assert row == {
            "id": user_id,
            "display_name": "Alice",
            "email": "alice@example.com",
        }

    def test_paginates_with_real_cursor(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, workspace_id = owner_ctx
        extra_ids = [
            _seed_member(
                factory,
                workspace_id=workspace_id,
                email=f"user-{idx}@example.com",
                display_name=f"User {idx}",
            )
            for idx in range(3)
        ]

        client = _client(ctx, factory)
        page1 = client.get("/users", params={"limit": 2})
        assert page1.status_code == 200, page1.text
        body1 = page1.json()
        assert len(body1["data"]) == 2
        assert body1["has_more"] is True
        assert body1["next_cursor"] is not None

        page2 = client.get(
            "/users",
            params={"limit": 2, "cursor": body1["next_cursor"]},
        )
        assert page2.status_code == 200, page2.text
        body2 = page2.json()
        assert body2["has_more"] is False
        assert body2["next_cursor"] is None

        seen = {row["id"] for row in body1["data"] + body2["data"]}
        assert len(seen) == len(body1["data"]) + len(body2["data"])
        assert set(extra_ids).issubset(seen)
        assert ctx.actor_id in seen

    def test_workspace_membership_is_tenant_scoped(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, workspace_id = owner_ctx
        visible_id = _seed_member(
            factory,
            workspace_id=workspace_id,
            email="visible@example.com",
            display_name="Visible",
        )
        with factory() as s:
            sibling_owner = bootstrap_user(
                s,
                email="sibling-owner@example.com",
                display_name="Sibling Owner",
            )
            sibling = bootstrap_workspace(
                s,
                slug="sibling-users",
                name="Sibling",
                owner_user_id=sibling_owner.id,
            )
            with tenant_agnostic():
                hidden = bootstrap_user(
                    s,
                    email="hidden@example.com",
                    display_name="Hidden",
                )
                s.add(
                    UserWorkspace(
                        user_id=hidden.id,
                        workspace_id=sibling.id,
                        source="workspace_grant",
                        added_at=_PINNED,
                    )
                )
            s.commit()
            hidden_id = hidden.id

        body = _client(ctx, factory).get("/users").json()
        ids = {row["id"] for row in body["data"]}
        assert visible_id in ids
        assert hidden_id not in ids

    def test_invalid_cursor_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        resp = _client(ctx, factory).get("/users", params={"cursor": "not-a-cursor"})
        assert resp.status_code == 422, resp.text
        assert resp.json()["type"].endswith("/invalid_cursor")


class TestAuthZ:
    def test_worker_without_employees_read_is_denied(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        ctx, factory, _, _ = worker_ctx
        resp = _client(ctx, factory).get("/users")
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "permission_denied"
