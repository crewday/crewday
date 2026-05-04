"""Integration tests for ``GET /api/v1/auth/me``.

Exercises :func:`app.api.v1.auth.me.build_me_router` end-to-end against
a real engine (SQLite by default; Postgres when
``CREWDAY_TEST_DB=postgres``). The endpoint is the SPA's identity-
bootstrap probe — a regression silently bounces every authenticated
user to ``/login`` — so we pin the wire shape against the SPA's
:class:`AuthMe` contract (``app/web/src/auth/types.ts``) plus every
401 branch.

Coverage:

* **200 happy path.** Valid session cookie → response carries
  ``user_id`` / ``display_name`` / ``email`` /
  ``available_workspaces`` / ``current_workspace_id`` /
  ``is_deployment_admin`` matching the seeded user, and the keys
  + JSON types match the TS contract.
* **Workspace summary defaults.** The ``WorkspaceSummary`` ``timezone``
  / ``default_currency`` / ``default_country`` / ``default_locale``
  fields round-trip the persisted workspace defaults consumed by the
  SPA bootstrap.
* **Deployment-admin flag.** A user with a deployment-scope role
  grant flips ``is_deployment_admin`` to ``True``.
* **401 absent cookie.** ``error == 'session_required'``.
* **401 expired cookie.** A session row past its ``expires_at`` →
  ``error == 'session_invalid'``.
* **401 invalidated cookie.** A session row stamped with
  ``invalidated_at`` (revoked) → ``error == 'session_invalid'``.

See ``docs/specs/03-auth-and-tokens.md`` §"Sessions",
``docs/specs/12-rest-api.md`` §"Auth", and
``docs/specs/14-web-frontend.md`` §"Workspace selector".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, delete, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.assets.models import AssetType
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import db_session as db_session_dep
from app.api.v1.auth import me as me_module
from app.auth import session as auth_session
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.integration.auth._cleanup import delete_api_tokens_for_scope

pytestmark = pytest.mark.integration


# Pinned UA + Accept-Language. The router's :func:`auth_session.validate`
# call hashes both — a mismatch with the seed ``issue`` pair trips the
# §15 fingerprint gate and the 200 path never lands.
_TEST_UA: str = "pytest-auth-me"
_TEST_ACCEPT_LANGUAGE: str = "en"
_SEED_EMAILS: tuple[str, ...] = (
    "happy@example.com",
    "defaults@example.com",
    "country-missing@example.com",
    "admin@example.com",
    "expired@example.com",
    "revoked@example.com",
)
_SEED_SLUGS: tuple[str, ...] = (
    "ws-happy",
    "ws-defaults",
    "ws-country-missing",
    "ws-admin",
    "ws-expired",
    "ws-revoked",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-auth-me-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def client(
    engine: Engine,
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """FastAPI :class:`TestClient` mounted on the auth/me router.

    Each HTTP request opens its own Session against ``engine``, commits
    on clean exit, rolls back on exception — mirroring the production
    UoW so the seed step's writes are visible to the router's reads.

    ``app.auth.session.get_settings`` is patched so the session-hash
    pepper is deterministic across the ``issue`` seed-step and the
    ``validate`` check the router runs.
    """
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    # Pre-sweep this module's deterministic seeds so failed prior runs
    # cannot trip unique constraints or audit-count assertions.
    with session_factory() as s:
        seeded_user_ids = tuple(
            s.scalars(select(User.id).where(User.email_lower.in_(_SEED_EMAILS)))
        )
        seeded_ws_ids = tuple(
            s.scalars(select(Workspace.id).where(Workspace.slug.in_(_SEED_SLUGS)))
        )
        s.execute(
            delete(AuditLog).where(
                or_(
                    AuditLog.workspace_id.in_(seeded_ws_ids),
                    AuditLog.actor_id.in_(seeded_user_ids),
                    AuditLog.entity_id.in_((*seeded_user_ids, *seeded_ws_ids)),
                )
            )
        )
        delete_api_tokens_for_scope(s, user_ids=seeded_user_ids)
        s.execute(delete(User).where(User.email_lower.in_(_SEED_EMAILS)))
        s.commit()

    app = FastAPI()
    app.include_router(me_module.build_me_router(), prefix="/api/v1")

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

    # Sweep committed rows so sibling integration tests see clean tables.
    # Keep the predicates tied to this module's deterministic seed emails
    # and slugs; the integration engine is shared across tests.
    with engine.connect() as conn:
        seeded_user_ids = list(
            conn.execute(select(User.id).where(User.email_lower.in_(_SEED_EMAILS)))
            .scalars()
            .all()
        )
        seeded_ws_ids = list(
            conn.execute(select(Workspace.id).where(Workspace.slug.in_(_SEED_SLUGS)))
            .scalars()
            .all()
        )
        conn.execute(delete(SessionRow).where(SessionRow.user_id.in_(seeded_user_ids)))
        delete_api_tokens_for_scope(
            conn,
            workspace_ids=seeded_ws_ids,
            user_ids=seeded_user_ids,
        )
        conn.execute(
            delete(PermissionGroupMember).where(
                or_(
                    PermissionGroupMember.workspace_id.in_(seeded_ws_ids),
                    PermissionGroupMember.user_id.in_(seeded_user_ids),
                )
            )
        )
        conn.execute(
            delete(RoleGrant).where(
                or_(
                    RoleGrant.workspace_id.in_(seeded_ws_ids),
                    RoleGrant.user_id.in_(seeded_user_ids),
                )
            )
        )
        conn.execute(
            delete(PermissionGroup).where(
                PermissionGroup.workspace_id.in_(seeded_ws_ids)
            )
        )
        conn.execute(
            delete(UserWorkspace).where(
                or_(
                    UserWorkspace.workspace_id.in_(seeded_ws_ids),
                    UserWorkspace.user_id.in_(seeded_user_ids),
                )
            )
        )
        conn.execute(delete(AssetType).where(AssetType.workspace_id.in_(seeded_ws_ids)))
        conn.execute(
            delete(AuditLog).where(
                or_(
                    AuditLog.workspace_id.in_(seeded_ws_ids),
                    AuditLog.actor_id.in_(seeded_user_ids),
                    AuditLog.entity_id.in_((*seeded_user_ids, *seeded_ws_ids)),
                )
            )
        )
        conn.execute(delete(Workspace).where(Workspace.id.in_(seeded_ws_ids)))
        conn.execute(delete(User).where(User.id.in_(seeded_user_ids)))
        conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _issue_cookie(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    settings: Settings,
) -> str:
    """Issue a live session for ``user_id`` and return the raw cookie value."""
    with session_factory() as s:
        result = issue(
            s,
            user_id=user_id,
            has_owner_grant=True,
            ua=_TEST_UA,
            ip="127.0.0.1",
            accept_language=_TEST_ACCEPT_LANGUAGE,
            settings=settings,
        )
        s.commit()
        return result.cookie_value


def _seed_owner_workspace(
    session_factory: sessionmaker[Session],
    *,
    email: str,
    display_name: str,
    slug: str,
    name: str,
    default_timezone: str = "UTC",
    default_currency: str = "USD",
    default_country: str = "XX",
    default_locale: str = "en",
) -> tuple[str, str]:
    """Seed a workspace + owner user; return ``(user_id, workspace_slug)``."""
    with session_factory() as s:
        user = bootstrap_user(s, email=email, display_name=display_name)
        ws = bootstrap_workspace(
            s,
            slug=slug,
            name=name,
            owner_user_id=user.id,
        )
        ws.default_timezone = default_timezone
        ws.default_currency = default_currency
        ws.default_locale = default_locale
        ws.settings_json = {"workspace.default_country": default_country}
        s.commit()
        return user.id, ws.slug


# ---------------------------------------------------------------------------
# 200 happy path + schema assertion
# ---------------------------------------------------------------------------


# Schema contract — the keys + JSON types the SPA's ``AuthMe``
# interface in ``app/web/src/auth/types.ts`` consumes. Pinned here so
# any wire-shape drift (added or removed key, type change) breaks this
# test before it bounces every authenticated user back to ``/login``.
_AUTHME_SHAPE: dict[str, type | tuple[type, ...]] = {
    "user_id": str,
    "display_name": str,
    "email": str,
    "available_workspaces": list,
    # current_workspace_id is ``str | null`` in the TS contract.
    "current_workspace_id": (str, type(None)),
    "is_deployment_admin": bool,
}

# AvailableWorkspace inner shape (TS ``AvailableWorkspace`` /
# server :class:`AvailableWorkspaceResponse`).
_AVAILABLE_WORKSPACE_SHAPE: dict[str, type | tuple[type, ...]] = {
    "workspace": dict,
    # grant_role / binding_org_id are nullable per TS.
    "grant_role": (str, type(None)),
    "binding_org_id": (str, type(None)),
    "source": str,
}

# WorkspaceSummary inner shape (TS ``Workspace`` projection).
_WORKSPACE_SUMMARY_SHAPE: dict[str, type | tuple[type, ...]] = {
    "id": str,
    "name": str,
    "timezone": str,
    "default_currency": str,
    "default_country": str,
    "default_locale": str,
}


def _assert_shape(
    payload: dict[str, object],
    *,
    schema: dict[str, type | tuple[type, ...]],
    label: str,
) -> None:
    """Assert ``payload`` carries exactly the schema's keys with matching JSON types.

    Strict on both directions — a missing key fails the test, AND an
    unexpected key fails too. The TS contract enumerates exactly the
    fields the SPA consumes; a new server field is a drift the
    frontend hasn't ratified yet, so the test should catch it before
    the contract goes out of sync.
    """
    missing = sorted(set(schema) - set(payload))
    assert not missing, f"{label}: missing keys {missing}"
    extra = sorted(set(payload) - set(schema))
    assert not extra, (
        f"{label}: unexpected keys {extra} — server added a field the TS "
        "AuthMe contract has not been updated for yet"
    )
    for key, expected_type in schema.items():
        value = payload[key]
        assert isinstance(value, expected_type), (
            f"{label}: {key!r} type mismatch — "
            f"expected {expected_type}, got {type(value).__name__}"
        )


class TestAuthMeHappyPath:
    """Valid cookie → 200 + full schema match against the TS contract."""

    def test_returns_user_workspaces_and_admin_flag(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id, slug = _seed_owner_workspace(
            session_factory,
            email="happy@example.com",
            display_name="Happy User",
            slug="ws-happy",
            name="Happy",
        )
        cookie = _issue_cookie(session_factory, user_id=user_id, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        r = client.get("/api/v1/auth/me")

        assert r.status_code == 200, r.text
        body = r.json()
        # Top-level shape — every TS ``AuthMe`` key + JSON type.
        _assert_shape(body, schema=_AUTHME_SHAPE, label="AuthMe")
        # Spec-pinned values.
        assert body["user_id"] == user_id
        assert body["display_name"] == "Happy User"
        assert body["email"] == "happy@example.com"
        # current_workspace_id is always None on the bare-host
        # ``/auth/me`` envelope today (the SPA picks the workspace
        # client-side from ``available_workspaces``).
        assert body["current_workspace_id"] is None
        # No deployment-scope grant → flag is False.
        assert body["is_deployment_admin"] is False

        # available_workspaces shape — exactly one entry for the seeded
        # owner workspace; check both layers of the nested envelope.
        assert isinstance(body["available_workspaces"], list)
        assert len(body["available_workspaces"]) == 1
        entry = body["available_workspaces"][0]
        _assert_shape(
            entry,
            schema=_AVAILABLE_WORKSPACE_SHAPE,
            label="AvailableWorkspace",
        )
        # Owners-group bootstrap collapses to ``manager`` per §03.
        assert entry["grant_role"] == "manager"
        assert entry["binding_org_id"] is None
        assert entry["source"] == "workspace_grant"

        ws = entry["workspace"]
        _assert_shape(ws, schema=_WORKSPACE_SUMMARY_SHAPE, label="WorkspaceSummary")
        # ``id`` is the URL slug (so the SPA's ``slugFor`` can build
        # ``/w/{id}/...`` links without a follow-up shape migration).
        assert ws["id"] == slug
        assert ws["name"] == "Happy"

    def test_workspace_summary_carries_default_columns(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """Workspace locale defaults round-trip from persisted workspace values."""
        user_id, _slug = _seed_owner_workspace(
            session_factory,
            email="defaults@example.com",
            display_name="Defaults",
            slug="ws-defaults",
            name="Defaults",
            default_timezone="Asia/Tokyo",
            default_currency="JPY",
            default_country="JP",
            default_locale="ja-JP",
        )
        cookie = _issue_cookie(session_factory, user_id=user_id, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        r = client.get("/api/v1/auth/me")
        assert r.status_code == 200, r.text
        ws = r.json()["available_workspaces"][0]["workspace"]
        assert ws["timezone"] == "Asia/Tokyo"
        assert ws["default_currency"] == "JPY"
        assert ws["default_country"] == "JP"
        assert ws["default_locale"] == "ja-JP"

    def test_workspace_summary_country_falls_back_when_setting_absent(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """Workspace country follows the settings-json fallback used by settings."""
        user_id, slug = _seed_owner_workspace(
            session_factory,
            email="country-missing@example.com",
            display_name="Country Missing",
            slug="ws-country-missing",
            name="Country Missing",
            default_timezone="Europe/Paris",
            default_currency="EUR",
            default_country="FR",
            default_locale="fr",
        )
        with session_factory() as s:
            ws = s.scalar(select(Workspace).where(Workspace.slug == slug))
            assert ws is not None
            ws.settings_json = {}
            s.commit()

        cookie = _issue_cookie(session_factory, user_id=user_id, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        r = client.get("/api/v1/auth/me")
        assert r.status_code == 200, r.text
        ws = r.json()["available_workspaces"][0]["workspace"]
        assert ws["timezone"] == "Europe/Paris"
        assert ws["default_currency"] == "EUR"
        assert ws["default_country"] == "XX"
        assert ws["default_locale"] == "fr"


class TestAuthMeDeploymentAdmin:
    """``is_deployment_admin`` reflects an active deployment grant."""

    def test_deployment_grant_flips_admin_flag(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id, _slug = _seed_owner_workspace(
            session_factory,
            email="admin@example.com",
            display_name="Admin",
            slug="ws-admin",
            name="Admin",
        )
        # Add a deployment-scope grant — :func:`is_deployment_admin`
        # only checks for *any* row with ``scope_kind='deployment'``.
        with session_factory() as s, tenant_agnostic():
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=None,
                    user_id=user_id,
                    grant_role="manager",
                    scope_kind="deployment",
                    scope_property_id=None,
                    created_at=datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC),
                    created_by_user_id=None,
                )
            )
            s.commit()

        cookie = _issue_cookie(session_factory, user_id=user_id, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        r = client.get("/api/v1/auth/me")
        assert r.status_code == 200, r.text
        assert r.json()["is_deployment_admin"] is True


# ---------------------------------------------------------------------------
# 401 branches
# ---------------------------------------------------------------------------


class TestAuthMeUnauthorized:
    """Every failure mode of :func:`auth_session.validate` collapses to 401."""

    def test_no_cookie_returns_401_session_required(self, client: TestClient) -> None:
        r = client.get("/api/v1/auth/me")
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["error"] == "session_required"

    def test_unknown_cookie_returns_401_session_invalid(
        self, client: TestClient
    ) -> None:
        client.cookies.set(SESSION_COOKIE_NAME, "not-a-real-session-token")
        r = client.get("/api/v1/auth/me")
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["error"] == "session_invalid"

    def test_expired_cookie_returns_401_session_invalid(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """A session row whose ``expires_at`` is in the past → 401.

        ``auth_session.validate`` raises :class:`SessionExpired`; the
        router maps both :class:`SessionInvalid` and
        :class:`SessionExpired` onto the same envelope so the caller
        cannot distinguish "never existed" from "expired" — the same
        enumeration-proofness the domain layer enforces.
        """
        user_id, _slug = _seed_owner_workspace(
            session_factory,
            email="expired@example.com",
            display_name="Expired",
            slug="ws-expired",
            name="Expired",
        )
        cookie = _issue_cookie(session_factory, user_id=user_id, settings=settings)
        # Push the row's ``expires_at`` into the past.
        session_id = auth_session.hash_cookie_value(cookie)
        with session_factory() as s, tenant_agnostic():
            row = s.get(SessionRow, session_id)
            assert row is not None
            row.expires_at = datetime.now(UTC) - timedelta(days=1)
            s.commit()

        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        r = client.get("/api/v1/auth/me")
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["error"] == "session_invalid"

    def test_invalidated_cookie_returns_401_session_invalid(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """A revoked / invalidated session row → 401.

        Mirrors the logout post-condition: the row carries
        ``invalidated_at`` + ``invalidation_cause`` and
        :func:`auth_session.validate` refuses it.
        """
        user_id, _slug = _seed_owner_workspace(
            session_factory,
            email="revoked@example.com",
            display_name="Revoked",
            slug="ws-revoked",
            name="Revoked",
        )
        cookie = _issue_cookie(session_factory, user_id=user_id, settings=settings)
        # Invalidate the freshly-issued session via the domain helper —
        # same code path the logout router takes.
        with session_factory() as s:
            count = auth_session.invalidate_for_user(s, user_id=user_id, cause="logout")
            s.commit()
        assert count == 1

        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        r = client.get("/api/v1/auth/me")
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["error"] == "session_invalid"
