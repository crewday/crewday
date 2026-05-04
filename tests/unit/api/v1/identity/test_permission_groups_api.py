"""HTTP-level tests for ``/permission_groups`` (cd-jinb).

Exercises the full router surface against an in-memory SQLite engine
with the workspace ctx pinned and owner / worker personas seeded by
the sibling :mod:`tests.unit.api.v1.identity.conftest` fixtures.

Covers:

* CRUD happy paths (create / read / list / patch / delete).
* System-group protection on PATCH + DELETE.
* Slug uniqueness 409.
* Unknown action key 422 on capability writes.
* Membership add / remove (idempotent).
* Last-owner protection on member removal (with forensic audit row).
* Owners-membership writes gated on the root-only action.
* Worker rejection on ``groups.create`` / ``groups.edit``.
* Pagination + scope_kind filter on listing.
* OpenAPI tag shape.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import PermissionGroup, PermissionGroupMember
from app.api.v1.permission_groups import build_permission_groups_router
from app.events import (
    PermissionGroupDeleted,
    PermissionGroupMemberAdded,
    PermissionGroupMemberRemoved,
    PermissionGroupUpserted,
)
from app.events import (
    bus as default_event_bus,
)
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user
from tests.unit.api.v1.identity.conftest import build_client


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client(
        [("", build_permission_groups_router())],
        factory,
        ctx,
    )


def _seed_user_in_workspace(
    factory: sessionmaker[Session],
    *,
    email: str,
    display_name: str,
    workspace_id: str,
) -> str:
    """Seed a fresh user — used as a target for membership writes.

    Workspace membership is irrelevant for the permission-group
    tests; the model accepts arbitrary ``user_id`` values on the
    member row.
    """
    _ = workspace_id
    with factory() as s:
        user = bootstrap_user(s, email=email, display_name=display_name)
        s.commit()
        return user.id


# ---------------------------------------------------------------------------
# GET /permission_groups
# ---------------------------------------------------------------------------


class TestList:
    def test_list_includes_owners(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get("/permission_groups")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        slugs = {row["slug"] for row in body["data"]}
        # ``bootstrap_workspace`` seeds the owners system group.
        assert "owners" in slugs

    def test_list_organization_scope_returns_empty(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get("/permission_groups?scope_kind=organization")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_list_other_workspace_scope_id_returns_empty(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        # A scope_id from a sibling workspace must not enumerate.
        resp = client.get("/permission_groups?scope_kind=workspace&scope_id=other")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_list_pagination(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        # Owners group is already seeded; create 2 more so we have 3
        # rows and limit=2 surfaces a cursor.
        client.post("/permission_groups", json={"slug": "g1", "name": "G1"})
        client.post("/permission_groups", json={"slug": "g2", "name": "G2"})
        resp = client.get("/permission_groups?limit=2")
        body = resp.json()
        assert len(body["data"]) == 2
        assert body["has_more"] is True
        resp2 = client.get(f"/permission_groups?limit=2&cursor={body['next_cursor']}")
        body2 = resp2.json()
        # Total seen across both pages = 3.
        ids = {r["id"] for r in body["data"]} | {r["id"] for r in body2["data"]}
        assert len(ids) == 3

    def test_invalid_cursor_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get("/permission_groups?cursor=!!!bad!!!")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /permission_groups
# ---------------------------------------------------------------------------


class TestCreate:
    def test_owner_creates_group(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.post(
            "/permission_groups",
            json={
                "slug": "family",
                "name": "Family",
                "capabilities": {"tasks.create": True},
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["slug"] == "family"
        assert body["system"] is False
        assert body["capabilities"] == {"tasks.create": True}

    def test_unknown_capability_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.post(
            "/permission_groups",
            json={
                "slug": "x",
                "name": "X",
                "capabilities": {"not_a_real_action": True},
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"] == "unknown_action_key"

    def test_duplicate_slug_returns_409(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        client.post("/permission_groups", json={"slug": "team", "name": "Team"})
        resp = client.post(
            "/permission_groups", json={"slug": "team", "name": "Team 2"}
        )
        assert resp.status_code == 409
        assert resp.json()["error"] == "permission_group_slug_taken"

    def test_owners_slug_collides_with_seeded(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Re-creating ``owners`` collides with the bootstrap row."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.post(
            "/permission_groups",
            json={"slug": "owners", "name": "Fake Owners"},
        )
        assert resp.status_code == 409

    def test_worker_cannot_create(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        resp = client.post(
            "/permission_groups",
            json={"slug": "team", "name": "Team"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET / PATCH / DELETE /permission_groups/{id}
# ---------------------------------------------------------------------------


class TestReadUpdateDelete:
    def test_read_known(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        created = client.post(
            "/permission_groups", json={"slug": "x", "name": "X"}
        ).json()
        resp = client.get(f"/permission_groups/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["slug"] == "x"

    def test_read_unknown_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get("/permission_groups/01HWANOTHERE0000000000000A")
        assert resp.status_code == 404
        assert resp.json()["error"] == "permission_group_not_found"

    def test_patch_renames(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        created = client.post(
            "/permission_groups", json={"slug": "x", "name": "X"}
        ).json()
        resp = client.patch(
            f"/permission_groups/{created['id']}", json={"name": "Renamed"}
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed"

    def test_patch_capabilities_on_system_group_returns_409(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Mutating capabilities on the system ``owners`` group is rejected."""
        ctx, factory, ws_id = owner_ctx
        client = _client(ctx, factory)
        with factory() as s:
            row = (
                s.query(PermissionGroup)
                .filter_by(workspace_id=ws_id, slug="owners")
                .one()
            )
            owners_id = row.id
        resp = client.patch(
            f"/permission_groups/{owners_id}",
            json={"capabilities": {"tasks.create": True}},
        )
        assert resp.status_code == 409
        assert resp.json()["error"] == "system_group_protected"

    def test_patch_renames_system_group(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A name-only PATCH on a system group is allowed."""
        ctx, factory, ws_id = owner_ctx
        client = _client(ctx, factory)
        with factory() as s:
            row = (
                s.query(PermissionGroup)
                .filter_by(workspace_id=ws_id, slug="owners")
                .one()
            )
            owners_id = row.id
        resp = client.patch(f"/permission_groups/{owners_id}", json={"name": "Owners"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Owners"

    def test_patch_unknown_capability_returns_422(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        created = client.post(
            "/permission_groups", json={"slug": "x", "name": "X"}
        ).json()
        resp = client.patch(
            f"/permission_groups/{created['id']}",
            json={"capabilities": {"unknown.action": True}},
        )
        assert resp.status_code == 422
        assert resp.json()["error"] == "unknown_action_key"

    def test_delete_user_group(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        created = client.post(
            "/permission_groups", json={"slug": "x", "name": "X"}
        ).json()
        resp = client.delete(f"/permission_groups/{created['id']}")
        assert resp.status_code == 204

    def test_delete_system_group_returns_409(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        client = _client(ctx, factory)
        with factory() as s:
            row = (
                s.query(PermissionGroup)
                .filter_by(workspace_id=ws_id, slug="owners")
                .one()
            )
            owners_id = row.id
        resp = client.delete(f"/permission_groups/{owners_id}")
        assert resp.status_code == 409
        assert resp.json()["error"] == "system_group_protected"

    def test_delete_unknown_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.delete("/permission_groups/01HWAUNKNOWN000000000000DD")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


class TestMembers:
    def test_add_then_list_members(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        client = _client(ctx, factory)
        created = client.post(
            "/permission_groups", json={"slug": "team", "name": "Team"}
        ).json()
        target = _seed_user_in_workspace(
            factory, email="m@example.com", display_name="M", workspace_id=ws_id
        )
        resp = client.post(
            f"/permission_groups/{created['id']}/members",
            json={"user_id": target},
        )
        assert resp.status_code == 201
        assert resp.json()["user_id"] == target

        listing = client.get(f"/permission_groups/{created['id']}/members").json()
        assert any(m["user_id"] == target for m in listing["data"])

    def test_add_member_idempotent(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        client = _client(ctx, factory)
        created = client.post(
            "/permission_groups", json={"slug": "team", "name": "Team"}
        ).json()
        target = _seed_user_in_workspace(
            factory, email="m2@example.com", display_name="M2", workspace_id=ws_id
        )
        first = client.post(
            f"/permission_groups/{created['id']}/members",
            json={"user_id": target},
        )
        second = client.post(
            f"/permission_groups/{created['id']}/members",
            json={"user_id": target},
        )
        assert first.status_code == second.status_code == 201
        # Same row both times: same group + user identity. The
        # ``added_at`` timestamp is set on the first insert; the second
        # call short-circuits through the idempotency branch and returns
        # the existing row, so the user_id + group_id must match
        # (timestamp serialisation is backend-dependent — SQLite strips
        # tzinfo on a round-trip — so we don't assert on the exact
        # microsecond payload).
        assert first.json()["group_id"] == second.json()["group_id"]
        assert first.json()["user_id"] == second.json()["user_id"]

    def test_remove_member(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        client = _client(ctx, factory)
        created = client.post(
            "/permission_groups", json={"slug": "team", "name": "Team"}
        ).json()
        target = _seed_user_in_workspace(
            factory, email="m3@example.com", display_name="M3", workspace_id=ws_id
        )
        client.post(
            f"/permission_groups/{created['id']}/members",
            json={"user_id": target},
        )
        resp = client.delete(f"/permission_groups/{created['id']}/members/{target}")
        assert resp.status_code == 204

    def test_remove_member_idempotent_for_missing_row(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A remove against a non-member is a no-op (still 204)."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        created = client.post(
            "/permission_groups", json={"slug": "team", "name": "Team"}
        ).json()
        resp = client.delete(
            f"/permission_groups/{created['id']}/members/01HWASTRANGER0000000000000"
        )
        assert resp.status_code == 204

    def test_remove_member_unknown_group_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.delete(
            "/permission_groups/01HWANOPE0000000000000000A/members/01HWAUSER0000000000000000"
        )
        assert resp.status_code == 404

    def test_last_owner_removal_blocked(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Removing the sole member of ``owners`` must 422.

        The forensic ``member_remove_rejected`` audit row that the
        router writes on a fresh UoW is exercised by the domain-layer
        integration tests
        (:mod:`tests.integration.identity.test_owners_governance`).
        Here we cover the HTTP response shape and confirm the
        membership row stays intact through the rollback.
        """
        ctx, factory, ws_id = owner_ctx
        client = _client(ctx, factory)
        with factory() as s:
            owners = (
                s.query(PermissionGroup)
                .filter_by(workspace_id=ws_id, slug="owners")
                .one()
            )
            owners_id = owners.id
        resp = client.delete(f"/permission_groups/{owners_id}/members/{ctx.actor_id}")
        assert resp.status_code == 422
        assert resp.json()["error"] == "would_orphan_owners_group"
        # Membership stayed intact (the primary UoW rolled back).
        with factory() as s:
            row = s.get(PermissionGroupMember, (owners_id, ctx.actor_id))
            assert row is not None

    def test_owners_membership_writes_use_root_only_gate(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Adding a member to ``owners`` runs the root-only gate.

        For an owner ctx (``actor_was_owner_member=True``) the gate
        passes; we check the row landed.
        """
        ctx, factory, ws_id = owner_ctx
        client = _client(ctx, factory)
        target = _seed_user_in_workspace(
            factory, email="o2@example.com", display_name="O2", workspace_id=ws_id
        )
        with factory() as s:
            owners_id = (
                s.query(PermissionGroup)
                .filter_by(workspace_id=ws_id, slug="owners")
                .one()
                .id
            )
        resp = client.post(
            f"/permission_groups/{owners_id}/members",
            json={"user_id": target},
        )
        assert resp.status_code == 201
        # And the row really landed.
        with factory() as s:
            row = s.get(PermissionGroupMember, (owners_id, target))
            assert row is not None

    def test_worker_cannot_manage_members(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """A worker hitting POST .../members fails ``groups.manage_members``."""
        ctx, factory, ws_id, _ = worker_ctx
        # Seed a non-system group via direct DB insert so there is
        # something to add a member to.
        with factory() as s:
            group = PermissionGroup(
                id=new_ulid(),
                workspace_id=ws_id,
                slug="team",
                name="Team",
                system=False,
                capabilities_json={},
                created_at=datetime.now(tz=UTC),
            )
            s.add(group)
            s.commit()
            group_id = group.id
        client = _client(ctx, factory)
        resp = client.post(
            f"/permission_groups/{group_id}/members",
            json={"user_id": ctx.actor_id},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# SSE event publishing — bus round-trip (cd-twyto)
# ---------------------------------------------------------------------------


class TestSseEvents:
    """Pin each mutation to the matching ``app.events`` publish.

    The §02 / §05 Permissions page in a sibling tab refreshes its
    group list / per-group roster / resolver verdict via the SSE
    events emitted here; the SPA dispatcher round-trip is covered by
    ``mocks/web/src/lib/sse.ts`` + ``app/web/src/lib/sse.ts``.
    """

    def test_create_update_delete_publish_group_events(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        captured: list[PermissionGroupUpserted | PermissionGroupDeleted] = []
        default_event_bus.subscribe(PermissionGroupUpserted)(captured.append)
        default_event_bus.subscribe(PermissionGroupDeleted)(captured.append)

        try:
            created = client.post(
                "/permission_groups",
                json={"slug": "alerts", "name": "Alerts"},
            ).json()
            group_id = created["id"]
            client.patch(
                f"/permission_groups/{group_id}",
                json={"name": "Alerts (renamed)"},
            )
            client.delete(f"/permission_groups/{group_id}")
        finally:
            default_event_bus._reset_for_tests()

        assert [type(event).name for event in captured] == [
            "permission_group.upserted",
            "permission_group.upserted",
            "permission_group.deleted",
        ]
        assert [event.workspace_id for event in captured] == [ctx.workspace_id] * 3
        assert [event.group_id for event in captured] == [group_id] * 3

    def test_member_add_remove_publish_member_events(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        client = _client(ctx, factory)
        created = client.post(
            "/permission_groups",
            json={"slug": "team", "name": "Team"},
        ).json()
        group_id = created["id"]
        target = _seed_user_in_workspace(
            factory,
            email="member@example.com",
            display_name="Member",
            workspace_id=ws_id,
        )
        captured: list[PermissionGroupMemberAdded | PermissionGroupMemberRemoved] = []
        default_event_bus.subscribe(PermissionGroupMemberAdded)(captured.append)
        default_event_bus.subscribe(PermissionGroupMemberRemoved)(captured.append)

        try:
            client.post(
                f"/permission_groups/{group_id}/members",
                json={"user_id": target},
            )
            client.delete(f"/permission_groups/{group_id}/members/{target}")
        finally:
            default_event_bus._reset_for_tests()

        assert [type(event).name for event in captured] == [
            "permission_group_member.added",
            "permission_group_member.removed",
        ]
        assert [event.workspace_id for event in captured] == [ctx.workspace_id] * 2
        assert [event.group_id for event in captured] == [group_id] * 2
        assert [event.user_id for event in captured] == [target] * 2


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
            "/permission_groups",
            "/permission_groups/{group_id}",
            "/permission_groups/{group_id}/members",
            "/permission_groups/{group_id}/members/{user_id}",
        ):
            for op in schema["paths"][path].values():
                assert "identity" in op["tags"]
                assert "permission_groups" in op["tags"]
