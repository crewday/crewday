"""Integration test for ``/api/v1/me/tokens`` — personal access token surface.

Exercises the bare-host PAT router end-to-end against a real engine
(SQLite by default; Postgres when ``CREWDAY_TEST_DB=postgres``),
driving the FastAPI router with a live passkey session.

Coverage:

* ``POST /me/tokens`` happy path — returns plaintext once, row
  carries ``kind='personal'`` + ``workspace_id=NULL`` +
  ``subject_user_id`` populated.
* ``POST /me/tokens`` validation — empty scopes (422
  ``scopes_required``); a workspace scope mixed in (422
  ``me_scope_conflict``).
* ``POST /me/tokens`` cap — 6th PAT for the same user returns 422
  ``too_many_personal_tokens``.
* ``GET /me/tokens`` — returns only the caller's PATs, never
  someone else's.
* ``DELETE /me/tokens/{id}`` — revokes the caller's own PAT;
  revoking another user's PAT or a workspace token id returns 404.
* Auth — a request without a session cookie returns 401.

See ``docs/specs/03-auth-and-tokens.md`` §"Personal access tokens"
and ``docs/specs/12-rest-api.md`` §"Auth / me / tokens".
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.identity.models import ApiToken, User
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.workspace.models import Workspace
from app.api.deps import db_session as db_session_dep
from app.api.v1.auth import me_tokens as me_tokens_module
from app.auth.audit import AGNOSTIC_ACTOR_ID, AGNOSTIC_WORKSPACE_ID
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.auth.tokens import (
    record_request_audit,
    truncate_ip_prefix,
)
from app.auth.tokens import (
    verify as verify_token,
)
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.clock import SystemClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user
from tests.integration.auth._cleanup import delete_api_tokens_for_scope

pytestmark = pytest.mark.integration


# Pinned UA / Accept-Language so the :func:`validate` fingerprint
# gate agrees with the seed :func:`issue` call. Matches the shape in
# test_logout.py.
_TEST_UA: str = "pytest-me-tokens"
_TEST_ACCEPT_LANGUAGE: str = "en"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _delete_user_artifacts(session: Session, *, user_id: str) -> None:
    workspace_ids = [
        grant.workspace_id
        for grant in session.scalars(
            select(RoleGrant).where(RoleGrant.user_id == user_id)
        ).all()
    ]
    for audit_row in session.scalars(
        select(AuditLog).where(AuditLog.workspace_id == AGNOSTIC_WORKSPACE_ID)
    ).all():
        diff = audit_row.diff
        if (
            audit_row.actor_id == user_id
            or (isinstance(diff, dict) and diff.get("user_id") == user_id)
            or (isinstance(diff, dict) and diff.get("subject_user_id") == user_id)
        ):
            session.delete(audit_row)
    delete_api_tokens_for_scope(session, user_ids=(user_id,))
    for grant in session.scalars(select(RoleGrant).where(RoleGrant.user_id == user_id)):
        session.delete(grant)
    for workspace_id in workspace_ids:
        workspace = session.get(Workspace, workspace_id)
        if workspace is not None:
            session.delete(workspace)
    for sess in session.scalars(
        select(SessionRow).where(SessionRow.user_id == user_id)
    ).all():
        session.delete(sess)
    user = session.get(User, user_id)
    if user is not None:
        session.delete(user)


def _seed_live_grant(session: Session, *, user_id: str) -> str:
    now = SystemClock().now()
    workspace_id = new_ulid()
    grant_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=f"pat-{workspace_id[-8:].lower()}",
            name="PAT Test Workspace",
            plan="free",
            quota_json={},
            created_at=now,
        )
    )
    session.flush()
    session.add(
        RoleGrant(
            id=grant_id,
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role="worker",
            scope_property_id=None,
            created_at=now,
            created_by_user_id=None,
        )
    )
    return workspace_id


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-me-tokens-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def seed_user(session_factory: sessionmaker[Session]) -> Iterator[str]:
    """Seed a user row and yield its id. Cleans up on teardown."""
    tag = new_ulid()[-8:].lower()
    email = f"pat-{tag}@example.com"
    with session_factory() as s:
        user = bootstrap_user(s, email=email, display_name="PAT User")
        user_id = user.id
        s.commit()
    yield user_id
    with session_factory() as s, tenant_agnostic():
        _delete_user_artifacts(s, user_id=user_id)
        s.commit()


@pytest.fixture
def seed_other_user(session_factory: sessionmaker[Session]) -> Iterator[str]:
    """Seed a second user for cross-subject PAT checks."""
    tag = new_ulid()[-8:].lower()
    email = f"pat-other-{tag}@example.com"
    with session_factory() as s:
        user = bootstrap_user(s, email=email, display_name="Other PAT User")
        user_id = user.id
        s.commit()
    yield user_id
    with session_factory() as s, tenant_agnostic():
        _delete_user_artifacts(s, user_id=user_id)
        s.commit()


@pytest.fixture
def client(
    engine: Engine,
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """:class:`TestClient` mounted on the me_tokens router.

    Patches :func:`app.auth.session.get_settings` so the session
    pepper matches between the seed :func:`issue` and the router's
    :func:`validate`. Uses a dep override for the UoW so writes land
    on ``engine`` and subsequent assertions read the committed rows.
    """
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    app = FastAPI()
    app.include_router(
        me_tokens_module.build_me_tokens_router(),
        prefix="/api/v1",
    )

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


def _issue_session(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    settings: Settings,
) -> str:
    """Issue a real session row for ``user_id``; return the raw cookie value."""
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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestMeTokensHttpFlow:
    """Full POST → GET → DELETE loop for personal access tokens."""

    def test_mint_then_list_then_revoke(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        # 1. Mint
        r = client.post(
            "/api/v1/me/tokens",
            json={
                "label": "kitchen-printer",
                "scopes": {"me.tasks:read": True, "me.bookings:read": True},
                "expires_at_days": 90,
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["token"].startswith("mip_")
        assert body["kind"] == "personal"
        key_id = body["key_id"]

        # Row carries the PAT shape.
        with session_factory() as s:
            row = s.get(ApiToken, key_id)
            assert row is not None
            assert row.kind == "personal"
            assert row.workspace_id is None
            assert row.subject_user_id == seed_user
            assert row.delegate_for_user_id is None

        # 2. List — the PAT appears in the caller's /me/tokens list.
        r_list = client.get("/api/v1/me/tokens")
        assert r_list.status_code == 200, r_list.text
        rows = r_list.json()
        assert len(rows) == 1
        assert rows[0]["key_id"] == key_id
        assert rows[0]["kind"] == "personal"
        # The subject-side list never surfaces the delegate_for_user_id
        # discriminator because the surface is dedicated to PATs.
        assert "delegate_for_user_id" not in rows[0]
        # §03 "Personal access tokens": plaintext `token` is returned
        # ONLY on the 201 mint response — never again. cd-rpxd
        # acceptance criterion #3 — regression-pinned so a future
        # schema edit that re-surfaces the secret fails loudly.
        assert "token" not in rows[0]

        # 3. Revoke — 204.
        r_del = client.delete(f"/api/v1/me/tokens/{key_id}")
        assert r_del.status_code == 204, r_del.text

        # Listing still includes the row, flagged revoked.
        r_list2 = client.get("/api/v1/me/tokens")
        assert r_list2.status_code == 200
        rows2 = r_list2.json()
        assert rows2[0]["key_id"] == key_id
        assert rows2[0]["revoked_at"] is not None

    def test_empty_scopes_is_422_scopes_required(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r = client.post(
            "/api/v1/me/tokens",
            json={"label": "bad", "scopes": {}},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "scopes_required"

    def test_workspace_scope_is_422_me_scope_conflict(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r = client.post(
            "/api/v1/me/tokens",
            json={
                "label": "bad",
                "scopes": {"me.tasks:read": True, "tasks:read": True},
            },
        )
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "me_scope_conflict"

    def test_sixth_pat_is_422_too_many_personal(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        for i in range(5):
            r = client.post(
                "/api/v1/me/tokens",
                json={
                    "label": f"pat-{i}",
                    "scopes": {"me.tasks:read": True},
                },
            )
            assert r.status_code == 201, r.text
        r = client.post(
            "/api/v1/me/tokens",
            json={"label": "6th", "scopes": {"me.tasks:read": True}},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "too_many_personal_tokens"

    def test_no_session_cookie_is_401(self, client: TestClient) -> None:
        client.cookies.clear()
        r = client.post(
            "/api/v1/me/tokens",
            json={"label": "no-sess", "scopes": {"me.tasks:read": True}},
        )
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "session_required"

    def test_get_without_session_cookie_is_401(self, client: TestClient) -> None:
        """``GET /me/tokens`` shares the session-required gate."""
        client.cookies.clear()
        r = client.get("/api/v1/me/tokens")
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "session_required"

    def test_delete_without_session_cookie_is_401(self, client: TestClient) -> None:
        """``DELETE /me/tokens/{id}`` shares the session-required gate."""
        client.cookies.clear()
        r = client.delete("/api/v1/me/tokens/01HWA00000000000000000NOPE")
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "session_required"

    def test_delete_unknown_token_is_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r = client.delete("/api/v1/me/tokens/01HWA00000000000000000NOPE")
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "token_not_found"

    def test_post_revoke_alias_is_idempotent(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r_mint = client.post(
            "/api/v1/me/tokens",
            json={"label": "post-revoke", "scopes": {"me.tasks:read": True}},
        )
        assert r_mint.status_code == 201, r_mint.text
        key_id = r_mint.json()["key_id"]

        r1 = client.post(f"/api/v1/me/tokens/{key_id}/revoke")
        r2 = client.post(f"/api/v1/me/tokens/{key_id}/revoke")
        assert r1.status_code == 204, r1.text
        assert r2.status_code == 204, r2.text

        with session_factory() as s:
            row = s.get(ApiToken, key_id)
            assert row is not None
            assert row.revoked_at is not None

    def test_rotate_returns_new_plaintext_same_key_id_with_overlap(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        with session_factory() as s, tenant_agnostic():
            _seed_live_grant(s, user_id=seed_user)
            s.commit()
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r_mint = client.post(
            "/api/v1/me/tokens",
            json={
                "label": "rotate-me",
                "scopes": {"me.tasks:read": True},
                "expires_at_days": 30,
            },
        )
        assert r_mint.status_code == 201, r_mint.text
        original = r_mint.json()
        key_id = original["key_id"]

        r = client.post(f"/api/v1/me/tokens/{key_id}/rotate")
        assert r.status_code == 200, r.text
        rotated = r.json()
        assert rotated["key_id"] == key_id
        assert rotated["kind"] == "personal"
        assert rotated["token"] != original["token"]
        assert rotated["prefix"] != original["prefix"]
        assert rotated["expires_at"] == original["expires_at"]

        with session_factory() as s:
            assert verify_token(s, token=rotated["token"]).key_id == key_id
        with session_factory() as s:
            assert verify_token(s, token=original["token"]).key_id == key_id

    def test_rotate_another_users_pat_is_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        seed_other_user: str,
    ) -> None:
        other_cookie = _issue_session(
            session_factory, user_id=seed_other_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, other_cookie)
        r_mint = client.post(
            "/api/v1/me/tokens",
            json={"label": "foreign", "scopes": {"me.tasks:read": True}},
        )
        assert r_mint.status_code == 201, r_mint.text
        foreign_id = r_mint.json()["key_id"]

        caller_cookie = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, caller_cookie)
        r = client.post(f"/api/v1/me/tokens/{foreign_id}/rotate")
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "token_not_found"

    def test_audit_returns_personal_lifecycle_and_request_rows(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r_mint = client.post(
            "/api/v1/me/tokens",
            json={"label": "audit", "scopes": {"me.tasks:read": True}},
        )
        assert r_mint.status_code == 201, r_mint.text
        key_id = r_mint.json()["key_id"]
        with session_factory() as s, tenant_agnostic():
            record_request_audit(
                s,
                token_id=key_id,
                method="GET",
                path="/api/v1/me/profile",
                status=200,
                ip_prefix=truncate_ip_prefix("203.0.113.42"),
                user_agent="pat-client/1.0",
                correlation_id="req-pat",
            )
            s.commit()

        r = client.get(f"/api/v1/me/tokens/{key_id}/audit")
        assert r.status_code == 200, r.text
        entries = r.json()
        actions = [entry["action"] for entry in entries]
        assert "api_token.minted" in actions
        assert "api_token.request" in actions
        request = next(
            entry for entry in entries if entry["action"] == "api_token.request"
        )
        assert request["actor_id"] == seed_user
        assert request["method"] == "GET"
        assert request["path"] == "/api/v1/me/profile"
        assert request["status"] == 200
        assert request["ip_prefix"] == "203.0.113.0/24"

    def test_audit_another_users_pat_returns_empty(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
        seed_other_user: str,
    ) -> None:
        other_cookie = _issue_session(
            session_factory, user_id=seed_other_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, other_cookie)
        r_mint = client.post(
            "/api/v1/me/tokens",
            json={"label": "foreign-audit", "scopes": {"me.tasks:read": True}},
        )
        assert r_mint.status_code == 201, r_mint.text
        foreign_id = r_mint.json()["key_id"]

        caller_cookie = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, caller_cookie)
        r = client.get(f"/api/v1/me/tokens/{foreign_id}/audit")
        assert r.status_code == 200
        assert r.json() == []


# ---------------------------------------------------------------------------
# cd-rqhy: identity-surface audit rows + shared helper
# ---------------------------------------------------------------------------


class TestMeTokensIdentityAudit:
    """``identity.token.minted`` / ``identity.token.revoked`` rows land.

    The router writes through :func:`app.audit.write_audit` with the
    shared :func:`app.auth.audit.agnostic_audit_ctx` sentinel — never
    a re-derived copy. Both audit rows pin the zero-ULID workspace +
    actor and carry the acting user's id in the ``diff`` payload, so
    the row shape is portable across every bare-host identity surface
    (avatar, signup, recovery, magic-link, session).
    """

    def test_mint_writes_identity_token_minted(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """``POST /me/tokens`` writes one ``identity.token.minted`` row."""
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r = client.post(
            "/api/v1/me/tokens",
            json={
                "label": "audit-mint",
                "scopes": {"me.tasks:read": True},
                "expires_at_days": 30,
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        key_id = body["key_id"]
        prefix = body["prefix"]

        with session_factory() as s, tenant_agnostic():
            rows = (
                s.query(AuditLog)
                .filter(AuditLog.entity_id == key_id)
                .filter(AuditLog.action == "identity.token.minted")
                .all()
            )
            assert len(rows) == 1, "exactly one identity.token.minted row"
            row = rows[0]
            # Shared zero-ULID seam — same shape every other bare-host
            # identity surface uses.
            assert row.workspace_id == AGNOSTIC_WORKSPACE_ID
            assert row.actor_id == AGNOSTIC_ACTOR_ID
            assert row.actor_kind == "system"
            assert row.entity_kind == "api_token"
            diff = row.diff
            assert isinstance(diff, dict)
            assert diff["user_id"] == seed_user
            # cd-6vq5 idiom: before_hash transitions None -> key_id
            # on mint; after_hash carries the new live state.
            assert diff["before_hash"] is None
            assert diff["after_hash"] == key_id
            assert diff["prefix"] == prefix
            assert diff["kind"] == "personal"
            assert diff["scopes"] == ["me.tasks:read"]

    def test_mint_writes_both_api_token_and_identity_rows(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """The two audit rows coexist: entity-lifecycle + identity-surface.

        ``app.auth.tokens.mint`` writes ``api_token.minted``;
        ``/me/tokens`` writes ``identity.token.minted``. Both share
        ``entity_id`` so an investigator can join the views.
        """
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r = client.post(
            "/api/v1/me/tokens",
            json={"label": "two-rows", "scopes": {"me.tasks:read": True}},
        )
        assert r.status_code == 201
        key_id = r.json()["key_id"]

        with session_factory() as s, tenant_agnostic():
            actions = sorted(
                row.action
                for row in s.query(AuditLog).filter(AuditLog.entity_id == key_id).all()
            )
            assert actions == ["api_token.minted", "identity.token.minted"]

    def test_revoke_writes_identity_token_revoked(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """``DELETE /me/tokens/{id}`` writes one ``identity.token.revoked``."""
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r_mint = client.post(
            "/api/v1/me/tokens",
            json={"label": "audit-revoke", "scopes": {"me.tasks:read": True}},
        )
        assert r_mint.status_code == 201
        key_id = r_mint.json()["key_id"]

        r_del = client.delete(f"/api/v1/me/tokens/{key_id}")
        assert r_del.status_code == 204

        with session_factory() as s, tenant_agnostic():
            rows = (
                s.query(AuditLog)
                .filter(AuditLog.entity_id == key_id)
                .filter(AuditLog.action == "identity.token.revoked")
                .all()
            )
            assert len(rows) == 1
            row = rows[0]
            assert row.workspace_id == AGNOSTIC_WORKSPACE_ID
            assert row.actor_id == AGNOSTIC_ACTOR_ID
            assert row.actor_kind == "system"
            diff = row.diff
            assert isinstance(diff, dict)
            assert diff["user_id"] == seed_user
            # cd-6vq5 idiom inverted: before_hash carries the old live
            # state (key_id), after_hash transitions to None.
            assert diff["before_hash"] == key_id
            assert diff["after_hash"] is None
            assert diff["kind"] == "personal"

    def test_rotate_writes_identity_token_rotated(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """``POST /me/tokens/{id}/rotate`` writes one identity audit row."""
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r_mint = client.post(
            "/api/v1/me/tokens",
            json={"label": "audit-rotate", "scopes": {"me.tasks:read": True}},
        )
        assert r_mint.status_code == 201
        key_id = r_mint.json()["key_id"]

        r_rotate = client.post(f"/api/v1/me/tokens/{key_id}/rotate")
        assert r_rotate.status_code == 200
        rotated = r_rotate.json()

        with session_factory() as s, tenant_agnostic():
            rows = (
                s.query(AuditLog)
                .filter(AuditLog.entity_id == key_id)
                .filter(AuditLog.action == "identity.token.rotated")
                .all()
            )
            assert len(rows) == 1
            row = rows[0]
            assert row.workspace_id == AGNOSTIC_WORKSPACE_ID
            assert row.actor_id == AGNOSTIC_ACTOR_ID
            assert row.actor_kind == "system"
            diff = row.diff
            assert isinstance(diff, dict)
            assert diff["user_id"] == seed_user
            assert diff["before_hash"] == key_id
            assert diff["after_hash"] == key_id
            assert diff["prefix"] == rotated["prefix"]
            assert diff["kind"] == "personal"

    def test_revoke_idempotent_does_not_duplicate_audit(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """A second DELETE on an already-revoked PAT writes no second row.

        Mirrors ``identity.avatar.cleared`` state-gating: an idempotent
        retry that didn't actually change state writes no row, so the
        log doesn't accumulate noise on buggy-client retries.
        """
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r_mint = client.post(
            "/api/v1/me/tokens",
            json={"label": "idempotent", "scopes": {"me.tasks:read": True}},
        )
        key_id = r_mint.json()["key_id"]

        r1 = client.delete(f"/api/v1/me/tokens/{key_id}")
        # Second DELETE on the same id is an idempotent 204 — the
        # caller still owns the row and ``revoke_personal`` returns
        # silently when ``revoked_at`` is already set (no second
        # ``api_token.revoked`` row, matching the workspace-side
        # "one revoke event per token lifetime" invariant). The state
        # gate in the router suppresses the identity row the same way.
        r2 = client.delete(f"/api/v1/me/tokens/{key_id}")
        assert r1.status_code == 204
        assert r2.status_code == 204

        with session_factory() as s, tenant_agnostic():
            rows = (
                s.query(AuditLog)
                .filter(AuditLog.entity_id == key_id)
                .filter(AuditLog.action == "identity.token.revoked")
                .all()
            )
            assert len(rows) == 1, "exactly one revoke row across both DELETEs"

    def test_revoke_unknown_token_writes_no_identity_audit(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """Revoking a non-existent PAT id is a 404 with no audit row.

        State-gated: there's no live row to revoke, so no identity
        event happened.
        """
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        nonce_id = "01HWA00000000000000000FAKE"
        r = client.delete(f"/api/v1/me/tokens/{nonce_id}")
        assert r.status_code == 404

        with session_factory() as s, tenant_agnostic():
            rows = (
                s.query(AuditLog)
                .filter(AuditLog.entity_id == nonce_id)
                .filter(AuditLog.action == "identity.token.revoked")
                .all()
            )
            assert rows == []


class TestSharedAgnosticAuditCtx:
    """The six bare-host identity surfaces share one helper.

    cd-rqhy consolidated six byte-identical ``_agnostic_audit_ctx``
    copies into :func:`app.auth.audit.agnostic_audit_ctx`. This test
    pins the consolidation: the canonical helper exists, every former
    callsite imports it (rather than re-deriving the zero-ULID shape),
    and the shape itself stays the spec-pinned sentinel.
    """

    def test_canonical_helper_returns_zero_ulid_system_actor(self) -> None:
        """The canonical factory pins the zero-ULID system-actor shape."""
        from app.auth.audit import agnostic_audit_ctx

        ctx = agnostic_audit_ctx()
        assert ctx.workspace_id == "0" * 26
        assert ctx.actor_id == "0" * 26
        assert ctx.actor_kind == "system"
        assert ctx.principal_kind == "system"
        assert ctx.workspace_slug == ""
        # Correlation id is fresh per call so sibling writes get their
        # own trace cursor.
        ctx2 = agnostic_audit_ctx()
        assert ctx.audit_correlation_id != ctx2.audit_correlation_id

    def test_callers_import_canonical_helper(self) -> None:
        """Every former ``_agnostic_audit_ctx`` is now the shared one.

        Pin that the six bare-host identity modules all resolve their
        helper to the same function object — guards against a future
        edit that re-introduces a local copy and silently drifts.
        """
        from app.api.v1.auth import (
            me_avatar as me_avatar_module,
        )
        from app.api.v1.auth import (
            recovery as recovery_router,
        )
        from app.api.v1.auth import (
            signup as signup_router,
        )
        from app.auth import (
            audit as canonical,
        )
        from app.auth import (
            magic_link as magic_link_module,
        )
        from app.auth import (
            recovery as recovery_module,
        )
        from app.auth import (
            session as session_module,
        )
        from app.auth import (
            signup as signup_module,
        )
        from app.domain.identity import (
            email_change as email_change_module,
        )

        target = canonical.agnostic_audit_ctx
        # Every bare-host identity module either re-exports the
        # canonical helper under its legacy name (``_agnostic_audit_ctx``
        # / ``_identity_audit_ctx``) or imports it directly.
        assert magic_link_module._agnostic_audit_ctx is target
        assert recovery_module._agnostic_audit_ctx is target
        assert signup_module._agnostic_audit_ctx is target
        assert session_module._agnostic_audit_ctx is target
        assert email_change_module._agnostic_audit_ctx is target
        assert me_avatar_module._identity_audit_ctx is target
        assert signup_router._agnostic_audit_ctx is target
        assert recovery_router._agnostic_audit_ctx is target
