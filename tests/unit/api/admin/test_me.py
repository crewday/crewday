"""Unit tests for :mod:`app.api.admin.me` (cd-yj4k).

Covers the response-shape contract of ``GET /admin/api/v1/me`` and
``GET /admin/api/v1/me/admins`` against an in-memory SQLite engine
with :class:`Base.metadata` schema. The cd-xgmu dep tests in
:mod:`tests.unit.api.admin.test_deps` exhaustively cover the auth
gate; this module focuses on:

* ``/me``:
    - returns the SPA-shaped :class:`AdminMeResponse` for a session
      admin (full capability map);
    - returns the same shape via a deployment-scoped token (subset
      capability map);
    - 404s for callers without an active deployment grant.
* ``/me/admins``:
    - lists every active deployment grant joined to the user row;
    - 404s for non-admin callers;
    - excludes workspace-only grants from the listing (the
      ``scope_kind='deployment'`` predicate is load-bearing).

See ``docs/specs/12-rest-api.md`` §"Admin surface" and
``docs/specs/14-web-frontend.md`` §"Admin shell".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

# Eagerly load every ORM model the production factory imports so
# ``Base.metadata.create_all`` below stays consistent regardless of
# pytest collection order. Mirrors the import block in the cd-xgmu
# dep tests — without these imports a sibling integration test that
# runs first can leave Base.metadata holding rows whose FKs target
# tables we'd otherwise skip.
import app.adapters.db.payroll.models
import app.adapters.db.places.models  # noqa: F401
from app.adapters.db.authz.models import DeploymentOwner, RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.api.admin import admin_router
from app.api.deps import db_session as db_session_dep
from app.api.errors import add_exception_handlers
from app.auth import tokens as auth_tokens
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.tenancy import (
    DEPLOYMENT_SCOPE_CATALOG,
    WorkspaceContext,
    tenant_agnostic,
)
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_TEST_UA = "pytest-admin-me"
_TEST_ACCEPT_LANGUAGE = "en"


# ---------------------------------------------------------------------------
# Fixtures (mirrored from ``test_deps.py`` so each module owns its own
# in-memory engine, fixture lifetimes don't bleed across modules under
# pytest-xdist, and the imports here stay explicit about what each
# fixture covers).
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-admin-me-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """:class:`TestClient` mounting the admin router under ``/admin/api/v1``.

    Patches :func:`app.auth.session.get_settings` so the session
    pepper matches between the seed :func:`issue` and the dep's
    :func:`validate`. The dep override on
    :func:`app.api.deps.db_session` plumbs every request through a
    rolled-back ``Session`` bound to ``engine``.
    """
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    app = FastAPI()
    app.include_router(admin_router, prefix="/admin/api/v1")
    add_exception_handlers(app)

    def _session() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[db_session_dep] = _session

    with TestClient(
        app,
        base_url="https://testserver",
        headers={
            "User-Agent": _TEST_UA,
            "Accept-Language": _TEST_ACCEPT_LANGUAGE,
        },
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_user(
    session: Session,
    *,
    email: str,
    display_name: str,
) -> str:
    """Insert a :class:`User` row; return its id."""
    user = bootstrap_user(session, email=email, display_name=display_name)
    return user.id


def _seed_workspace(session: Session, *, slug: str) -> str:
    """Insert a :class:`Workspace` row; return its id."""
    workspace_id = new_ulid()
    with tenant_agnostic():
        session.add(
            Workspace(
                id=workspace_id,
                slug=slug,
                name=slug.title(),
                plan="free",
                quota_json={},
                created_at=_PINNED,
            )
        )
        session.flush()
    return workspace_id


def _grant_deployment_admin(
    session: Session,
    *,
    user_id: str,
    created_at: datetime | None = None,
    created_by_user_id: str | None = None,
    grant_role: str = "manager",
) -> str:
    """Plant a deployment-scope grant; return the grant id."""
    grant_id = new_ulid()
    with tenant_agnostic():
        session.add(
            RoleGrant(
                id=grant_id,
                workspace_id=None,
                user_id=user_id,
                grant_role=grant_role,
                scope_kind="deployment",
                created_at=created_at or _PINNED,
                created_by_user_id=created_by_user_id,
            )
        )
        session.flush()
    return grant_id


def _grant_deployment_owner(
    session: Session,
    *,
    user_id: str,
    added_by_user_id: str | None = None,
) -> None:
    """Plant an ``owners@deployment`` membership row."""
    with tenant_agnostic():
        session.add(
            DeploymentOwner(
                user_id=user_id,
                added_at=_PINNED,
                added_by_user_id=added_by_user_id,
            )
        )
        session.flush()


def _grant_workspace_role(
    session: Session,
    *,
    user_id: str,
    workspace_id: str,
    grant_role: str = "manager",
) -> str:
    """Plant a workspace-scope grant — must NOT show up on the admin list."""
    grant_id = new_ulid()
    with tenant_agnostic():
        session.add(
            RoleGrant(
                id=grant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                grant_role=grant_role,
                scope_kind="workspace",
                created_at=_PINNED,
            )
        )
        session.flush()
    return grant_id


def _issue_session(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    settings: Settings,
) -> str:
    with session_factory() as s:
        result = issue(
            s,
            user_id=user_id,
            has_owner_grant=False,
            ua=_TEST_UA,
            ip="127.0.0.1",
            accept_language=_TEST_ACCEPT_LANGUAGE,
            settings=settings,
        )
        s.commit()
        return result.cookie_value


def _mint_scoped_token(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    workspace_id: str,
    workspace_slug: str,
    scopes: dict[str, object],
) -> str:
    with session_factory() as s:
        ctx = WorkspaceContext(
            workspace_id=workspace_id,
            workspace_slug=workspace_slug,
            actor_id=user_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id=new_ulid(),
        )
        result = auth_tokens.mint(
            s,
            ctx,
            user_id=user_id,
            label="test scoped token",
            scopes=scopes,
            expires_at=None,
            kind="scoped",
            now=_PINNED,
        )
        s.commit()
        return result.token


# ---------------------------------------------------------------------------
# /admin/api/v1/me
# ---------------------------------------------------------------------------


class TestAdminMe:
    """``GET /admin/api/v1/me`` response shape + auth gating."""

    def test_me_returns_admin_payload(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """A session admin gets the SPA-shaped envelope + full catalogue."""
        with session_factory() as s:
            user_id = _seed_user(
                s, email="ada@example.com", display_name="Ada Lovelace"
            )
            _grant_deployment_admin(s, user_id=user_id)
            _grant_deployment_owner(s, user_id=user_id)
            s.commit()

        cookie_value = _issue_session(
            session_factory, user_id=user_id, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        resp = client.get("/admin/api/v1/me")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # Shape: every SPA-required field is present and typed.
        assert body["user_id"] == user_id
        assert body["display_name"] == "Ada Lovelace"
        assert body["email"] == "ada@example.com"
        assert body["is_owner"] is True

        # Capabilities reflect the dep's full scope catalogue for a
        # session principal.
        assert isinstance(body["capabilities"], dict)
        assert set(body["capabilities"].keys()) == DEPLOYMENT_SCOPE_CATALOG
        assert all(value is True for value in body["capabilities"].values())
        # Spec §12 "Admin surface": ``capabilities`` keys land in
        # sorted order on the wire so an ETag-style cache (or a
        # snapshot diff) does not flap on Python dict insertion
        # order. ``json.dumps`` preserves Python dict insertion
        # order, so this asserts what the SPA actually sees.
        assert list(body["capabilities"].keys()) == sorted(DEPLOYMENT_SCOPE_CATALOG)

    def test_me_returns_admin_payload_via_token(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A deployment-scoped token narrows capabilities to its row's keys."""
        with session_factory() as s:
            user_id = _seed_user(
                s, email="agent@example.com", display_name="Agent Smith"
            )
            workspace_id = _seed_workspace(s, slug="token-ws")
            s.commit()

        token = _mint_scoped_token(
            session_factory,
            user_id=user_id,
            workspace_id=workspace_id,
            workspace_slug="token-ws",
            scopes={"deployment.audit:read": True},
        )

        resp = client.get(
            "/admin/api/v1/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # Token principal — user fields point at the minter (the
        # delegating user for delegated tokens). Capabilities are
        # narrowed to the row's scope set; absent keys mean "not
        # granted".
        assert body["user_id"] == user_id
        assert body["display_name"] == "Agent Smith"
        assert body["email"] == "agent@example.com"
        assert body["is_owner"] is False
        assert body["capabilities"] == {"deployment.audit:read": True}

    def test_me_404_for_non_admin(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """A session for a user without a deployment grant 404s."""
        with session_factory() as s:
            user_id = _seed_user(
                s, email="plain@example.com", display_name="Plain User"
            )
            s.commit()

        cookie_value = _issue_session(
            session_factory, user_id=user_id, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        resp = client.get("/admin/api/v1/me")
        assert resp.status_code == 404, resp.text
        body = resp.json()
        # RFC 7807 problem+json envelope with the canonical
        # ``not_found`` error code lifted into the extra fields.
        assert body["status"] == 404
        assert body["type"].endswith("/not_found")
        assert body.get("error") == "not_found"


# ---------------------------------------------------------------------------
# /admin/api/v1/me/admins
# ---------------------------------------------------------------------------


class TestAdminMeAdmins:
    """``GET /admin/api/v1/me/admins`` listing + filtering invariants."""

    def test_me_admins_lists_all_deployment_grants(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """Every active deployment grant surfaces in the listing, oldest-first."""
        with session_factory() as s:
            caller_id = _seed_user(
                s, email="ada@example.com", display_name="Ada Lovelace"
            )
            grace_id = _seed_user(
                s, email="grace@example.com", display_name="Grace Hopper"
            )

            # Grant the caller first (older) so we can assert
            # ordering. The "granted_by" sentinel is the bootstrap
            # null — surfaces as ``"system"``.
            caller_grant_id = _grant_deployment_admin(
                s,
                user_id=caller_id,
                created_at=_PINNED,
            )
            _grant_deployment_owner(s, user_id=caller_id)
            # Grace was promoted later, by the caller.
            grace_grant_id = _grant_deployment_admin(
                s,
                user_id=grace_id,
                created_at=_PINNED + timedelta(hours=1),
                created_by_user_id=caller_id,
            )
            s.commit()

        cookie_value = _issue_session(
            session_factory, user_id=caller_id, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        resp = client.get("/admin/api/v1/me/admins")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        admins = body["admins"]
        assert len(admins) == 2

        # Oldest-first ordering by ``created_at``.
        first, second = admins
        assert first["id"] == caller_grant_id
        assert first["user_id"] == caller_id
        assert first["display_name"] == "Ada Lovelace"
        assert first["email"] == "ada@example.com"
        assert first["is_owner"] is True
        # Bootstrap null → ``"system"`` sentinel.
        assert first["granted_by"] == "system"
        # ISO-8601 with explicit UTC offset.
        assert first["granted_at"].endswith("+00:00")

        assert second["id"] == grace_grant_id
        assert second["user_id"] == grace_id
        assert second["display_name"] == "Grace Hopper"
        assert second["email"] == "grace@example.com"
        assert second["is_owner"] is False
        assert second["granted_by"] == caller_id

        # Groups not yet seeded — empty list keeps the contract
        # forward-compatible without a shape break.
        assert body["groups"] == []

    def test_me_admins_404_for_non_admin(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """A non-admin caller cannot enumerate the deployment-admin team."""
        with session_factory() as s:
            user_id = _seed_user(
                s, email="plain@example.com", display_name="Plain User"
            )
            s.commit()

        cookie_value = _issue_session(
            session_factory, user_id=user_id, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        resp = client.get("/admin/api/v1/me/admins")
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["status"] == 404
        assert body["type"].endswith("/not_found")
        assert body.get("error") == "not_found"

    def test_me_admins_excludes_workspace_only_users(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """A workspace-scope grant for a stranger MUST NOT appear on the list."""
        with session_factory() as s:
            caller_id = _seed_user(
                s, email="ada@example.com", display_name="Ada Lovelace"
            )
            stranger_id = _seed_user(
                s,
                email="stranger@example.com",
                display_name="Workspace Manager",
            )
            workspace_id = _seed_workspace(s, slug="other-ws")

            _grant_deployment_admin(s, user_id=caller_id)
            # The stranger holds a workspace-scope grant only — the
            # ``scope_kind='deployment'`` predicate must filter them
            # out of the admin team listing.
            _grant_workspace_role(
                s,
                user_id=stranger_id,
                workspace_id=workspace_id,
                grant_role="manager",
            )
            s.commit()

        cookie_value = _issue_session(
            session_factory, user_id=caller_id, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        resp = client.get("/admin/api/v1/me/admins")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        admins = body["admins"]
        assert len(admins) == 1
        assert admins[0]["user_id"] == caller_id
        # Defence in depth: the stranger's id is nowhere on the wire.
        wire_user_ids = {row["user_id"] for row in admins}
        assert stranger_id not in wire_user_ids
