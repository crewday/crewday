"""HTTP-level tests for ``/property_work_role_assignments`` (cd-za6n).

Covers the CRUD contract per spec §12 "Users / work roles / settings"
and the §02 / §05 invariants enforced by the domain service:

* Workspace parity: cross-workspace borrows of a ``user_work_role``
  collapse to 422 (the cross-workspace surface is invisible to the
  caller).
* Property reachability: a ``property_id`` not linked to the caller's
  workspace via ``property_workspace`` collapses to 422.
* Duplicate-active row: the partial UNIQUE on
  ``(user_work_role_id, property_id) WHERE deleted_at IS NULL``
  surfaces as 409 so the SPA can show a different toast.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.places.models import (
    Property,
    PropertyWorkRoleAssignment,
    PropertyWorkspace,
)
from app.adapters.db.workspace.models import UserWorkRole, UserWorkspace, WorkRole
from app.api.v1.property_work_role_assignments import (
    build_property_work_role_assignments_router,
)
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client, ctx_for


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client(
        [("", build_property_work_role_assignments_router())],
        factory,
        ctx,
    )


def _seed_property(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    name: str = "Villa Sud",
) -> str:
    """Seed a property + ``property_workspace`` junction row.

    Returns the property id. The junction binds the property to
    ``workspace_id`` as ``owner_workspace`` so the reachability check
    in the service finds it.
    """
    with factory() as s:
        prop = Property(
            id=new_ulid(),
            name=name,
            kind="vacation",
            address=f"{name} address",
            address_json={"country": "FR"},
            country="FR",
            locale=None,
            default_currency=None,
            timezone="Europe/Paris",
            lat=None,
            lon=None,
            client_org_id=None,
            owner_user_id=None,
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
            deleted_at=None,
        )
        s.add(prop)
        s.add(
            PropertyWorkspace(
                property_id=prop.id,
                workspace_id=workspace_id,
                label=name,
                membership_role="owner_workspace",
                created_at=datetime.now(tz=UTC),
            )
        )
        s.commit()
        return prop.id


def _seed_property_unlinked(factory: sessionmaker[Session]) -> str:
    """Seed a bare ``property`` row with no ``property_workspace`` link."""
    with factory() as s:
        prop = Property(
            id=new_ulid(),
            name="Orphan Villa",
            kind="vacation",
            address="—",
            address_json={"country": "FR"},
            country="FR",
            locale=None,
            default_currency=None,
            timezone="Europe/Paris",
            lat=None,
            lon=None,
            client_org_id=None,
            owner_user_id=None,
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
            deleted_at=None,
        )
        s.add(prop)
        s.commit()
        return prop.id


def _seed_work_role(factory: sessionmaker[Session], workspace_id: str) -> str:
    with factory() as s:
        row = WorkRole(
            id=new_ulid(),
            workspace_id=workspace_id,
            key=f"role-{new_ulid()[-6:]}",
            name="Maid",
            description_md="",
            default_settings_json={},
            icon_name="",
            created_at=datetime.now(tz=UTC),
            deleted_at=None,
        )
        s.add(row)
        s.commit()
        return row.id


def _seed_user_membership(
    factory: sessionmaker[Session], *, user_id: str, workspace_id: str
) -> None:
    with factory() as s:
        existing = s.get(UserWorkspace, (user_id, workspace_id))
        if existing is not None:
            return
        s.add(
            UserWorkspace(
                user_id=user_id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=datetime.now(tz=UTC),
            )
        )
        s.commit()


def _seed_user_work_role(
    factory: sessionmaker[Session],
    *,
    user_id: str,
    workspace_id: str,
    work_role_id: str,
) -> str:
    with factory() as s:
        row = UserWorkRole(
            id=new_ulid(),
            user_id=user_id,
            workspace_id=workspace_id,
            work_role_id=work_role_id,
            started_on=date(2026, 4, 1),
            ended_on=None,
            pay_rule_id=None,
            created_at=datetime.now(tz=UTC),
            deleted_at=None,
        )
        s.add(row)
        s.commit()
        return row.id


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_happy(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        role_id = _seed_work_role(factory, ws_id)
        uwr_id = _seed_user_work_role(
            factory, user_id=ctx.actor_id, workspace_id=ws_id, work_role_id=role_id
        )
        prop_id = _seed_property(factory, workspace_id=ws_id)
        client = _client(ctx, factory)

        resp = client.post(
            "/property_work_role_assignments",
            json={
                "user_work_role_id": uwr_id,
                "property_id": prop_id,
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["user_work_role_id"] == uwr_id
        assert body["property_id"] == prop_id
        assert body["workspace_id"] == ws_id
        assert body["schedule_ruleset_id"] is None
        assert body["property_pay_rule_id"] is None
        assert body["deleted_at"] is None

    def test_create_carries_schedule_ruleset(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """The optional ``schedule_ruleset_id`` pointer round-trips.

        ``schedule_ruleset_id`` is a soft reference (the
        ``schedule_ruleset`` table does not yet exist; §06 lands it in
        a sibling task), so any string id round-trips here. The FK-
        bound ``property_pay_rule_id`` is exercised in the integration
        suite where a real ``pay_rule`` row can be seeded.
        """
        ctx, factory, ws_id = owner_ctx
        role_id = _seed_work_role(factory, ws_id)
        uwr_id = _seed_user_work_role(
            factory, user_id=ctx.actor_id, workspace_id=ws_id, work_role_id=role_id
        )
        prop_id = _seed_property(factory, workspace_id=ws_id)
        client = _client(ctx, factory)

        resp = client.post(
            "/property_work_role_assignments",
            json={
                "user_work_role_id": uwr_id,
                "property_id": prop_id,
                "schedule_ruleset_id": "01HWRULESET00000000000000",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["schedule_ruleset_id"] == "01HWRULESET00000000000000"
        assert body["property_pay_rule_id"] is None

    def test_create_403_worker(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """A worker grant cannot mint per-property pinnings."""
        ctx, factory, ws_id, _ = worker_ctx
        role_id = _seed_work_role(factory, ws_id)
        uwr_id = _seed_user_work_role(
            factory, user_id=ctx.actor_id, workspace_id=ws_id, work_role_id=role_id
        )
        prop_id = _seed_property(factory, workspace_id=ws_id)
        client = _client(ctx, factory)

        resp = client.post(
            "/property_work_role_assignments",
            json={"user_work_role_id": uwr_id, "property_id": prop_id},
        )
        assert resp.status_code == 403

    def test_create_409_duplicate_active(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Re-pinning the same (role, property) while a live row exists is 409."""
        ctx, factory, ws_id = owner_ctx
        role_id = _seed_work_role(factory, ws_id)
        uwr_id = _seed_user_work_role(
            factory, user_id=ctx.actor_id, workspace_id=ws_id, work_role_id=role_id
        )
        prop_id = _seed_property(factory, workspace_id=ws_id)
        client = _client(ctx, factory)

        first = client.post(
            "/property_work_role_assignments",
            json={"user_work_role_id": uwr_id, "property_id": prop_id},
        )
        assert first.status_code == 201, first.text

        second = client.post(
            "/property_work_role_assignments",
            json={"user_work_role_id": uwr_id, "property_id": prop_id},
        )
        assert second.status_code == 409
        assert (
            second.json()["detail"]["error"]
            == "property_work_role_assignment_duplicate"
        )

    def test_create_422_property_not_in_workspace(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """``property_id`` without a ``property_workspace`` link → 422."""
        ctx, factory, ws_id = owner_ctx
        role_id = _seed_work_role(factory, ws_id)
        uwr_id = _seed_user_work_role(
            factory, user_id=ctx.actor_id, workspace_id=ws_id, work_role_id=role_id
        )
        prop_id = _seed_property_unlinked(factory)
        client = _client(ctx, factory)

        resp = client.post(
            "/property_work_role_assignments",
            json={"user_work_role_id": uwr_id, "property_id": prop_id},
        )
        assert resp.status_code == 422
        assert (
            resp.json()["detail"]["error"] == "property_work_role_assignment_invariant"
        )
        assert "not linked" in resp.json()["detail"]["message"]

    def test_create_422_user_work_role_not_in_workspace(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A user_work_role id from another workspace is invisible → 422."""
        ctx, factory, ws_id = owner_ctx
        # A user_work_role id that does not exist anywhere — same surface
        # as a cross-workspace borrow attempt (tenant filter collapses
        # both into "row not found in this workspace").
        prop_id = _seed_property(factory, workspace_id=ws_id)
        client = _client(ctx, factory)

        resp = client.post(
            "/property_work_role_assignments",
            json={
                "user_work_role_id": "01HWUNKNOWN000000000000000",
                "property_id": prop_id,
            },
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"]["error"] == "property_work_role_assignment_invariant"
        assert "user_work_role" in body["detail"]["message"]


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestList:
    def test_list_paginated(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Cursor envelope walks forward across multiple pages."""
        ctx, factory, ws_id = owner_ctx
        role_id = _seed_work_role(factory, ws_id)
        uwr_id = _seed_user_work_role(
            factory, user_id=ctx.actor_id, workspace_id=ws_id, work_role_id=role_id
        )
        prop_ids = [
            _seed_property(factory, workspace_id=ws_id, name=f"Villa {i}")
            for i in range(3)
        ]
        client = _client(ctx, factory)
        for pid in prop_ids:
            r = client.post(
                "/property_work_role_assignments",
                json={"user_work_role_id": uwr_id, "property_id": pid},
            )
            assert r.status_code == 201, r.text

        resp = client.get("/property_work_role_assignments?limit=2")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["has_more"] is True
        assert body["next_cursor"] is not None
        assert len(body["data"]) == 2

        resp2 = client.get(
            f"/property_work_role_assignments?cursor={body['next_cursor']}&limit=2"
        )
        body2 = resp2.json()
        assert resp2.status_code == 200
        assert body2["has_more"] is False
        assert len(body2["data"]) == 1

    def test_list_filters_by_property(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """``?property_id=…`` narrows to a single property."""
        ctx, factory, ws_id = owner_ctx
        role_id = _seed_work_role(factory, ws_id)
        uwr_id = _seed_user_work_role(
            factory, user_id=ctx.actor_id, workspace_id=ws_id, work_role_id=role_id
        )
        prop_a = _seed_property(factory, workspace_id=ws_id, name="A")
        prop_b = _seed_property(factory, workspace_id=ws_id, name="B")
        client = _client(ctx, factory)
        for pid in (prop_a, prop_b):
            client.post(
                "/property_work_role_assignments",
                json={"user_work_role_id": uwr_id, "property_id": pid},
            )

        resp = client.get(f"/property_work_role_assignments?property_id={prop_a}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["property_id"] == prop_a

    def test_list_filters_by_user_work_role(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """``?user_work_role_id=…`` narrows to one user_work_role."""
        ctx, factory, ws_id = owner_ctx
        role_a = _seed_work_role(factory, ws_id)
        role_b = _seed_work_role(factory, ws_id)
        uwr_a = _seed_user_work_role(
            factory, user_id=ctx.actor_id, workspace_id=ws_id, work_role_id=role_a
        )
        uwr_b = _seed_user_work_role(
            factory, user_id=ctx.actor_id, workspace_id=ws_id, work_role_id=role_b
        )
        prop = _seed_property(factory, workspace_id=ws_id)
        # uwr_a + uwr_b each pin to the same property — distinct rows
        # under the partial UNIQUE because the (role, property) pair
        # differs by role.
        client = _client(ctx, factory)
        for uwr in (uwr_a, uwr_b):
            r = client.post(
                "/property_work_role_assignments",
                json={"user_work_role_id": uwr, "property_id": prop},
            )
            assert r.status_code == 201, r.text

        resp = client.get(f"/property_work_role_assignments?user_work_role_id={uwr_a}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["user_work_role_id"] == uwr_a


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestPatch:
    def test_update_partial(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """PATCH body lands only the sent fields.

        Touches ``schedule_ruleset_id`` only — it's a soft reference
        with no FK so any id value is fine in this unit test. The
        FK-bound ``property_pay_rule_id`` exercise lives in the
        integration suite where a real ``pay_rule`` row can be seeded.
        """
        ctx, factory, ws_id = owner_ctx
        role_id = _seed_work_role(factory, ws_id)
        uwr_id = _seed_user_work_role(
            factory, user_id=ctx.actor_id, workspace_id=ws_id, work_role_id=role_id
        )
        prop_id = _seed_property(factory, workspace_id=ws_id)
        client = _client(ctx, factory)
        created = client.post(
            "/property_work_role_assignments",
            json={
                "user_work_role_id": uwr_id,
                "property_id": prop_id,
                "schedule_ruleset_id": "01HWRULESET00000000000000",
            },
        ).json()

        resp = client.patch(
            f"/property_work_role_assignments/{created['id']}",
            json={"schedule_ruleset_id": "01HWRULESET11111111111111"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["schedule_ruleset_id"] == "01HWRULESET11111111111111"
        assert body["property_pay_rule_id"] is None

    def test_patch_unknown_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.patch(
            "/property_work_role_assignments/nope",
            json={"schedule_ruleset_id": "01HWRULESET00000000000000"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_soft_deletes(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """DELETE stamps ``deleted_at`` and returns 204."""
        ctx, factory, ws_id = owner_ctx
        role_id = _seed_work_role(factory, ws_id)
        uwr_id = _seed_user_work_role(
            factory, user_id=ctx.actor_id, workspace_id=ws_id, work_role_id=role_id
        )
        prop_id = _seed_property(factory, workspace_id=ws_id)
        client = _client(ctx, factory)
        created = client.post(
            "/property_work_role_assignments",
            json={"user_work_role_id": uwr_id, "property_id": prop_id},
        ).json()

        resp = client.delete(f"/property_work_role_assignments/{created['id']}")
        assert resp.status_code == 204

        # Row lives in DB but is invisible to the default lookup.
        with factory() as s:
            row = s.get(PropertyWorkRoleAssignment, created["id"])
            assert row is not None
            assert row.deleted_at is not None

        # Re-pin after archive mints a fresh row (partial UNIQUE
        # excludes tombstones — covers the "archive + re-pin" flow).
        repin = client.post(
            "/property_work_role_assignments",
            json={"user_work_role_id": uwr_id, "property_id": prop_id},
        )
        assert repin.status_code == 201, repin.text
        assert repin.json()["id"] != created["id"]

    def test_delete_unknown_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.delete("/property_work_role_assignments/nope")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cross-workspace
# ---------------------------------------------------------------------------


class TestCrossWorkspace:
    def test_cross_workspace_blocked(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A row in workspace A is invisible from workspace B's caller.

        Seeds two workspaces in the same DB. The owner of WS-A creates
        a row; the WS-B owner cannot read, patch, or delete it. The
        404 surface is identical to "never existed" per §01 "tenant
        surface is not enumerable".
        """
        ctx_a, factory, ws_a_id = owner_ctx
        role_id = _seed_work_role(factory, ws_a_id)
        uwr_id = _seed_user_work_role(
            factory,
            user_id=ctx_a.actor_id,
            workspace_id=ws_a_id,
            work_role_id=role_id,
        )
        prop_id = _seed_property(factory, workspace_id=ws_a_id)
        client_a = _client(ctx_a, factory)
        created = client_a.post(
            "/property_work_role_assignments",
            json={"user_work_role_id": uwr_id, "property_id": prop_id},
        ).json()

        # Stand up workspace B alongside.
        with factory() as s:
            owner_b = bootstrap_user(
                s, email="owner-b@example.com", display_name="Owner B"
            )
            ws_b = bootstrap_workspace(
                s, slug="ws-b", name="WS B", owner_user_id=owner_b.id
            )
            s.commit()
            ctx_b = ctx_for(
                workspace_id=ws_b.id,
                workspace_slug=ws_b.slug,
                actor_id=owner_b.id,
                grant_role="manager",
                actor_was_owner_member=True,
            )

        client_b = _client(ctx_b, factory)
        # Listing is empty — WS-B owns no rows.
        listing = client_b.get("/property_work_role_assignments")
        assert listing.status_code == 200
        assert listing.json()["data"] == []

        # PATCH and DELETE on the WS-A row collapse to 404. The 404
        # short-circuits before the FK-bound ``property_pay_rule_id``
        # would even matter — see the integration suite for the
        # FK-validated round-trip.
        patch_resp = client_b.patch(
            f"/property_work_role_assignments/{created['id']}",
            json={"schedule_ruleset_id": "01HWRULESET11111111111111"},
        )
        assert patch_resp.status_code == 404

        del_resp = client_b.delete(f"/property_work_role_assignments/{created['id']}")
        assert del_resp.status_code == 404


# ---------------------------------------------------------------------------
# Authz gate (workers blocked on every verb)
# ---------------------------------------------------------------------------


class TestAuthzGate:
    """``work_roles.manage`` gates list, create, update, delete alike.

    The §05 action catalog defaults ``work_roles.manage`` to
    ``owners, managers``; per-property pinnings are roster info that
    intentionally does not fan out to ``worker`` grants. Each verb
    must reject a worker grant with 403 — including the listing,
    which tells the SPA "where does this employee work" and is
    therefore coarser than ``scope.view``.
    """

    def test_list_403_worker(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        resp = client.get("/property_work_role_assignments")
        assert resp.status_code == 403

    def test_patch_403_worker(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        # The action gate runs at the workspace scope ahead of any
        # row-level surface, so an unknown id still surfaces 403.
        resp = client.patch(
            "/property_work_role_assignments/nope",
            json={"schedule_ruleset_id": "01HWRULESET00000000000000"},
        )
        assert resp.status_code == 403

    def test_delete_403_worker(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        resp = client.delete("/property_work_role_assignments/nope")
        assert resp.status_code == 403


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
        op = schema["paths"]["/property_work_role_assignments"]["get"]
        assert "identity" in op["tags"]
        assert "property_work_role_assignments" in op["tags"]
