"""Integration test for ``/api/v1/me/push-tokens`` — native push-token surface.

Exercises the bare-host native-app push-token router end-to-end against
a real engine (SQLite by default; Postgres when
``CREWDAY_TEST_DB=postgres``), driving the FastAPI router with a live
passkey session.

Coverage:

* ``POST /me/push-tokens`` happy path with the gate ``ON`` — returns
  201, row carries ``platform`` + raw ``token`` + ``last_seen_at``;
  audit row fires with no raw token in the diff.
* ``POST`` 501 ``push_unavailable`` when
  :attr:`Settings.native_push_enabled` is off.
* ``GET / DELETE`` are always live regardless of the gate (sign-out
  must be able to prune even on a deployment with native push off).
* ``GET`` self-only — never returns rows owned by another user.
* ``DELETE /me/push-tokens/{id}`` cross-user collapse — removing
  another user's row is a silent no-op (204), and the row stays
  intact for its owner.
* ``PUT /me/push-tokens/{id}`` cross-user collapse — refresh against
  another user's row returns 404 ``push_token_not_found``.
* 409 ``token_claimed`` — second user registering an
  ``(platform, token)`` pair already owned.
* 401 ``session_required`` — no cookie.
* Audit redaction — a ``user_push_token.registered`` row carries the
  diff metadata but never the raw ``token`` bytes.

See ``docs/specs/02-domain-model.md`` §"user_push_token",
``docs/specs/12-rest-api.md`` §"Device push tokens".
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
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.identity.models import User, UserPushToken
from app.api.deps import db_session as db_session_dep
from app.api.v1.auth import me_push_tokens as me_push_tokens_module
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.tenancy import tenant_agnostic
from tests.factories.identity import bootstrap_user

pytestmark = pytest.mark.integration


_TEST_UA: str = "pytest-me-push-tokens"
_TEST_ACCEPT_LANGUAGE: str = "en"
_AGNOSTIC_WORKSPACE_ID = "00000000000000000000000000"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_gate_on() -> Settings:
    """Settings with ``native_push_enabled=True`` (registration allowed)."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-me-push-tokens-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
        native_push_enabled=True,
    )


