"""Integration — resource-aware ``scope.view`` / ``scope.edit_settings`` gates.

cd-821v1 follow-up to cd-7t1f1. The scoped-API-token gate
(:func:`app.authz.enforce.require`) maps a route's §05 action to the §03
scope a scoped token must hold. A handful of routes are gated by the
**resource-agnostic** actions ``scope.view`` / ``scope.edit_settings``,
which name no resource — so those routes now declare the resource scope
explicitly at their :func:`app.authz.dep.Permission` gate
(``required_scope=...``). Without that, a token holding a *documented*
scope was wrongly 403'd on its own documented route.

This suite mints the ctx of a scoped token directly (overriding
``current_workspace_context`` like
``tests.integration.test_asset_actions_api``) and drives the **real**
router factories so the assertions ride the production ``Permission``
wiring:

* A token holding the route's documented scope reaches the handler
  (200 / expected), where before it 403'd ``insufficient_scope``.
* A token holding the wrong scope still 403s ``insufficient_scope`` with
  the ``WWW-Authenticate`` challenge naming the scope it lacks — the
  hole cd-7t1f1 closed stays closed.
* A route whose resource has no §03 scope (assets — ``scope.view`` with
  no override) stays deny-by-default for every scoped token.
* Non-scoped principals (sessions) never touch the gate.

The seeded actor holds a ``manager`` grant so the role walk always
*allows*; the scope gate is therefore the sole decider, which is exactly
the behaviour under test.

See ``docs/specs/03-auth-and-tokens.md`` §"Scopes" and
``app/authz/scopes.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.adapters.db.places.models import Property, PropertyWorkspace
from app.api.assets import build_assets_router
from app.api.deps import current_workspace_context, db_session
from app.api.errors import add_exception_handlers
from app.api.v1.employees import build_employees_router
from app.api.v1.inventory import build_inventory_router
from app.api.v1.places import build_properties_router
from app.api.v1.role_grants import build_users_role_grants_router
from app.tenancy.context import WorkspaceContext
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
_SLUG = "scoped-generic-gates"
_PROPERTY_ID = "prop_scoped_generic_gates"


def _scoped_ctx(
    *,
    workspace_id: str,
    actor_id: str,
    scopes: frozenset[str],
) -> WorkspaceContext:
    """A scoped-API-token ctx — arms the §03 scope gate."""
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=_SLUG,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id="corr_scoped_generic_gates",
        principal_kind="token",
        token_kind="scoped",
        token_scopes=scopes,
    )


def _session_ctx(*, workspace_id: str, actor_id: str) -> WorkspaceContext:
    """A passkey-session ctx — ``principal_kind='session'`` skips the gate."""
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=_SLUG,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id="corr_scoped_generic_gates_session",
    )


def _client(session: Session, ctx: WorkspaceContext) -> TestClient:
    """Mount the real gated routers behind a fixed ctx + session."""
    app = FastAPI()
    app.include_router(build_properties_router())
    app.include_router(build_inventory_router())
    app.include_router(build_users_role_grants_router())
    app.include_router(build_employees_router())
    app.include_router(build_assets_router(), prefix="/assets")
    add_exception_handlers(app)

    def override_ctx() -> WorkspaceContext:
        return ctx

    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[current_workspace_context] = override_ctx
    app.dependency_overrides[db_session] = override_db
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def seeded(db_session: Session) -> tuple[str, str]:
    """Workspace + one property + a ``manager`` grant for the actor.

    Returns ``(workspace_id, actor_id)``. ``bootstrap_workspace`` already
    seeds the owner a ``manager`` grant + owners-group membership, so the
    role walk allows both ``scope.view`` and ``scope.edit_settings`` — every
    assertion below therefore isolates the scoped-token scope gate.
    """
    owner = bootstrap_user(
        db_session,
        email="scoped-generic-gates-owner@example.com",
        display_name="Owner",
    )
    workspace = bootstrap_workspace(
        db_session,
        slug=_SLUG,
        name="Scoped Generic Gates",
        owner_user_id=owner.id,
    )
    db_session.add(
        Property(
            id=_PROPERTY_ID,
            name="Villa",
            kind="residence",
            address="Villa Road",
            address_json={"line1": "Villa Road", "country": "US"},
            country="US",
            timezone="UTC",
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_NOW,
            updated_at=_NOW,
            deleted_at=None,
        )
    )
    db_session.add(
        PropertyWorkspace(
            property_id=_PROPERTY_ID,
            workspace_id=workspace.id,
            label="Villa",
            membership_role="owner_workspace",
            share_guest_identity=False,
            status="active",
            created_at=_NOW,
        )
    )
    db_session.flush()
    return workspace.id, owner.id


def _assert_insufficient_scope(response: object, *, scope: str | None) -> None:
    from httpx import Response

    assert isinstance(response, Response)
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"] == "insufficient_scope"
    challenge = response.headers["WWW-Authenticate"]
    assert 'error="insufficient_scope"' in challenge
    if scope is None:
        # Deny-by-default: no mapped scope → the challenge names none.
        assert "scope=" not in challenge
        assert "scope" not in body
    else:
        assert body["scope"] == scope
        assert f'scope="{scope}"' in challenge


# ---------------------------------------------------------------------------
# Documented scope reaches its documented route (the cd-821v1 repros).
# ---------------------------------------------------------------------------


class TestDocumentedScopeReachesRoute:
    def test_properties_read_reaches_property_detail(
        self, db_session: Session, seeded: tuple[str, str]
    ) -> None:
        ws_id, actor_id = seeded
        ctx = _scoped_ctx(
            workspace_id=ws_id,
            actor_id=actor_id,
            scopes=frozenset({"properties:read"}),
        )
        client = _client(db_session, ctx)
        r = client.get(f"/properties/{_PROPERTY_ID}")
        assert r.status_code == 200, r.text

    def test_inventory_read_reaches_item_list(
        self, db_session: Session, seeded: tuple[str, str]
    ) -> None:
        ws_id, actor_id = seeded
        ctx = _scoped_ctx(
            workspace_id=ws_id,
            actor_id=actor_id,
            scopes=frozenset({"inventory:read"}),
        )
        client = _client(db_session, ctx)
        r = client.get(f"/properties/{_PROPERTY_ID}/items")
        assert r.status_code == 200, r.text

    def test_inventory_write_reaches_item_create(
        self, db_session: Session, seeded: tuple[str, str]
    ) -> None:
        ws_id, actor_id = seeded
        ctx = _scoped_ctx(
            workspace_id=ws_id,
            actor_id=actor_id,
            scopes=frozenset({"inventory:write"}),
        )
        client = _client(db_session, ctx)
        r = client.post(
            f"/properties/{_PROPERTY_ID}/items",
            json={"name": "Towels", "unit": "each"},
        )
        assert r.status_code == 201, r.text

    def test_users_read_reaches_role_grants_list(
        self, db_session: Session, seeded: tuple[str, str]
    ) -> None:
        ws_id, actor_id = seeded
        ctx = _scoped_ctx(
            workspace_id=ws_id,
            actor_id=actor_id,
            scopes=frozenset({"users:read"}),
        )
        client = _client(db_session, ctx)
        r = client.get(f"/users/{actor_id}/role_grants")
        assert r.status_code == 200, r.text

    def test_properties_write_reaches_property_settings(
        self, db_session: Session, seeded: tuple[str, str]
    ) -> None:
        # places property-settings read is gated by a *direct* ``require``
        # with ``required_scope="properties:write"`` (not a Permission dep).
        ws_id, actor_id = seeded
        ctx = _scoped_ctx(
            workspace_id=ws_id,
            actor_id=actor_id,
            scopes=frozenset({"properties:write"}),
        )
        client = _client(db_session, ctx)
        r = client.get(f"/properties/{_PROPERTY_ID}/settings")
        assert r.status_code == 200, r.text

    def test_users_write_reaches_employee_settings(
        self, db_session: Session, seeded: tuple[str, str]
    ) -> None:
        # employees settings-read is gated by ``scope.edit_settings`` (the
        # manager tier), so its scope mirror is ``users:write`` — the same
        # engagement-surface reasoning as property settings.
        ws_id, actor_id = seeded
        ctx = _scoped_ctx(
            workspace_id=ws_id,
            actor_id=actor_id,
            scopes=frozenset({"users:write"}),
        )
        client = _client(db_session, ctx)
        r = client.get(f"/employees/{actor_id}/settings")
        assert r.status_code == 200, r.text

    def test_read_scope_implied_by_write(
        self, db_session: Session, seeded: tuple[str, str]
    ) -> None:
        # §03 "``*:read`` implied by ``*:write``": a properties:write
        # token satisfies the properties:read detail gate.
        ws_id, actor_id = seeded
        ctx = _scoped_ctx(
            workspace_id=ws_id,
            actor_id=actor_id,
            scopes=frozenset({"properties:write"}),
        )
        client = _client(db_session, ctx)
        r = client.get(f"/properties/{_PROPERTY_ID}")
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# No widening — wrong scope still 403s (the hole cd-7t1f1 closed).
# ---------------------------------------------------------------------------


class TestWrongScopeStillDenied:
    def test_tasks_read_denied_on_property_detail(
        self, db_session: Session, seeded: tuple[str, str]
    ) -> None:
        ws_id, actor_id = seeded
        ctx = _scoped_ctx(
            workspace_id=ws_id,
            actor_id=actor_id,
            scopes=frozenset({"tasks:read"}),
        )
        client = _client(db_session, ctx)
        r = client.get(f"/properties/{_PROPERTY_ID}")
        _assert_insufficient_scope(r, scope="properties:read")

    def test_tasks_read_denied_on_inventory_list(
        self, db_session: Session, seeded: tuple[str, str]
    ) -> None:
        ws_id, actor_id = seeded
        ctx = _scoped_ctx(
            workspace_id=ws_id,
            actor_id=actor_id,
            scopes=frozenset({"tasks:read"}),
        )
        client = _client(db_session, ctx)
        r = client.get(f"/properties/{_PROPERTY_ID}/items")
        _assert_insufficient_scope(r, scope="inventory:read")

    def test_tasks_read_denied_on_role_grants_list(
        self, db_session: Session, seeded: tuple[str, str]
    ) -> None:
        ws_id, actor_id = seeded
        ctx = _scoped_ctx(
            workspace_id=ws_id,
            actor_id=actor_id,
            scopes=frozenset({"tasks:read"}),
        )
        client = _client(db_session, ctx)
        r = client.get(f"/users/{actor_id}/role_grants")
        _assert_insufficient_scope(r, scope="users:read")

    def test_users_read_denied_on_employee_settings(
        self, db_session: Session, seeded: tuple[str, str]
    ) -> None:
        # ``users:read`` does NOT satisfy the ``users:write`` employee-
        # settings gate — the manager-tier surface needs the write scope.
        ws_id, actor_id = seeded
        ctx = _scoped_ctx(
            workspace_id=ws_id,
            actor_id=actor_id,
            scopes=frozenset({"users:read"}),
        )
        client = _client(db_session, ctx)
        r = client.get(f"/employees/{actor_id}/settings")
        _assert_insufficient_scope(r, scope="users:write")

    def test_inventory_read_denied_on_item_create(
        self, db_session: Session, seeded: tuple[str, str]
    ) -> None:
        # ``inventory:read`` does NOT satisfy the ``inventory:write``
        # settings gate — adjust/write verbs stand alone (no implication).
        ws_id, actor_id = seeded
        ctx = _scoped_ctx(
            workspace_id=ws_id,
            actor_id=actor_id,
            scopes=frozenset({"inventory:read"}),
        )
        client = _client(db_session, ctx)
        r = client.post(
            f"/properties/{_PROPERTY_ID}/items",
            json={"name": "Towels", "unit": "each"},
        )
        _assert_insufficient_scope(r, scope="inventory:write")


# ---------------------------------------------------------------------------
# Deny-by-default — unmapped resource families stay closed.
# ---------------------------------------------------------------------------


class TestUnmappedFamilyDenyByDefault:
    def test_assets_list_denied_even_with_broad_scopes(
        self, db_session: Session, seeded: tuple[str, str]
    ) -> None:
        # ``assets`` has no §03 scope, so the assets ``scope.view`` gate
        # names no ``required_scope`` — a scoped token holding a bag of
        # documented scopes still can't reach it.
        ws_id, actor_id = seeded
        ctx = _scoped_ctx(
            workspace_id=ws_id,
            actor_id=actor_id,
            scopes=frozenset(
                {"properties:write", "inventory:write", "users:write", "tasks:write"}
            ),
        )
        client = _client(db_session, ctx)
        r = client.get("/assets/")
        _assert_insufficient_scope(r, scope=None)


# ---------------------------------------------------------------------------
# Non-scoped principals never touch the gate.
# ---------------------------------------------------------------------------


class TestNonScopedPrincipalUnaffected:
    def test_session_principal_reaches_route_without_any_scope(
        self, db_session: Session, seeded: tuple[str, str]
    ) -> None:
        ws_id, actor_id = seeded
        ctx = _session_ctx(workspace_id=ws_id, actor_id=actor_id)
        client = _client(db_session, ctx)
        # No token scopes at all — a session actor is unaffected by the
        # scope gate; the manager role walk allows both routes.
        assert client.get(f"/properties/{_PROPERTY_ID}").status_code == 200
        assert client.get("/assets/").status_code == 200

    def test_delegated_token_unaffected_by_gate(
        self, db_session: Session, seeded: tuple[str, str]
    ) -> None:
        # A delegated token (``token_kind='delegated'``) carries no
        # scopes and resolves authority from the user's grants — the
        # scoped-token gate is a no-op for it.
        ws_id, actor_id = seeded
        ctx = WorkspaceContext(
            workspace_id=ws_id,
            workspace_slug=_SLUG,
            actor_id=actor_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id="corr_scoped_generic_gates_delegated",
            principal_kind="token",
            token_kind="delegated",
            token_scopes=frozenset(),
        )
        client = _client(db_session, ctx)
        assert client.get(f"/properties/{_PROPERTY_ID}").status_code == 200
        assert client.get("/assets/").status_code == 200
