"""HTTP-level tests for ``/employees`` (cd-g6nf, cd-jtgo).

Exercises the manager roster endpoint:

* manager / owner can list the workspace roster (200, valid shape);
* worker is rejected with 403 ``permission_denied``;
* cross-workspace bleed-through is impossible (membership rows in
  another workspace stay invisible);
* the projection joins ``users``, ``work_engagement``,
  ``user_work_role``, ``role_grant``, and ``property_workspace``
  correctly (role chip set, property fan-out, started_on
  resolution).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.workspace.models import (
    UserWorkRole,
    UserWorkspace,
    WorkEngagement,
    WorkRole,
)
from app.api.v1.employees import build_employees_router
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", build_employees_router())], factory, ctx)


def _seed_work_role(
    factory: sessionmaker[Session], *, workspace_id: str, key: str
) -> str:
    with factory() as s:
        row = WorkRole(
            id=new_ulid(),
            workspace_id=workspace_id,
            key=key,
            name=key.title(),
            description_md="",
            default_settings_json={},
            icon_name="",
            created_at=_PINNED,
            deleted_at=None,
        )
        s.add(row)
        s.commit()
        return row.id


def _seed_user_work_role(
    factory: sessionmaker[Session],
    *,
    user_id: str,
    workspace_id: str,
    work_role_id: str,
    deleted: bool = False,
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
            created_at=_PINNED,
            deleted_at=_PINNED if deleted else None,
        )
        s.add(row)
        s.commit()
        return row.id


def _seed_engagement(
    factory: sessionmaker[Session],
    *,
    user_id: str,
    workspace_id: str,
    started_on: date = date(2026, 4, 1),
    archived_on: date | None = None,
) -> str:
    with factory() as s:
        row = WorkEngagement(
            id=new_ulid(),
            user_id=user_id,
            workspace_id=workspace_id,
            engagement_kind="payroll",
            supplier_org_id=None,
            pay_destination_id=None,
            reimbursement_destination_id=None,
            started_on=started_on,
            archived_on=archived_on,
            notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        s.add(row)
        s.commit()
        return row.id


def _seed_property(
    factory: sessionmaker[Session], *, workspace_id: str, label: str = "Villa"
) -> str:
    """Insert a :class:`Property` row + the workspace junction."""
    with factory() as s:
        prop = Property(
            id=new_ulid(),
            name=label,
            address="1 Test Street",
            timezone="UTC",
            created_at=_PINNED,
        )
        s.add(prop)
        s.flush()
        s.add(
            PropertyWorkspace(
                property_id=prop.id,
                workspace_id=workspace_id,
                label=label,
                membership_role="owner_workspace",
                created_at=_PINNED,
            )
        )
        s.commit()
        return prop.id


def _seed_role_grant(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
    grant_role: str = "worker",
    scope_property_id: str | None = None,
) -> str:
    with factory() as s:
        row = RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_property_id=scope_property_id,
            created_at=_PINNED,
            created_by_user_id=None,
        )
        s.add(row)
        s.commit()
        return row.id


def _seed_member(
    factory: sessionmaker[Session], *, user_id: str, workspace_id: str
) -> None:
    """Add a :class:`UserWorkspace` membership row.

    Mirrors :func:`tests.unit.api.v1.identity.conftest.seed_worker_user`'s
    membership shape but takes an already-created user id; some tests
    seed the user via :func:`bootstrap_user` and only need the
    junction row.
    """
    with factory() as s:
        s.add(
            UserWorkspace(
                user_id=user_id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=_PINNED,
            )
        )
        s.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestList:
    def test_owner_lists_self_in_empty_workspace(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Owner sees themselves — bootstrap_workspace seeded the row."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get("/employees")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["id"] == ctx.actor_id
        assert body[0]["email"] == "owner@example.com"
        assert body[0]["name"] == "Owner"

    def test_returns_bare_array_not_envelope(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Critical contract: SPA expects ``Employee[]``, not ``{data, ...}``.

        cd-g6nf records the bare-array decision; if a future refactor
        introduces the standard ``{data, next_cursor, has_more}``
        envelope it MUST also migrate every ``fetchJson<Employee[]>``
        call site in the SPA in lockstep. This assertion is the
        sentinel that catches a one-sided change.
        """
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        body = client.get("/employees").json()
        assert isinstance(body, list), (
            "GET /employees must return a JSON array — see cd-g6nf"
        )
        # Defensive — even an empty roster must stay a list, not a
        # dict with a `data` key. The envelope shape would surface as
        # `{}` or `{"data": []}` here on a regression.
        for row in body:
            assert isinstance(row, dict)
            assert "data" not in row, "envelope sentinel leaked into row"

    def test_lists_multiple_employees(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        with factory() as s:
            extra = bootstrap_user(s, email="bob@example.com", display_name="Bob Smith")
            s.commit()
            extra_id = extra.id
        _seed_member(factory, user_id=extra_id, workspace_id=ws_id)
        client = _client(ctx, factory)
        body = client.get("/employees").json()
        assert {row["id"] for row in body} == {ctx.actor_id, extra_id}
        bob = next(row for row in body if row["id"] == extra_id)
        assert bob["name"] == "Bob Smith"
        assert bob["avatar_initials"] == "BS"

    def test_started_on_from_engagement(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Active engagement.started_on wins over user.created_at."""
        ctx, factory, ws_id = owner_ctx
        _seed_engagement(
            factory,
            user_id=ctx.actor_id,
            workspace_id=ws_id,
            started_on=date(2025, 1, 1),
        )
        client = _client(ctx, factory)
        body = client.get("/employees").json()
        owner_row = next(r for r in body if r["id"] == ctx.actor_id)
        assert owner_row["started_on"] == "2025-01-01"

    def test_started_on_falls_back_when_no_engagement(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """User without an active engagement falls back to user.created_at.date()."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        body = client.get("/employees").json()
        owner_row = next(r for r in body if r["id"] == ctx.actor_id)
        # Bootstrap_user uses SystemClock().now() — assert the fallback
        # is a parseable date, not the engagement-driven value.
        assert isinstance(owner_row["started_on"], str)
        assert len(owner_row["started_on"]) == 10  # YYYY-MM-DD

    def test_archived_engagement_does_not_drive_started_on(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """An archived-only engagement is treated as no active engagement."""
        ctx, factory, ws_id = owner_ctx
        _seed_engagement(
            factory,
            user_id=ctx.actor_id,
            workspace_id=ws_id,
            started_on=date(2025, 1, 1),
            archived_on=date(2026, 1, 1),
        )
        client = _client(ctx, factory)
        body = client.get("/employees").json()
        owner_row = next(r for r in body if r["id"] == ctx.actor_id)
        # Falls back to user.created_at.date() (not 2025-01-01 from
        # the archived engagement).
        assert owner_row["started_on"] != "2025-01-01"

    def test_role_chip_set_from_active_user_work_roles(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        maid_role = _seed_work_role(factory, workspace_id=ws_id, key="maid")
        cook_role = _seed_work_role(factory, workspace_id=ws_id, key="cook")
        _seed_user_work_role(
            factory,
            user_id=ctx.actor_id,
            workspace_id=ws_id,
            work_role_id=maid_role,
        )
        _seed_user_work_role(
            factory,
            user_id=ctx.actor_id,
            workspace_id=ws_id,
            work_role_id=cook_role,
        )
        client = _client(ctx, factory)
        body = client.get("/employees").json()
        row = next(r for r in body if r["id"] == ctx.actor_id)
        assert row["roles"] == ["cook", "maid"]  # sorted by key

    def test_soft_deleted_role_excluded(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        gone_role = _seed_work_role(factory, workspace_id=ws_id, key="driver")
        _seed_user_work_role(
            factory,
            user_id=ctx.actor_id,
            workspace_id=ws_id,
            work_role_id=gone_role,
            deleted=True,
        )
        client = _client(ctx, factory)
        body = client.get("/employees").json()
        row = next(r for r in body if r["id"] == ctx.actor_id)
        assert "driver" not in row["roles"]

    def test_property_fan_out_for_workspace_grant(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Workspace-scoped grant fans out across every linked property."""
        ctx, factory, ws_id = owner_ctx
        prop_a = _seed_property(factory, workspace_id=ws_id, label="Villa A")
        prop_b = _seed_property(factory, workspace_id=ws_id, label="Villa B")
        _seed_role_grant(
            factory,
            workspace_id=ws_id,
            user_id=ctx.actor_id,
            grant_role="worker",
            scope_property_id=None,
        )
        client = _client(ctx, factory)
        body = client.get("/employees").json()
        row = next(r for r in body if r["id"] == ctx.actor_id)
        assert set(row["properties"]) == {prop_a, prop_b}

    def test_property_pinned_grant_narrows_set(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Property-scoped grant narrows to the pinned property only.

        Uses a freshly seeded worker user (not the owner ctx actor)
        because :func:`bootstrap_workspace` already seeds the owner
        with a workspace-scoped manager role grant — fanning out the
        property set to every linked property would mask the narrowing
        check.
        """
        ctx, factory, ws_id = owner_ctx
        prop_a = _seed_property(factory, workspace_id=ws_id, label="Villa A")
        _seed_property(factory, workspace_id=ws_id, label="Villa B")
        with factory() as s:
            worker = bootstrap_user(
                s, email="prop-worker@example.com", display_name="Prop Worker"
            )
            s.commit()
            worker_id = worker.id
        _seed_member(factory, user_id=worker_id, workspace_id=ws_id)
        _seed_role_grant(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            grant_role="worker",
            scope_property_id=prop_a,
        )
        client = _client(ctx, factory)
        body = client.get("/employees").json()
        row = next(r for r in body if r["id"] == worker_id)
        assert row["properties"] == [prop_a]


class TestAuthZ:
    def test_worker_returns_403(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Workers do not hold ``employees.read`` — must be 403."""
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        resp = client.get("/employees")
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "permission_denied"

    def test_manager_owner_succeeds(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Owner / manager surface holds the gate by default-allow."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get("/employees")
        assert resp.status_code == 200, resp.text


class TestTenancy:
    def test_cross_workspace_membership_invisible(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A membership in workspace B never bleeds into workspace A's roster.

        Seeds a sibling workspace + user with their own membership +
        engagement + role grant rows, then asserts the original
        workspace's ``/employees`` listing does NOT include the
        sibling.
        """
        ctx, factory, _ws_a_id = owner_ctx
        with factory() as s:
            sibling_owner = bootstrap_user(
                s, email="other@example.com", display_name="Other Owner"
            )
            ws_b = bootstrap_workspace(
                s,
                slug="ws-sibling",
                name="Sibling WS",
                owner_user_id=sibling_owner.id,
            )
            s.commit()
            ws_b_id = ws_b.id
            sibling_id = sibling_owner.id
        # Seed sibling workspace data — engagement + role grant +
        # work role linkage. None of this should surface under ws-a.
        _seed_engagement(factory, user_id=sibling_id, workspace_id=ws_b_id)
        sibling_role = _seed_work_role(factory, workspace_id=ws_b_id, key="maid")
        _seed_user_work_role(
            factory,
            user_id=sibling_id,
            workspace_id=ws_b_id,
            work_role_id=sibling_role,
        )

        client = _client(ctx, factory)
        body = client.get("/employees").json()
        assert all(row["id"] != sibling_id for row in body)
        # The owner should still see themselves in their own
        # workspace — sanity check the cross-tenancy filter did not
        # over-restrict.
        assert any(row["id"] == ctx.actor_id for row in body)

    def test_no_active_engagement_in_other_workspace_leaks(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """An engagement in workspace B for a user in workspace A is invisible.

        Slightly different from the previous test: the user has a
        membership in workspace A (the caller's workspace) but their
        active engagement lives in workspace B. The roster row for
        workspace A must NOT pick up workspace B's engagement —
        ``started_on`` must come from the user's identity row instead.
        """
        ctx, factory, ws_a_id = owner_ctx
        with factory() as s:
            cross_user = bootstrap_user(
                s, email="cross@example.com", display_name="Cross User"
            )
            ws_b = bootstrap_workspace(
                s,
                slug="ws-cross",
                name="Cross WS",
                owner_user_id=cross_user.id,
            )
            s.commit()
            ws_b_id = ws_b.id
            cross_id = cross_user.id
        # Membership in workspace A — this is what makes the user
        # visible on the A roster.
        _seed_member(factory, user_id=cross_id, workspace_id=ws_a_id)
        # Engagement in workspace B — should NOT drive started_on for
        # the workspace A roster row.
        _seed_engagement(
            factory,
            user_id=cross_id,
            workspace_id=ws_b_id,
            started_on=date(2024, 6, 1),
        )

        client = _client(ctx, factory)
        body = client.get("/employees").json()
        cross_row = next(r for r in body if r["id"] == cross_id)
        # If workspace B's engagement leaked in, started_on would be
        # 2024-06-01. Asserting the absence of that string is the
        # cleanest way to express "no leak" without coupling to the
        # exact fallback value.
        assert cross_row["started_on"] != "2024-06-01"


class TestShape:
    def test_response_carries_every_employee_field(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Every key in app/web/src/types/employee.ts must round-trip."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        body = client.get("/employees").json()
        row = body[0]
        # SPA-required field set — keep this assertion in lockstep
        # with ``app/web/src/types/employee.ts``. A mismatch surfaces
        # as a TypeError in the SPA before render, so the contract is
        # cheap to enforce here.
        expected = {
            "id",
            "name",
            "roles",
            "properties",
            "avatar_initials",
            "avatar_file_id",
            "avatar_url",
            "phone",
            "email",
            "started_on",
            "capabilities",
            "workspaces",
            "villas",
            "language",
            "weekly_availability",
            "evidence_policy",
            "preferred_locale",
            "settings_override",
        }
        assert set(row.keys()) >= expected

    def test_avatar_initials_handles_unicode(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Display names with accents / emoji round-trip correctly."""
        ctx, factory, ws_id = owner_ctx
        with factory() as s:
            extra = bootstrap_user(s, email="ana@example.com", display_name="Ana Élisa")
            s.commit()
            extra_id = extra.id
        _seed_member(factory, user_id=extra_id, workspace_id=ws_id)
        client = _client(ctx, factory)
        body = client.get("/employees").json()
        ana = next(r for r in body if r["id"] == extra_id)
        # First char of each of the two leading tokens, uppercased.
        assert ana["avatar_initials"] == "AÉ"

    def test_whitespace_display_name_falls_back_to_email_local_part(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A whitespace-only display name falls back gracefully.

        ``name`` resolves to the email's local part (not the literal
        whitespace), and ``avatar_initials`` matches the resolved
        name — never an empty cell. This is the contract the SPA's
        roster pages rely on: a non-empty ``name`` and matching
        initials, even on a malformed identity row.
        """
        ctx, factory, ws_id = owner_ctx
        with factory() as s:
            extra = bootstrap_user(s, email="ghost@example.com", display_name="   ")
            s.commit()
            extra_id = extra.id
        _seed_member(factory, user_id=extra_id, workspace_id=ws_id)
        client = _client(ctx, factory)
        body = client.get("/employees").json()
        ghost = next(r for r in body if r["id"] == extra_id)
        assert ghost["name"] == "ghost"
        assert ghost["avatar_initials"] == "G"

    def test_openapi_carries_identity_and_employees_tags(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        schema = client.get("/openapi.json").json()
        list_op = schema["paths"]["/employees"]["get"]
        assert "identity" in list_op["tags"]
        assert "employees" in list_op["tags"]
        assert list_op["operationId"] == "employees.list"


class TestEmptyWorkspace:
    """The flat-array contract requires a true ``[]`` on an empty roster.

    The "owner sees self" baseline always carries one row because
    :func:`bootstrap_workspace` seeds an owner ``UserWorkspace`` row.
    To exercise the ``user_ids == []`` branch we have to delete the
    membership behind the active ctx. The handler still returns 200
    with an empty list — never crashes, never leaks a sibling
    workspace's roster, never returns a ``{data: []}`` envelope.
    """

    def test_no_members_returns_empty_array(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        with factory() as s, tenant_agnostic():
            s.query(UserWorkspace).filter(UserWorkspace.workspace_id == ws_id).delete()
            s.commit()
        client = _client(ctx, factory)
        resp = client.get("/employees")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == []


class TestSoftDelete:
    """Soft-deleted joined rows must not bleed into the projection.

    ``user_work_role.deleted_at`` (assignment-level) and
    ``work_role.deleted_at`` (catalogue-level) both retire role
    chips; ``property.deleted_at`` retires property assignments. The
    cascades live in the domain services — these tests pin the
    read-side guard so a half-applied retire never surfaces stale
    chips on the manager roster.
    """

    def test_soft_deleted_work_role_excluded(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A retired :class:`WorkRole` row prunes its chip from the roster.

        Distinct from the existing ``test_soft_deleted_role_excluded``
        which retires the *assignment* (``UserWorkRole.deleted_at``);
        here the assignment stays live and the *catalogue row* itself
        is retired. Without the explicit ``WorkRole.deleted_at IS NULL``
        guard on the read side the chip would still surface a slug
        whose backing role no longer exists.
        """
        ctx, factory, ws_id = owner_ctx
        gone_role = _seed_work_role(factory, workspace_id=ws_id, key="phantom")
        _seed_user_work_role(
            factory,
            user_id=ctx.actor_id,
            workspace_id=ws_id,
            work_role_id=gone_role,
        )
        # Soft-delete the catalogue row, leaving the user_work_role
        # assignment live. The roster must still hide the chip.
        with factory() as s, tenant_agnostic():
            row = s.get(WorkRole, gone_role)
            assert row is not None
            row.deleted_at = _PINNED
            s.commit()
        client = _client(ctx, factory)
        body = client.get("/employees").json()
        row_dict = next(r for r in body if r["id"] == ctx.actor_id)
        assert "phantom" not in row_dict["roles"]

    def test_soft_deleted_property_excluded_from_workspace_grant(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Workspace-scoped grant fan-out drops retired properties.

        A workspace-scoped grant fans out across every property linked
        to the workspace. A retired property's id should NOT appear in
        the resulting set — the read-side guard joins
        :class:`Property` and filters ``deleted_at IS NULL``.
        """
        ctx, factory, ws_id = owner_ctx
        live = _seed_property(factory, workspace_id=ws_id, label="Live Villa")
        gone = _seed_property(factory, workspace_id=ws_id, label="Retired Villa")
        with factory() as s, tenant_agnostic():
            row = s.get(Property, gone)
            assert row is not None
            row.deleted_at = _PINNED
            s.commit()
        _seed_role_grant(
            factory,
            workspace_id=ws_id,
            user_id=ctx.actor_id,
            grant_role="worker",
            scope_property_id=None,
        )
        client = _client(ctx, factory)
        body = client.get("/employees").json()
        owner_row = next(r for r in body if r["id"] == ctx.actor_id)
        assert live in owner_row["properties"]
        assert gone not in owner_row["properties"]

    def test_soft_deleted_property_excluded_from_pinned_grant(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """A property-pinned grant on a retired property surfaces no row.

        The grant points at one property; if that property has been
        retired, the user's ``properties`` list collapses to ``[]``
        rather than carrying the dangling id.
        """
        ctx, factory, ws_id = owner_ctx
        gone = _seed_property(factory, workspace_id=ws_id, label="Retired Villa")
        with factory() as s, tenant_agnostic():
            row = s.get(Property, gone)
            assert row is not None
            row.deleted_at = _PINNED
            s.commit()
        with factory() as s:
            worker = bootstrap_user(
                s, email="pinned@example.com", display_name="Pinned Worker"
            )
            s.commit()
            worker_id = worker.id
        _seed_member(factory, user_id=worker_id, workspace_id=ws_id)
        _seed_role_grant(
            factory,
            workspace_id=ws_id,
            user_id=worker_id,
            grant_role="worker",
            scope_property_id=gone,
        )
        client = _client(ctx, factory)
        body = client.get("/employees").json()
        worker_row = next(r for r in body if r["id"] == worker_id)
        assert worker_row["properties"] == []


# ---------------------------------------------------------------------------
# GET /employees/{employee_id} — manager EmployeeDetailPage composite.
# ---------------------------------------------------------------------------


class TestDetail:
    """Pin the ``EmployeeDetail`` shape consumed by the SPA.

    The four ``subject_*`` lists are intentionally empty today — the
    per-list composites land in their own follow-up tasks. The shape
    contract still must hold so the SPA's overview tab renders
    instead of falling through to "Failed to load.".
    """

    def test_owner_returns_subject_with_empty_lists(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get(f"/employees/{ctx.actor_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Every key in the SPA's ``EmployeeDetail`` interface — keep
        # this in lockstep with
        # ``app/web/src/pages/manager/EmployeeDetailPage.tsx``.
        assert set(body.keys()) == {
            "subject",
            "subject_tasks",
            "subject_expenses",
            "subject_leaves",
            "subject_payslips",
        }
        assert body["subject"]["id"] == ctx.actor_id
        assert body["subject"]["email"] == "owner@example.com"
        assert body["subject_tasks"] == []
        assert body["subject_expenses"] == []
        assert body["subject_leaves"] == []
        assert body["subject_payslips"] == []

    def test_subject_matches_list_projection(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """``detail.subject`` is byte-identical to the list-endpoint row.

        Both surfaces project through :func:`_project_employee`; if a
        future refactor accidentally diverges the projections (a new
        field on one but not the other), this test catches it.
        """
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        list_body = client.get("/employees").json()
        list_row = next(r for r in list_body if r["id"] == ctx.actor_id)
        detail_body = client.get(f"/employees/{ctx.actor_id}").json()
        assert detail_body["subject"] == list_row

    def test_unknown_employee_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get("/employees/01HWNOTAREALEMPLOYEEID00")
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "not_found"

    def test_cross_workspace_employee_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """An employee in a sibling workspace is invisible — 404, not 403.

        Discriminating "not in workspace" from "doesn't exist" would
        leak whether a given user id is a real account; the
        membership-gated 404 keeps both paths indistinguishable.
        """
        ctx, factory, _ = owner_ctx
        with factory() as s:
            sibling_owner = bootstrap_user(
                s, email="sibling@example.com", display_name="Sibling"
            )
            bootstrap_workspace(
                s,
                slug="ws-detail-sibling",
                name="Sibling WS",
                owner_user_id=sibling_owner.id,
            )
            s.commit()
            sibling_id = sibling_owner.id
        client = _client(ctx, factory)
        resp = client.get(f"/employees/{sibling_id}")
        assert resp.status_code == 404

    def test_worker_returns_403(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Workers do not hold ``employees.read`` — must be 403.

        The composite gate mirrors the list endpoint: a worker hitting
        the manager profile page is a regression, not an accidental
        privacy leak. Reject before any subject lookup so the 403
        response carries no information about the target id.
        """
        ctx, factory, _, worker_id = worker_ctx
        client = _client(ctx, factory)
        resp = client.get(f"/employees/{worker_id}")
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "permission_denied"


# ---------------------------------------------------------------------------
# GET /employees/{employee_id}/settings — manager settings tab payload.
# ---------------------------------------------------------------------------


def _set_workspace_settings(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    settings_json: dict[str, object],
) -> None:
    """Patch :class:`Workspace.settings_json` for a seeded workspace."""
    from app.adapters.db.workspace.models import Workspace

    with factory() as s, tenant_agnostic():
        ws = s.get(Workspace, workspace_id)
        assert ws is not None
        ws.settings_json = settings_json
        s.commit()


def _seed_engagement_with_overrides(
    factory: sessionmaker[Session],
    *,
    user_id: str,
    workspace_id: str,
    settings_override_json: dict[str, object],
) -> str:
    """Seed an active engagement carrying ``settings_override_json``."""
    eid = _seed_engagement(factory, user_id=user_id, workspace_id=workspace_id)
    with factory() as s:
        row = s.get(WorkEngagement, eid)
        assert row is not None
        row.settings_override_json = settings_override_json
        s.commit()
    return eid


class TestSettings:
    """Pin the ``EntitySettingsPayload`` cascade for the employee tab."""

    def test_owner_returns_catalog_default_when_no_overrides(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Empty workspace + no engagement → every key resolves to catalog."""
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get(f"/employees/{ctx.actor_id}/settings")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body.keys()) == {"overrides", "resolved"}
        assert body["overrides"] == {}
        # Every catalog key resolves with ``source = "catalog"``.
        assert body["resolved"]["evidence.policy"] == {
            "value": "optional",
            "source": "catalog",
        }
        assert body["resolved"]["tasks.checklist_required"] == {
            "value": False,
            "source": "catalog",
        }
        # Sanity — the catalog has many keys; sample-of-three is enough
        # to pin the shape without binding to the full key list (which
        # is tested directly under tests/unit/api/v1/test_settings_api).
        assert all(
            set(entry.keys()) == {"value", "source"}
            for entry in body["resolved"].values()
        )

    def test_workspace_override_surfaces_with_workspace_source(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Workspace ``settings_json`` value beats catalog and tags ``workspace``."""
        ctx, factory, ws_id = owner_ctx
        _set_workspace_settings(
            factory,
            workspace_id=ws_id,
            settings_json={"evidence.policy": "require"},
        )
        client = _client(ctx, factory)
        body = client.get(f"/employees/{ctx.actor_id}/settings").json()
        assert body["resolved"]["evidence.policy"] == {
            "value": "require",
            "source": "workspace",
        }

    def test_engagement_override_wins_and_tags_employee_source(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Active engagement override beats workspace + catalog.

        ``overrides`` carries the engagement's bare override map (no
        cascade applied); ``resolved`` runs the full cascade with
        ``source = "employee"`` for keys that came from the engagement.
        """
        ctx, factory, ws_id = owner_ctx
        _set_workspace_settings(
            factory,
            workspace_id=ws_id,
            settings_json={"evidence.policy": "require"},
        )
        _seed_engagement_with_overrides(
            factory,
            user_id=ctx.actor_id,
            workspace_id=ws_id,
            settings_override_json={
                "evidence.policy": "forbid",
                "tasks.checklist_required": True,
            },
        )
        client = _client(ctx, factory)
        body = client.get(f"/employees/{ctx.actor_id}/settings").json()
        assert body["overrides"] == {
            "evidence.policy": "forbid",
            "tasks.checklist_required": True,
        }
        assert body["resolved"]["evidence.policy"] == {
            "value": "forbid",
            "source": "employee",
        }
        assert body["resolved"]["tasks.checklist_required"] == {
            "value": True,
            "source": "employee",
        }
        # A key not overridden at the engagement falls back to its
        # catalog default — provenance must reflect the actual layer.
        assert body["resolved"]["pay.frequency"]["source"] == "catalog"

    def test_inheritance_marker_falls_through(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """``"inherit"`` and ``None`` at the entity layer fall through.

        §02 cascade: the engagement carrying the literal string
        ``"inherit"`` (or a JSON ``null``) for a key behaves as if the
        key were absent. The next layer wins; provenance reports that
        layer, not ``employee``.
        """
        ctx, factory, ws_id = owner_ctx
        _set_workspace_settings(
            factory,
            workspace_id=ws_id,
            settings_json={"evidence.policy": "require"},
        )
        _seed_engagement_with_overrides(
            factory,
            user_id=ctx.actor_id,
            workspace_id=ws_id,
            settings_override_json={
                "evidence.policy": "inherit",
                "tasks.checklist_required": None,
            },
        )
        client = _client(ctx, factory)
        body = client.get(f"/employees/{ctx.actor_id}/settings").json()
        # Engagement-override map keeps the raw payload (inheritance
        # markers and all) so a manager UI can show "explicitly cleared".
        assert body["overrides"] == {
            "evidence.policy": "inherit",
            "tasks.checklist_required": None,
        }
        # Resolution skips the inheritance marker and falls through.
        assert body["resolved"]["evidence.policy"] == {
            "value": "require",
            "source": "workspace",
        }
        assert body["resolved"]["tasks.checklist_required"] == {
            "value": False,
            "source": "catalog",
        }

    def test_archived_engagement_does_not_supply_overrides(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        """Only the active engagement contributes overrides.

        An archived engagement (``archived_on IS NOT NULL``) carries
        historical data that must not bleed back into the live
        settings cascade.
        """
        ctx, factory, ws_id = owner_ctx
        # Archived engagement carrying overrides — must NOT surface.
        archived_id = _seed_engagement(
            factory,
            user_id=ctx.actor_id,
            workspace_id=ws_id,
            archived_on=date(2026, 1, 1),
        )
        with factory() as s:
            row = s.get(WorkEngagement, archived_id)
            assert row is not None
            row.settings_override_json = {"evidence.policy": "forbid"}
            s.commit()
        client = _client(ctx, factory)
        body = client.get(f"/employees/{ctx.actor_id}/settings").json()
        assert body["overrides"] == {}
        assert body["resolved"]["evidence.policy"]["source"] == "catalog"

    def test_unknown_employee_returns_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        resp = client.get("/employees/01HWNOTAREALEMPLOYEEID00/settings")
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "not_found"

    def test_worker_returns_403(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        """Workers do not hold ``scope.edit_settings`` — must be 403.

        The settings tab on the manager EmployeeDetailPage is by
        definition manager-facing; a worker hitting the route is a
        privacy regression. The gate matches ``/settings/catalog``.
        """
        ctx, factory, _, worker_id = worker_ctx
        client = _client(ctx, factory)
        resp = client.get(f"/employees/{worker_id}/settings")
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "permission_denied"