@pytest.fixture
def settings_gate_off() -> Settings:
    """Settings with ``native_push_enabled=False`` (501 on POST)."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-me-push-tokens-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
        native_push_enabled=False,
    )


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def seed_user(session_factory: sessionmaker[Session]) -> Iterator[str]:
    """Seed the primary user row and yield its id; cleans up on teardown."""
    from app.util.ulid import new_ulid

    tag = new_ulid()[-8:].lower()
    email = f"push-{tag}@example.com"
    with session_factory() as s:
        user = bootstrap_user(s, email=email, display_name="Push User")
        user_id = user.id
        s.commit()
    yield user_id
    with session_factory() as s, tenant_agnostic():
        for tok in s.scalars(
            select(UserPushToken).where(UserPushToken.user_id == user_id)
        ).all():
            s.delete(tok)
        for sess in s.scalars(
            select(SessionRow).where(SessionRow.user_id == user_id)
        ).all():
            s.delete(sess)
        u = s.get(User, user_id)
        if u is not None:
            s.delete(u)
        s.commit()


@pytest.fixture
def seed_other_user(session_factory: sessionmaker[Session]) -> Iterator[str]:
    """Seed a second user row used for cross-user collision / collapse tests."""
    from app.util.ulid import new_ulid

    tag = new_ulid()[-8:].lower()
    email = f"push-other-{tag}@example.com"
    with session_factory() as s:
        user = bootstrap_user(s, email=email, display_name="Other Push User")
        user_id = user.id
        s.commit()
    yield user_id
    with session_factory() as s, tenant_agnostic():
        for tok in s.scalars(
            select(UserPushToken).where(UserPushToken.user_id == user_id)
        ).all():
            s.delete(tok)
        for sess in s.scalars(
            select(SessionRow).where(SessionRow.user_id == user_id)
        ).all():
            s.delete(sess)
        u = s.get(User, user_id)
        if u is not None:
            s.delete(u)
        s.commit()


def _build_client(
    *,
    engine: Engine,
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Mount the me-push-tokens router on a fresh FastAPI app.

    Patches both :func:`app.auth.session.get_settings` and
    :func:`app.config.get_settings` so the session pepper agrees and
    the router's gate reads our test-supplied ``settings``.
    """
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)
    # The router itself reads ``app.config.get_settings`` per request
    # — patch the same module attribute so the Settings cache is not
    # in play for these tests. Using monkeypatch.setattr with the
    # importable module path keeps the override scoped to the fixture.
    monkeypatch.setattr("app.api.v1.auth.me_push_tokens.get_settings", lambda: settings)

    app = FastAPI()
    app.include_router(
        me_push_tokens_module.build_me_push_tokens_router(),
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


@pytest.fixture
def client_on(
    engine: Engine,
    settings_gate_on: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """``TestClient`` with the registration gate ON."""
    yield from _build_client(
        engine=engine,
        settings=settings_gate_on,
        session_factory=session_factory,
        monkeypatch=monkeypatch,
    )


@pytest.fixture
def client_off(
    engine: Engine,
    settings_gate_off: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """``TestClient`` with the registration gate OFF (POST -> 501)."""
    yield from _build_client(
        engine=engine,
        settings=settings_gate_off,
        session_factory=session_factory,
        monkeypatch=monkeypatch,
    )


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
# POST happy / gate / collision
# ---------------------------------------------------------------------------


class TestPostMePushToken:
    def test_register_then_list_then_delete(
        self,
        client_on: TestClient,
        session_factory: sessionmaker[Session],
        settings_gate_on: Settings,
        seed_user: str,
    ) -> None:
        cookie = _issue_session(
            session_factory, user_id=seed_user, settings=settings_gate_on
        )
        client_on.cookies.set(SESSION_COOKIE_NAME, cookie)

        # 1. Register
        r = client_on.post(
            "/api/v1/me/push-tokens",
            json={
                "platform": "android",
                "token": "fcm-handle-alpha",
                "device_label": "Pixel 9",
                "app_version": "1.0.0",
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["platform"] == "android"
        assert body["device_label"] == "Pixel 9"
        # §02 — raw token MUST NOT echo back over the wire.
        assert "token" not in body
        token_id = body["id"]

        # Row carries the raw token at the DB layer.
        with session_factory() as s, tenant_agnostic():
            row = s.get(UserPushToken, token_id)
            assert row is not None
            assert row.token == "fcm-handle-alpha"
            assert row.user_id == seed_user

        # Audit row fires with redacted diff (no raw token).
        with session_factory() as s, tenant_agnostic():
            audit = list(
                s.scalars(
                    select(AuditLog).where(
                        AuditLog.workspace_id == _AGNOSTIC_WORKSPACE_ID,
                        AuditLog.action == "user_push_token.registered",
                        AuditLog.entity_id == token_id,
                    )
                ).all()
            )
        assert len(audit) == 1
        diff = audit[0].diff
        assert isinstance(diff, dict)
        assert diff["platform"] == "android"
        assert "token" not in diff

        # 2. List — exactly the row we just registered.
        r_list = client_on.get("/api/v1/me/push-tokens")
        assert r_list.status_code == 200, r_list.text
        rows = r_list.json()
        assert len(rows) == 1
        assert rows[0]["id"] == token_id
        assert "token" not in rows[0]

        # 3. Delete — 204.
        r_del = client_on.delete(f"/api/v1/me/push-tokens/{token_id}")
        assert r_del.status_code == 204, r_del.text

        with session_factory() as s, tenant_agnostic():
            assert s.get(UserPushToken, token_id) is None

    def test_register_idempotent_no_second_audit(
        self,
        client_on: TestClient,
        session_factory: sessionmaker[Session],
        settings_gate_on: Settings,
        seed_user: str,
    ) -> None:
        cookie = _issue_session(
            session_factory, user_id=seed_user, settings=settings_gate_on
        )
        client_on.cookies.set(SESSION_COOKIE_NAME, cookie)

        body = {"platform": "ios", "token": "apns-handle-idem"}
        r1 = client_on.post("/api/v1/me/push-tokens", json=body)
        r2 = client_on.post("/api/v1/me/push-tokens", json=body)
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["id"] == r2.json()["id"]

        with session_factory() as s, tenant_agnostic():
            audit = list(
                s.scalars(
                    select(AuditLog).where(
                        AuditLog.workspace_id == _AGNOSTIC_WORKSPACE_ID,
                        AuditLog.action == "user_push_token.registered",
                        AuditLog.entity_id == r1.json()["id"],
                    )
                ).all()
            )
        # Re-registration is not audit-worthy — only the first call wrote a row.
        assert len(audit) == 1

    def test_register_501_when_gate_off(
        self,
        client_off: TestClient,
        session_factory: sessionmaker[Session],
        settings_gate_off: Settings,
        seed_user: str,
    ) -> None:
        cookie = _issue_session(
            session_factory, user_id=seed_user, settings=settings_gate_off
        )
        client_off.cookies.set(SESSION_COOKIE_NAME, cookie)
        r = client_off.post(
            "/api/v1/me/push-tokens",
            json={"platform": "android", "token": "fcm-handle"},
        )
        assert r.status_code == 501
        assert r.json()["detail"]["error"] == "push_unavailable"

    def test_get_and_delete_live_when_gate_off(
        self,
        client_off: TestClient,
        session_factory: sessionmaker[Session],
        settings_gate_off: Settings,
        seed_user: str,
    ) -> None:
        """Sign-out cleanup must work even on a gate-off deployment.

        We seed a row directly in the DB (bypassing the POST gate) so
        we can prove that ``GET`` and ``DELETE`` stay live even when
        the registration gate is off — that asymmetry is the §02
        invariant a sign-out flow leans on.
        """
        from app.util.ulid import new_ulid

        # Seed a row directly so we don't fight the gate.
        token_id = new_ulid()
        with session_factory() as s, tenant_agnostic():
            from datetime import UTC, datetime

            now = datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC)
            s.add(
                UserPushToken(
                    id=token_id,
                    user_id=seed_user,
                    platform="android",
                    token="fcm-handle-keep",
                    device_label=None,
                    app_version=None,
                    created_at=now,
                    last_seen_at=now,
                    disabled_at=None,
                )
            )
            s.commit()

        # Drive list + delete on the gate-off client.
        cookie = _issue_session(
            session_factory, user_id=seed_user, settings=settings_gate_off
        )
        client_off.cookies.set(SESSION_COOKIE_NAME, cookie)

        r_list = client_off.get("/api/v1/me/push-tokens")
        assert r_list.status_code == 200
        assert len(r_list.json()) == 1
        assert r_list.json()[0]["id"] == token_id

        r_del = client_off.delete(f"/api/v1/me/push-tokens/{token_id}")
        assert r_del.status_code == 204

    def test_register_409_token_claimed(
        self,
        client_on: TestClient,
        session_factory: sessionmaker[Session],
        settings_gate_on: Settings,
        seed_user: str,
        seed_other_user: str,
    ) -> None:
        # Owner registers first.
        cookie_owner = _issue_session(
            session_factory, user_id=seed_user, settings=settings_gate_on
        )
        client_on.cookies.set(SESSION_COOKIE_NAME, cookie_owner)
        r1 = client_on.post(
            "/api/v1/me/push-tokens",
            json={"platform": "android", "token": "shared-handle"},
        )
        assert r1.status_code == 201

        # Second user attempts the same (platform, token) pair.
        cookie_other = _issue_session(
            session_factory, user_id=seed_other_user, settings=settings_gate_on
        )
        client_on.cookies.set(SESSION_COOKIE_NAME, cookie_other)
        r2 = client_on.post(
            "/api/v1/me/push-tokens",
            json={"platform": "android", "token": "shared-handle"},
        )
        assert r2.status_code == 409
        assert r2.json()["detail"]["error"] == "token_claimed"

    def test_register_unknown_platform_is_422(
        self,
        client_on: TestClient,
        session_factory: sessionmaker[Session],
        settings_gate_on: Settings,
        seed_user: str,
    ) -> None:
        cookie = _issue_session(
            session_factory, user_id=seed_user, settings=settings_gate_on
        )
        client_on.cookies.set(SESSION_COOKIE_NAME, cookie)
        r = client_on.post(
            "/api/v1/me/push-tokens",
            json={"platform": "windows", "token": "h"},
        )
        # Pydantic literal narrowing rejects with the FastAPI 422 envelope.
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Self-only rules
# ---------------------------------------------------------------------------


class TestSelfOnly:
    def test_get_returns_only_caller_rows(
        self,
        client_on: TestClient,
        session_factory: sessionmaker[Session],
        settings_gate_on: Settings,
        seed_user: str,
        seed_other_user: str,
    ) -> None:
        # Caller registers their own row.
        cookie_owner = _issue_session(
            session_factory, user_id=seed_user, settings=settings_gate_on
        )
        client_on.cookies.set(SESSION_COOKIE_NAME, cookie_owner)
        client_on.post(
            "/api/v1/me/push-tokens",
            json={"platform": "android", "token": "owner-handle"},
        )

        # Other user registers theirs.
        cookie_other = _issue_session(
            session_factory, user_id=seed_other_user, settings=settings_gate_on
        )
        client_on.cookies.set(SESSION_COOKIE_NAME, cookie_other)
        client_on.post(
            "/api/v1/me/push-tokens",
            json={"platform": "ios", "token": "other-handle"},
        )

        # Caller's GET returns only their row.
        client_on.cookies.set(SESSION_COOKIE_NAME, cookie_owner)
        r = client_on.get("/api/v1/me/push-tokens")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["user_id"] == seed_user

    def test_put_cross_user_target_collapses_to_404(
        self,
        client_on: TestClient,
        session_factory: sessionmaker[Session],
        settings_gate_on: Settings,
        seed_user: str,
        seed_other_user: str,
    ) -> None:
        # Owner registers.
        cookie_owner = _issue_session(
            session_factory, user_id=seed_user, settings=settings_gate_on
        )
        client_on.cookies.set(SESSION_COOKIE_NAME, cookie_owner)
        r_reg = client_on.post(
            "/api/v1/me/push-tokens",
            json={"platform": "android", "token": "owner-handle-2"},
        )
        token_id = r_reg.json()["id"]

        # Intruder targets the owner's id.
        cookie_other = _issue_session(
            session_factory, user_id=seed_other_user, settings=settings_gate_on
        )
        client_on.cookies.set(SESSION_COOKIE_NAME, cookie_other)
        r_put = client_on.put(
            f"/api/v1/me/push-tokens/{token_id}",
            json={},
        )
        assert r_put.status_code == 404
        assert r_put.json()["detail"]["error"] == "push_token_not_found"

    def test_delete_cross_user_target_is_silent_204(
        self,
        client_on: TestClient,
        session_factory: sessionmaker[Session],
        settings_gate_on: Settings,
        seed_user: str,
        seed_other_user: str,
    ) -> None:
        # Owner registers.
        cookie_owner = _issue_session(
            session_factory, user_id=seed_user, settings=settings_gate_on
        )
        client_on.cookies.set(SESSION_COOKIE_NAME, cookie_owner)
        r_reg = client_on.post(
            "/api/v1/me/push-tokens",
            json={"platform": "android", "token": "owner-handle-3"},
        )
        token_id = r_reg.json()["id"]

        # Intruder targets the owner's id — 204 (no enumeration).
        cookie_other = _issue_session(
            session_factory, user_id=seed_other_user, settings=settings_gate_on
        )
        client_on.cookies.set(SESSION_COOKIE_NAME, cookie_other)
        r_del = client_on.delete(f"/api/v1/me/push-tokens/{token_id}")
        assert r_del.status_code == 204

        # Owner's row is intact + no audit row was written for the
        # cross-user no-op delete.
        with session_factory() as s, tenant_agnostic():
            row = s.get(UserPushToken, token_id)
            assert row is not None
            audit = list(
                s.scalars(
                    select(AuditLog).where(
                        AuditLog.workspace_id == _AGNOSTIC_WORKSPACE_ID,
                        AuditLog.action == "user_push_token.deleted",
                        AuditLog.entity_id == token_id,
                    )
                ).all()
            )
        assert audit == []


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


class TestAuth:
    def test_post_without_cookie_is_401(self, client_on: TestClient) -> None:
        client_on.cookies.clear()
        r = client_on.post(
            "/api/v1/me/push-tokens",
            json={"platform": "android", "token": "h"},
        )
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "session_required"

    def test_get_without_cookie_is_401(self, client_on: TestClient) -> None:
        client_on.cookies.clear()
        r = client_on.get("/api/v1/me/push-tokens")
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "session_required"

    def test_delete_without_cookie_is_401(self, client_on: TestClient) -> None:
        client_on.cookies.clear()
        r = client_on.delete("/api/v1/me/push-tokens/01HWA00000000000000000NOPE")
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "session_required"

    def test_put_without_cookie_is_401(self, client_on: TestClient) -> None:
        client_on.cookies.clear()
        r = client_on.put(
            "/api/v1/me/push-tokens/01HWA00000000000000000NOPE",
            json={},
        )
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "session_required"
