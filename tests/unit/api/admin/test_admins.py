"""Unit tests for :mod:`app.api.admin.admins`.

Covers the admin-team CRUD + groups routes per spec §12 "Admin
surface" §"Admin team":

* ``GET /admins`` — listing oldest-first.
* ``POST /admins`` — grant by user_id / email; idempotent re-grant;
  validation 422s on missing / ambiguous / unknown target.
* ``POST /admins/{id}/revoke`` — soft-retire (cd-x1xh); 404 on
  missing; audit row emits and the row's ``revoked_at`` /
  ``revoked_by_user_id`` / ``ended_on`` are stamped.
* ``GET /admins/groups`` — owners from ``deployment_owner`` and
  managers from deployment ``role_grant`` rows.
* Owners-group add / revoke require a deployment owner.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.identity.models import User
from app.api.admin.admins import (
    ERROR_AMBIGUOUS_TARGET,
    ERROR_LAST_OWNER,
    ERROR_MISSING_TARGET,
    ERROR_USER_NOT_FOUND,
)
from app.auth.session import SESSION_COOKIE_NAME
from app.config import Settings
from app.tenancy import tenant_agnostic
from tests.unit.api.admin._helpers import (
    PINNED,
    build_client,
    engine_fixture,
    grant_deployment_admin,
    grant_deployment_owner,
    issue_session,
    seed_user,
    settings_fixture,
)


@pytest.fixture
def settings() -> Settings:
    return settings_fixture("admins")


@pytest.fixture
def engine() -> Iterator[Engine]:
    yield from engine_fixture()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    yield from build_client(settings, session_factory, monkeypatch)


def _admin_cookie(
    session_factory: sessionmaker[Session], settings: Settings, *, owner: bool = True
) -> tuple[str, str]:
    with session_factory() as s:
        user_id = seed_user(s, email="ada@example.com", display_name="Ada")
        grant_deployment_admin(s, user_id=user_id)
        if owner:
            grant_deployment_owner(s, user_id=user_id)
        s.commit()
    return user_id, issue_session(session_factory, user_id=user_id, settings=settings)


class TestListAdmins:
    def test_returns_active_grants(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            ada = seed_user(s, email="ada@example.com", display_name="Ada")
            grace = seed_user(s, email="grace@example.com", display_name="Grace")
            grant_deployment_admin(s, user_id=ada, created_at=PINNED)
            grant_deployment_admin(
                s,
                user_id=grace,
                created_at=PINNED,
                created_by_user_id=ada,
            )
            s.commit()
        cookie = issue_session(session_factory, user_id=ada, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        body = client.get("/admin/api/v1/admins").json()
        emails = [row["email"] for row in body["admins"]]
        assert set(emails) == {"ada@example.com", "grace@example.com"}


class TestGrantAdmin:
    def test_grant_by_user_id_creates_row_and_audits(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            target = seed_user(s, email="target@example.com", display_name="T")
            s.commit()
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.post(
            "/admin/api/v1/admins",
            json={"user_id": target},
        )
        assert resp.status_code == 200, resp.text
        admin = resp.json()["admin"]
        assert admin["user_id"] == target
        assert admin["email"] == "target@example.com"

        with session_factory() as s, tenant_agnostic():
            grants = s.scalars(
                select(RoleGrant)
                .where(RoleGrant.scope_kind == "deployment")
                .where(RoleGrant.user_id == target)
            ).all()
            assert len(grants) == 1
            audits = s.scalars(
                select(AuditLog).where(AuditLog.action == "admin.granted")
            ).all()
            assert len(audits) == 1

    def test_grant_by_email_canonicalises_lookup(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            seed_user(s, email="Capital@Example.com", display_name="Cap")
            s.commit()
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.post(
            "/admin/api/v1/admins",
            json={"email": "CAPITAL@example.COM"},
        )
        assert resp.status_code == 200
        assert resp.json()["admin"]["email"] == "Capital@Example.com"

    def test_re_grant_is_idempotent_no_extra_audit(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            target = seed_user(s, email="target@example.com", display_name="T")
            s.commit()
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        first = client.post("/admin/api/v1/admins", json={"user_id": target})
        second = client.post("/admin/api/v1/admins", json={"user_id": target})
        assert first.status_code == 200
        assert second.status_code == 200
        # Same grant id — idempotent.
        assert first.json()["admin"]["id"] == second.json()["admin"]["id"]
        with session_factory() as s, tenant_agnostic():
            audits = s.scalars(
                select(AuditLog).where(AuditLog.action == "admin.granted")
            ).all()
            assert len(audits) == 1

    def test_missing_target_returns_typed_422(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.post("/admin/api/v1/admins", json={})
        assert resp.status_code == 422
        assert resp.json().get("error") == ERROR_MISSING_TARGET

    def test_ambiguous_target_returns_typed_422(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.post(
            "/admin/api/v1/admins",
            json={"user_id": "01HBOGUS00000000000000000", "email": "x@y.com"},
        )
        assert resp.status_code == 422
        assert resp.json().get("error") == ERROR_AMBIGUOUS_TARGET

    def test_unknown_user_returns_typed_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.post(
            "/admin/api/v1/admins",
            json={"user_id": "01HBOGUS00000000000000000"},
        )
        assert resp.status_code == 404
        assert resp.json().get("error") == ERROR_USER_NOT_FOUND

    def test_archived_user_treated_as_not_found(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s, tenant_agnostic():
            target = seed_user(s, email="archived@example.com", display_name="Old")
            row = s.get(User, target)
            assert row is not None
            row.archived_at = datetime.now(UTC)
            s.commit()
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.post(
            "/admin/api/v1/admins",
            json={"email": "archived@example.com"},
        )
        assert resp.status_code == 404
        assert resp.json().get("error") == ERROR_USER_NOT_FOUND


class TestRevokeAdmin:
    def test_revoke_soft_retires_row_and_audits(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """cd-x1xh: ``POST /admins/{id}/revoke`` is soft-retire, not hard-delete.

        The row stays in ``role_grant`` with ``revoked_at`` /
        ``revoked_by_user_id`` / ``ended_on`` stamped. Live-grant read
        paths (``list_admins`` etc.) filter on ``revoked_at IS NULL``
        so the row disappears from the admin surface; the audit trail
        survives via the preserved row plus the ``admin.revoked``
        audit_log entry.
        """
        with session_factory() as s:
            ada = seed_user(s, email="ada@example.com", display_name="Ada")
            grace = seed_user(s, email="grace@example.com", display_name="Grace")
            grant_deployment_admin(s, user_id=ada)
            grant_deployment_owner(s, user_id=ada)
            grace_grant = grant_deployment_admin(s, user_id=grace)
            s.commit()
        cookie = issue_session(session_factory, user_id=ada, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.post(f"/admin/api/v1/admins/{grace_grant}/revoke")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"revoked_id": grace_grant}
        with session_factory() as s, tenant_agnostic():
            row = s.get(RoleGrant, grace_grant)
            assert row is not None
            assert row.revoked_at is not None
            assert row.revoked_by_user_id == ada
            assert row.ended_on is not None
            audits = s.scalars(
                select(AuditLog).where(AuditLog.action == "admin.revoked")
            ).all()
            assert len(audits) == 1

    def test_revoke_unknown_id_404s(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.post("/admin/api/v1/admins/01HBOGUS00000000000000000/revoke")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"

    def test_revoke_workspace_grant_404s(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        from app.adapters.db.workspace.models import Workspace
        from app.util.ulid import new_ulid

        with session_factory() as s, tenant_agnostic():
            ws_id = new_ulid()
            s.add(
                Workspace(
                    id=ws_id,
                    slug="ws-x",
                    name="WS",
                    plan="free",
                    quota_json={},
                    created_at=PINNED,
                )
            )
            target = seed_user(s, email="ws@example.com", display_name="WS")
            ws_grant_id = new_ulid()
            s.add(
                RoleGrant(
                    id=ws_grant_id,
                    workspace_id=ws_id,
                    user_id=target,
                    grant_role="manager",
                    scope_kind="workspace",
                    created_at=PINNED,
                )
            )
            s.commit()
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.post(f"/admin/api/v1/admins/{ws_grant_id}/revoke")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"


class TestGroups:
    def test_groups_listing_returns_owners_and_managers(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        body = client.get("/admin/api/v1/admins/groups").json()
        groups = {g["slug"]: g for g in body["groups"]}
        assert list(groups) == ["owners", "managers"]
        assert [m["user_id"] for m in groups["owners"]["members"]] == [user]
        assert [m["user_id"] for m in groups["managers"]["members"]] == [user]

    def test_non_owner_owners_add_404s(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            target = seed_user(s, email="t@example.com", display_name="T")
            s.commit()
        _user, cookie = _admin_cookie(session_factory, settings, owner=False)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.post(
            "/admin/api/v1/admins/groups/owners/members",
            json={"user_id": target},
        )
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"

    def test_owner_adds_owner_and_audits(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            target = seed_user(s, email="t@example.com", display_name="T")
            s.commit()
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.post(
            "/admin/api/v1/admins/groups/owners/members",
            json={"user_id": target},
        )
        assert resp.status_code == 200, resp.text
        assert target in {row["user_id"] for row in resp.json()["members"]}
        with session_factory() as s, tenant_agnostic():
            audits = s.scalars(
                select(AuditLog).where(AuditLog.action == "admin.owner_added")
            ).all()
        assert len(audits) == 1
        assert audits[0].actor_was_owner_member is True

    def test_owner_add_is_idempotent_without_extra_audit(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        with session_factory() as s:
            target = seed_user(s, email="t@example.com", display_name="T")
            s.commit()
        _user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        first = client.post(
            "/admin/api/v1/admins/groups/owners/members",
            json={"user_id": target},
        )
        second = client.post(
            "/admin/api/v1/admins/groups/owners/members",
            json={"user_id": target},
        )
        assert first.status_code == 200
        assert second.status_code == 200
        with session_factory() as s, tenant_agnostic():
            audits = s.scalars(
                select(AuditLog).where(AuditLog.action == "admin.owner_added")
            ).all()
        assert len(audits) == 1

    def test_owner_revoke_removes_owner(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        caller, cookie = _admin_cookie(session_factory, settings)
        with session_factory() as s:
            target = seed_user(s, email="t@example.com", display_name="T")
            grant_deployment_owner(s, user_id=target, added_by_user_id=caller)
            s.commit()
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.post(
            f"/admin/api/v1/admins/groups/owners/members/{target}/revoke"
        )
        assert resp.status_code == 200, resp.text
        assert target not in {row["user_id"] for row in resp.json()["members"]}

    def test_last_owner_revoke_returns_typed_422(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user, cookie = _admin_cookie(session_factory, settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        resp = client.post(f"/admin/api/v1/admins/groups/owners/members/{user}/revoke")
        assert resp.status_code == 422
        assert resp.json().get("error") == ERROR_LAST_OWNER
