"""HTTP-level tests for ``/role_grants`` + ``/users/{id}/role_grants``.

Cd-jinb — exercises the full router surface against an in-memory DB
with the workspace context pinned, owners + worker personas seeded,
and the dependency-overridden :class:`TestClient` from
:mod:`tests.unit.api.v1.identity.conftest` (sibling pattern with
``test_work_engagements.py`` etc.).

Covers:

* CRUD happy paths for owner / manager / worker personas.
* §05 owner-authority rejection for non-owners minting ``manager``.
* Last-owner protection on revoke (``LastOwnerGrantProtected``).
* Cross-workspace property scope rejection.
* Cursor pagination on the user-keyed listing.
* PATCH re-scope (no-op + happy + cross-workspace 422).
* Workspace-scoped 404 on a sibling-tenant grant id.
* OpenAPI tag shape.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.workspace.models import UserWorkspace
from app.api.v1.role_grants import (
    build_role_grants_router,
    build_users_role_grants_router,
)
from app.events import RoleGrantCreated, RoleGrantRevoked
from app.events import (
    bus as default_event_bus,
)
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user
from tests.unit.api.v1.identity.conftest import build_client, ctx_for, seed_worker_user


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client(
        [
            ("", build_role_grants_router()),
            ("", build_users_role_grants_router()),
        ],
        factory,
        ctx,
    )


def _assert_problem_error(resp: Response, *, error: str) -> dict[str, object]:
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert isinstance(body, dict)
    assert body["error"] == error
    return body


def _seed_property_in_workspace(
    factory: sessionmaker[Session], *, workspace_id: str
) -> str:
    """Seed a property + ``property_workspace`` link for in-tenancy tests."""
    prop_id = new_ulid()
    now = datetime.now(tz=UTC)
    with factory() as s:
        s.add(
            Property(
                id=prop_id,
                name="Test Property",
                kind="residence",
                address="1 Test Street",
                address_json={},
                country="XX",
                timezone="UTC",
                tags_json=[],
                welcome_defaults_json={},
                property_notes_md="",
                created_at=now,
            )
        )
        s.add(
            PropertyWorkspace(
                property_id=prop_id,
                workspace_id=workspace_id,
                label="primary",
                membership_role="owner_workspace",
                created_at=now,
            )
        )
        s.commit()
    return prop_id


# ---------------------------------------------------------------------------
# POST /role_grants
# ---------------------------------------------------------------------------


class TestCreate:
    def test_owner_grants_worker(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        with factory() as s:
            target = bootstrap_user(s, email="t1@example.com", display_name="T1")
            s.add(
                UserWorkspace(
                    user_id=target.id,
                    workspace_id=ws_id,
                    source="workspace_grant",
                    added_at=datetime.now(tz=UTC),
                )
            )
            s.commit()
            target_id = target.id
        client = _client(ctx, factory)
        resp = client.post(
            "/role_grants",
            json={"user_id": target_id, "grant_role": "worker"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["user_id"] == target_id
        assert body["grant_role"] == "worker"
        assert body["scope_property_id"] is None
        assert body["created_by_user_id"] == ctx.actor_id

    def test_owner_grants_manager(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        with factory() as s:
            target = bootstrap_user(s, email="m1@example.com", display_name="M1")
            s.commit()
            target_id = target.id
        client = _client(ctx, factory)
        resp = client.post(
            "/role_grants",
            json={"user_id": target_id, "grant_role": "manager"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["grant_role"] == "manager"

    def test_invalid_grant_role_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        # Pydantic catches the invalid Literal at validation time
        # (FastAPI returns 422 with its native body shape; we don't
        # need to assert the exact ``error`` code here).
        resp = client.post(
            "/role_grants",
            json={"user_id": "u1", "grant_role": "ghost"},
        )
        assert resp.status_code == 422

    def test_property_scope_in_workspace(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        prop_id = _seed_property_in_workspace(factory, workspace_id=ws_id)
        with factory() as s:
            target = bootstrap_user(s, email="p1@example.com", display_name="P1")
            s.commit()
            target_id = target.id
        client = _client(ctx, factory)
        resp = client.post(
            "/role_grants",
            json={
                "user_id": target_id,
                "grant_role": "worker",
                "scope_property_id": prop_id,
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["scope_property_id"] == prop_id

    def test_unknown_user_id_returns_404_user_not_found(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A non-existent ``user_id`` lands a typed 404, not a 500.

        Replaces the previous opaque 500 from the deferred FK
        ``role_grant.user_id -> user.id`` violation. The pre-flight
        existence probe in :func:`grant` raises
        :class:`RoleGrantUserNotFound`; the router maps it to a 404
        ``user_not_found`` envelope.
        """
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.post(
            "/role_grants",
            json={"user_id": "0", "grant_role": "worker"},
        )
        assert resp.status_code == 404, resp.text
        body = _assert_problem_error(resp, error="user_not_found")
        assert body["message"] == body["detail"]

    def test_cross_workspace_property_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        # Property exists but is not linked to this workspace.
        with factory() as s:
            s.add(
                Property(
                    id="01HWAFOREIGNPROPERTY00000",
                    name="Foreign",
                    kind="residence",
                    address="9 Foreign Lane",
                    address_json={},
                    country="XX",
                    timezone="UTC",
                    tags_json=[],
                    welcome_defaults_json={},
                    property_notes_md="",
                    created_at=datetime.now(tz=UTC),
                )
            )
            target = bootstrap_user(s, email="x@example.com", display_name="X")
            s.commit()
            target_id = target.id
        client = _client(ctx, factory)
        resp = client.post(
            "/role_grants",
            json={
                "user_id": target_id,
                "grant_role": "worker",
                "scope_property_id": "01HWAFOREIGNPROPERTY00000",
            },
        )
        assert resp.status_code == 422
        body = _assert_problem_error(resp, error="cross_workspace_property")
        assert body["message"] == body["detail"]

    def test_worker_cannot_grant_manager(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """A worker hitting POST /role_grants is rejected by the action gate."""
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        resp = client.post(
            "/role_grants",
            json={"user_id": "u1", "grant_role": "manager"},
        )
        assert resp.status_code == 403
        # The action gate fires before owner-authority — see Permission.
        assert resp.json()["detail"]["error"] == "permission_denied"

    def test_non_owner_manager_cannot_grant_manager(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A manager who is NOT in the owners group fails owner-authority."""
        ctx, factory, ws_id = owner_ctx
        # Seed a manager who is not in the owners permission group.
        with factory() as s:
            non_owner = bootstrap_user(
                s, email="no@example.com", display_name="NoOwner"
            )
            s.add(
                UserWorkspace(
                    user_id=non_owner.id,
                    workspace_id=ws_id,
                    source="workspace_grant",
                    added_at=datetime.now(tz=UTC),
                )
            )
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=non_owner.id,
                    grant_role="manager",
                    scope_property_id=None,
                    created_at=datetime.now(tz=UTC),
                    created_by_user_id=None,
                )
            )
            s.commit()
            non_owner_id = non_owner.id
        non_owner_ctx_obj = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=non_owner_id,
            grant_role="manager",
            actor_was_owner_member=False,
        )
        client = _client(non_owner_ctx_obj, factory)
        with factory() as s:
            target = bootstrap_user(s, email="x2@example.com", display_name="X2")
            s.commit()
            target_id = target.id
        resp = client.post(
            "/role_grants",
            json={"user_id": target_id, "grant_role": "manager"},
        )
        assert resp.status_code == 403
        body = _assert_problem_error(resp, error="not_authorized_for_role")
        assert body["message"] == body["detail"]


# ---------------------------------------------------------------------------
# DELETE /role_grants/{id}
# ---------------------------------------------------------------------------


class TestRevoke:
    def test_owner_revokes_worker_grant(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        with factory() as s:
            worker_id = seed_worker_user(
                s,
                workspace_id=ws_id,
                email="w@example.com",
                display_name="W",
            )
            s.commit()
        # Re-fetch the grant id from the DB.
        with factory() as s:
            row = (
                s.query(RoleGrant)
                .filter_by(user_id=worker_id, workspace_id=ws_id)
                .one()
            )
            grant_id = row.id
        client = _client(ctx, factory)
        resp = client.delete(f"/role_grants/{grant_id}")
        assert resp.status_code == 204, resp.text

    def test_revoke_unknown_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.delete("/role_grants/01HWANOTHERE00000000000000")
        assert resp.status_code == 404
        _assert_problem_error(resp, error="role_grant_not_found")

    def test_last_owner_manager_grant_protected(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Revoking the sole owner's manager grant must 409.

        ``bootstrap_workspace`` seeds the owner's ``manager`` role
        grant and adds them to the system ``owners`` group; revoking
        that grant is the canonical last-owner case.
        """
        ctx, factory, ws_id = owner_ctx
        with factory() as s:
            row = (
                s.query(RoleGrant)
                .filter_by(
                    user_id=ctx.actor_id,
                    workspace_id=ws_id,
                    grant_role="manager",
                )
                .one()
            )
            grant_id = row.id
        client = _client(ctx, factory)
        resp = client.delete(f"/role_grants/{grant_id}")
        assert resp.status_code == 409
        _assert_problem_error(resp, error="last_owner_grant_protected")


# ---------------------------------------------------------------------------
# PATCH /role_grants/{id}
# ---------------------------------------------------------------------------


class TestPatch:
    def test_patch_rescope_to_property(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        prop_id = _seed_property_in_workspace(factory, workspace_id=ws_id)
        with factory() as s:
            target = bootstrap_user(s, email="p2@example.com", display_name="P2")
            row = RoleGrant(
                id=new_ulid(),
                workspace_id=ws_id,
                user_id=target.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=datetime.now(tz=UTC),
                created_by_user_id=None,
            )
            s.add(row)
            s.commit()
            grant_id = row.id
        client = _client(ctx, factory)
        resp = client.patch(
            f"/role_grants/{grant_id}",
            json={"scope_property_id": prop_id},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["scope_property_id"] == prop_id

    def test_empty_patch_returns_200_unchanged(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        with factory() as s:
            target = bootstrap_user(s, email="p3@example.com", display_name="P3")
            row = RoleGrant(
                id=new_ulid(),
                workspace_id=ws_id,
                user_id=target.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=datetime.now(tz=UTC),
                created_by_user_id=None,
            )
            s.add(row)
            s.commit()
            grant_id = row.id
        client = _client(ctx, factory)
        resp = client.patch(f"/role_grants/{grant_id}", json={})
        assert resp.status_code == 200
        assert resp.json()["id"] == grant_id

    def test_patch_unknown_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.patch(
            "/role_grants/01HWAUNKNOWN00000000000000",
            json={"scope_property_id": None},
        )
        assert resp.status_code == 404

    def test_patch_cross_workspace_property_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        with factory() as s:
            s.add(
                Property(
                    id="01HWAFOREIGN0000000000000F",
                    name="Foreign",
                    kind="residence",
                    address="9 Foreign Lane",
                    address_json={},
                    country="XX",
                    timezone="UTC",
                    tags_json=[],
                    welcome_defaults_json={},
                    property_notes_md="",
                    created_at=datetime.now(tz=UTC),
                )
            )
            target = bootstrap_user(s, email="p4@example.com", display_name="P4")
            row = RoleGrant(
                id=new_ulid(),
                workspace_id=ws_id,
                user_id=target.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=datetime.now(tz=UTC),
                created_by_user_id=None,
            )
            s.add(row)
            s.commit()
            grant_id = row.id
        client = _client(ctx, factory)
        resp = client.patch(
            f"/role_grants/{grant_id}",
            json={"scope_property_id": "01HWAFOREIGN0000000000000F"},
        )
        assert resp.status_code == 422
        _assert_problem_error(resp, error="cross_workspace_property")

    def test_non_owner_manager_cannot_rescope_manager_grant(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """§05 owner-authority: only owners may re-scope a manager grant.

        A non-owner manager passes the ``role_grants.create`` action
        gate but the finer-grained rule fires inside the PATCH handler
        and surfaces as 403 ``not_authorized_for_role``. Without this
        check a non-owner could narrow the sole owner's manager grant
        to a single property (governance bypass).
        """
        ctx, factory, ws_id = owner_ctx
        # Seed a non-owner manager (no owners-group membership) and
        # use them as the caller. Also seed a manager grant on a
        # different user that we'll try to rescope.
        prop_id = _seed_property_in_workspace(factory, workspace_id=ws_id)
        with factory() as s:
            non_owner = bootstrap_user(
                s, email="rescope@example.com", display_name="Rescope"
            )
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=non_owner.id,
                    grant_role="manager",
                    scope_property_id=None,
                    created_at=datetime.now(tz=UTC),
                    created_by_user_id=None,
                )
            )
            target = bootstrap_user(
                s, email="target@example.com", display_name="Target"
            )
            target_grant = RoleGrant(
                id=new_ulid(),
                workspace_id=ws_id,
                user_id=target.id,
                grant_role="manager",
                scope_property_id=None,
                created_at=datetime.now(tz=UTC),
                created_by_user_id=None,
            )
            s.add(target_grant)
            s.commit()
            non_owner_id = non_owner.id
            grant_id = target_grant.id
        non_owner_ctx_obj = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=non_owner_id,
            grant_role="manager",
            actor_was_owner_member=False,
        )
        client = _client(non_owner_ctx_obj, factory)
        resp = client.patch(
            f"/role_grants/{grant_id}",
            json={"scope_property_id": prop_id},
        )
        assert resp.status_code == 403
        _assert_problem_error(resp, error="not_authorized_for_role")

    def test_non_owner_manager_can_rescope_worker_grant(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A non-owner manager may rescope a non-manager grant."""
        ctx, factory, ws_id = owner_ctx
        prop_id = _seed_property_in_workspace(factory, workspace_id=ws_id)
        with factory() as s:
            non_owner = bootstrap_user(s, email="resc2@example.com", display_name="R2")
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=non_owner.id,
                    grant_role="manager",
                    scope_property_id=None,
                    created_at=datetime.now(tz=UTC),
                    created_by_user_id=None,
                )
            )
            target = bootstrap_user(s, email="t2@example.com", display_name="T2")
            target_grant = RoleGrant(
                id=new_ulid(),
                workspace_id=ws_id,
                user_id=target.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=datetime.now(tz=UTC),
                created_by_user_id=None,
            )
            s.add(target_grant)
            s.commit()
            non_owner_id = non_owner.id
            grant_id = target_grant.id
        non_owner_ctx_obj = ctx_for(
            workspace_id=ws_id,
            workspace_slug=ctx.workspace_slug,
            actor_id=non_owner_id,
            grant_role="manager",
            actor_was_owner_member=False,
        )
        client = _client(non_owner_ctx_obj, factory)
        resp = client.patch(
            f"/role_grants/{grant_id}",
            json={"scope_property_id": prop_id},
        )
        assert resp.status_code == 200
        assert resp.json()["scope_property_id"] == prop_id


# ---------------------------------------------------------------------------
# GET /users/{id}/role_grants
# ---------------------------------------------------------------------------


class TestList:
    def test_list_owner_grants(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get(f"/users/{ctx.actor_id}/role_grants")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # ``bootstrap_workspace`` seeds one ``manager`` grant for the
        # owner.
        assert len(body["data"]) == 1
        assert body["data"][0]["user_id"] == ctx.actor_id
        assert body["data"][0]["grant_role"] == "manager"
        assert body["has_more"] is False
        assert body["next_cursor"] is None

    def test_list_with_property_filter(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        prop_id = _seed_property_in_workspace(factory, workspace_id=ws_id)
        with factory() as s:
            row = RoleGrant(
                id=new_ulid(),
                workspace_id=ws_id,
                user_id=ctx.actor_id,
                grant_role="worker",
                scope_property_id=prop_id,
                created_at=datetime.now(tz=UTC),
                created_by_user_id=None,
            )
            s.add(row)
            s.commit()
        client = _client(ctx, factory)
        resp = client.get(
            f"/users/{ctx.actor_id}/role_grants?scope_property_id={prop_id}"
        )
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["scope_property_id"] == prop_id

    def test_list_pagination(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        # Owner already has the bootstrap manager grant. Add 3 more
        # worker grants pinned to fresh ULIDs so the listing has 4 rows.
        # cd-x1xh's partial UNIQUE forbids two live grants with the
        # same ``(workspace, user, role, scope_property_id)`` tuple
        # (NULL collapsed to '' inside the index), so each extra grant
        # rides a distinct ``scope_property_id``.
        for _ in range(3):
            prop_id = _seed_property_in_workspace(factory, workspace_id=ws_id)
            with factory() as s:
                s.add(
                    RoleGrant(
                        id=new_ulid(),
                        workspace_id=ws_id,
                        user_id=ctx.actor_id,
                        grant_role="worker",
                        scope_property_id=prop_id,
                        created_at=datetime.now(tz=UTC),
                        created_by_user_id=None,
                    )
                )
                s.commit()
        client = _client(ctx, factory)
        resp = client.get(f"/users/{ctx.actor_id}/role_grants?limit=2")
        body = resp.json()
        assert len(body["data"]) == 2
        assert body["has_more"] is True
        assert body["next_cursor"] is not None
        # Second page.
        resp2 = client.get(
            f"/users/{ctx.actor_id}/role_grants?limit=2&cursor={body['next_cursor']}"
        )
        body2 = resp2.json()
        assert len(body2["data"]) == 2
        # Cumulative count covers all 4 rows.
        ids_seen = {r["id"] for r in body["data"]} | {r["id"] for r in body2["data"]}
        assert len(ids_seen) == 4

    def test_list_unknown_user_returns_empty(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get("/users/01HWAGHOST0000000000000000/role_grants")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_invalid_cursor_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get(f"/users/{ctx.actor_id}/role_grants?cursor=!!!not-base64!!!")
        assert resp.status_code == 422
        assert resp.json()["type"].endswith("/invalid_cursor")


# ---------------------------------------------------------------------------
# SSE event publishing — bus round-trip (cd-twyto)
# ---------------------------------------------------------------------------


class TestSseEvents:
    """Pin POST + DELETE to the matching ``app.events`` publish.

    The §05 Permissions page in a sibling tab refreshes its group
    catalogue (owners-derived membership) + resolver verdict via the
    SSE events emitted here; the SPA dispatcher round-trip is covered
    by ``mocks/web/src/lib/sse.ts`` + ``app/web/src/lib/sse.ts``.
    """

    def test_create_and_revoke_publish_grant_events(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        with factory() as s:
            target = bootstrap_user(s, email="grantee@example.com", display_name="G")
            s.add(
                UserWorkspace(
                    user_id=target.id,
                    workspace_id=ws_id,
                    source="workspace_grant",
                    added_at=datetime.now(tz=UTC),
                )
            )
            s.commit()
            target_id = target.id
        client = _client(ctx, factory)
        captured: list[RoleGrantCreated | RoleGrantRevoked] = []
        default_event_bus.subscribe(RoleGrantCreated)(captured.append)
        default_event_bus.subscribe(RoleGrantRevoked)(captured.append)

        try:
            created = client.post(
                "/role_grants",
                json={"user_id": target_id, "grant_role": "worker"},
            ).json()
            grant_id = created["id"]
            client.delete(f"/role_grants/{grant_id}")
        finally:
            default_event_bus._reset_for_tests()

        assert [type(event).name for event in captured] == [
            "role_grant.created",
            "role_grant.revoked",
        ]
        assert [event.workspace_id for event in captured] == [ws_id, ws_id]
        assert [event.grant_id for event in captured] == [grant_id, grant_id]
        assert [event.user_id for event in captured] == [target_id, target_id]

    def test_patch_rescope_publishes_grant_created_event(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """PATCH-rescope fires ``role_grant.created`` (mirrors group upsert).

        Without the publish a sibling tab on /permissions would keep
        a stale resolver verdict after a manager grant was narrowed
        to a single property. The SPA dispatcher reuses the ``created``
        handler — see the ``RoleGrantCreated`` docstring + the
        ``role_grant.created`` cases in ``mocks/web/src/lib/sse.ts``
        and ``app/web/src/lib/sse.ts``.
        """
        ctx, factory, ws_id = owner_ctx
        prop_id = _seed_property_in_workspace(factory, workspace_id=ws_id)
        with factory() as s:
            target = bootstrap_user(
                s, email="patch-target@example.com", display_name="PT"
            )
            row = RoleGrant(
                id=new_ulid(),
                workspace_id=ws_id,
                user_id=target.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=datetime.now(tz=UTC),
                created_by_user_id=None,
            )
            s.add(row)
            s.commit()
            grant_id = row.id
            target_id = target.id
        client = _client(ctx, factory)
        captured: list[RoleGrantCreated] = []
        default_event_bus.subscribe(RoleGrantCreated)(captured.append)

        try:
            resp = client.patch(
                f"/role_grants/{grant_id}",
                json={"scope_property_id": prop_id},
            )
        finally:
            default_event_bus._reset_for_tests()

        assert resp.status_code == 200, resp.text
        assert [type(event).name for event in captured] == ["role_grant.created"]
        assert captured[0].workspace_id == ws_id
        assert captured[0].grant_id == grant_id
        assert captured[0].user_id == target_id

    def test_empty_patch_does_not_publish(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Empty-body PATCH is a no-op read; no event must fire."""
        ctx, factory, ws_id = owner_ctx
        with factory() as s:
            target = bootstrap_user(
                s, email="patch-noop@example.com", display_name="PN"
            )
            row = RoleGrant(
                id=new_ulid(),
                workspace_id=ws_id,
                user_id=target.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=datetime.now(tz=UTC),
                created_by_user_id=None,
            )
            s.add(row)
            s.commit()
            grant_id = row.id
        client = _client(ctx, factory)
        captured: list[RoleGrantCreated] = []
        default_event_bus.subscribe(RoleGrantCreated)(captured.append)

        try:
            resp = client.patch(f"/role_grants/{grant_id}", json={})
        finally:
            default_event_bus._reset_for_tests()

        assert resp.status_code == 200
        assert captured == []


# ---------------------------------------------------------------------------
# OpenAPI shape
# ---------------------------------------------------------------------------


class TestOpenApiShape:
    def test_routes_carry_identity_tag(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        schema = client.get("/openapi.json").json()
        for path in (
            "/role_grants",
            "/role_grants/{grant_id}",
            "/users/{user_id}/role_grants",
        ):
            for op in schema["paths"][path].values():
                assert "identity" in op["tags"]
                assert "role_grants" in op["tags"]
