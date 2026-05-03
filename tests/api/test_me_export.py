"""Unit tests for the bare-host ``/api/v1/me/export`` router (cd-p8qc).

Exercises :mod:`app.api.v1.auth.me_export` against a minimal FastAPI
instance — no factory, no tenancy middleware, no CSRF middleware. The
shape mirrors :mod:`tests.unit.api.v1.auth.test_me_avatar` but mounts
the export surface and seeds a workspace + membership so the route's
``_select_workspace_context`` resolves a delivery context for the
``privacy_export_ready`` email.

Coverage:

* ``POST /me/export`` happy path with a wired :class:`InMemoryMailer`
  → 202, ``download_url`` populated, and one
  ``privacy_export_ready`` email sent through the mailer + one
  ``email_delivery`` row queued → sent.
* ``POST /me/export`` without a session cookie → 401.
* ``POST /me/export`` per-user 3/hour budget — the 4th call within
  the window returns 429; an isolated :class:`ShieldStore` per case
  keeps sibling tests from sharing state.
* ``GET /me/export/{id}`` — returns the caller's own export; the
  same id requested by a different user returns 404 (the route
  does not leak existence across identities).
* ``GET /me/export/{id}`` without a session cookie → 401.

See ``docs/specs/15-security-privacy.md`` §"Privacy and data rights"
and ``docs/specs/10-messaging-notifications.md`` §10.1.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.abuse.throttle import ShieldStore
from app.adapters.db.base import Base
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.identity.models import User
from app.adapters.db.messaging.models import EmailDelivery, Notification
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import db_session as db_session_dep
from app.api.deps import get_storage
from app.api.v1.auth import me_export as me_export_module
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.clock import SystemClock
from app.util.ulid import new_ulid
from tests._fakes.mailer import InMemoryMailer
from tests._fakes.storage import InMemoryStorage

# Pinned UA / Accept-Language so the :func:`validate` fingerprint
# gate agrees with the seed :func:`issue` call — matches the
# integration test's convention.
_TEST_UA: str = "pytest-me-export"
_TEST_ACCEPT_LANGUAGE: str = "en"


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` package.

    The export builder scans every "interesting" table in the metadata
    graph; any context model that lives behind a lazy import would
    otherwise be missing from ``Base.metadata`` and skipped silently.
    Mirrors the loader in :mod:`tests.unit.domain.messaging.test_email_delivery_seam`.
    """
    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    """Minimal :class:`Settings` pinned to SQLite + a fixed root key.

    The root key is load-bearing — :func:`auth_session.issue` /
    ``validate`` derive the fingerprint pepper from it, and both
    sides must agree on the value.
    """
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-me-export-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    _load_all_models()
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
def seed_user_workspace(
    session_factory: sessionmaker[Session],
) -> tuple[str, str]:
    """Seed a user + workspace + membership and return ``(user_id, workspace_id)``.

    The route's ``_select_workspace_context`` joins ``user_workspace``
    against ``workspace`` to resolve a delivery context for the
    ``privacy_export_ready`` email — both rows must exist for the
    happy-path test to fire the mailer.
    """
    now = SystemClock().now()
    user_id = new_ulid()
    workspace_id = new_ulid()
    with session_factory() as s, tenant_agnostic():
        s.add(
            Workspace(
                id=workspace_id,
                slug="export-ws",
                name="Export WS",
                plan="free",
                quota_json={},
                settings_json={},
                default_timezone="UTC",
                default_locale="en",
                default_currency="USD",
                created_at=now,
                updated_at=now,
            )
        )
        s.add(
            User(
                id=user_id,
                email=f"export-{user_id[-6:].lower()}@example.com",
                email_lower=f"export-{user_id[-6:].lower()}@example.com",
                display_name="Export User",
                created_at=now,
            )
        )
        # Flush so the workspace row exists before the membership FK
        # is checked at INSERT time. SQLite's FK enforcement is
        # immediate per-statement, not deferred — the user_workspace
        # row would otherwise fail because pysqlite serialises the
        # inserts in dependency order but the membership row's FK is
        # checked synchronously against the in-flight transaction's
        # visible state.
        s.flush()
        s.add(
            UserWorkspace(
                user_id=user_id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=now,
            )
        )
        s.commit()
    return user_id, workspace_id


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def mailer() -> InMemoryMailer:
    return InMemoryMailer()


@pytest.fixture
def rate_limit_store() -> ShieldStore:
    """Per-case :class:`ShieldStore`; sibling tests never share state."""
    return ShieldStore()


@pytest.fixture
def client(
    engine: Engine,
    settings: Settings,
    session_factory: sessionmaker[Session],
    storage: InMemoryStorage,
    mailer: InMemoryMailer,
    rate_limit_store: ShieldStore,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Mount the export router on a minimal FastAPI."""
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    app = FastAPI()
    app.include_router(
        me_export_module.build_me_export_router(
            mailer=mailer,
            rate_limit_store=rate_limit_store,
        ),
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
    app.dependency_overrides[get_storage] = lambda: storage

    with TestClient(
        app,
        base_url="https://testserver",
        headers={
            "User-Agent": _TEST_UA,
            "Accept-Language": _TEST_ACCEPT_LANGUAGE,
        },
    ) as c:
        yield c


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
            has_owner_grant=False,
            ua=_TEST_UA,
            ip="127.0.0.1",
            accept_language=_TEST_ACCEPT_LANGUAGE,
            settings=settings,
        )
        s.commit()
        return result.cookie_value


# ---------------------------------------------------------------------------
# Happy path — POST /me/export
# ---------------------------------------------------------------------------


class TestPostExportHappyPath:
    def test_post_export_returns_202_and_dispatches_email(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user_workspace: tuple[str, str],
        storage: InMemoryStorage,
        mailer: InMemoryMailer,
    ) -> None:
        """The 202 carries a poll URL + signed download URL; the
        ``privacy_export_ready`` email lands through the standard
        :class:`NotificationService` path with one ``email_delivery``
        row queued → sent.
        """
        user_id, workspace_id = seed_user_workspace
        cookie_value = _issue_cookie(
            session_factory, user_id=user_id, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        r = client.post("/api/v1/me/export")
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "completed"
        assert body["id"]
        assert body["poll_url"].endswith(f"/me/export/{body['id']}")
        assert body["download_url"] is not None
        assert body["download_url"].startswith("memory://")

        # Email was dispatched via the standard fanout path.
        assert len(mailer.sent) == 1
        sent = mailer.sent[0]
        assert "data export" in sent.subject.lower()
        # Body carries the signed download URL the route returned.
        assert body["download_url"] in sent.body_text

        # One inbox row + one email_delivery row landed in the
        # workspace pinned by ``_select_workspace_context``.
        with session_factory() as s, tenant_agnostic():
            inbox = (
                s.execute(
                    select(Notification).where(
                        Notification.recipient_user_id == user_id
                    )
                )
                .scalars()
                .all()
            )
            assert len(inbox) == 1
            assert inbox[0].kind == "privacy_export_ready"
            assert inbox[0].workspace_id == workspace_id

            deliveries = (
                s.execute(
                    select(EmailDelivery).where(EmailDelivery.to_person_id == user_id)
                )
                .scalars()
                .all()
            )
            assert len(deliveries) == 1
            row = deliveries[0]
            assert row.template_key == "privacy_export_ready"
            assert row.delivery_state == "sent"
            assert row.workspace_id == workspace_id


# ---------------------------------------------------------------------------
# Auth — POST /me/export
# ---------------------------------------------------------------------------


class TestPostExportAuth:
    def test_post_without_session_is_401(self, client: TestClient) -> None:
        client.cookies.clear()
        r = client.post("/api/v1/me/export")
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["error"] == "session_required"


# ---------------------------------------------------------------------------
# Rate limiting — POST /me/export
# ---------------------------------------------------------------------------


class TestPostExportRateLimit:
    def test_fourth_request_in_window_is_429(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user_workspace: tuple[str, str],
    ) -> None:
        """3/hour per user; the 4th call inside the window returns 429.

        Uses an isolated :class:`ShieldStore` (via the ``rate_limit_store``
        fixture) so this test never collides with a sibling case.
        """
        user_id, _ = seed_user_workspace
        cookie_value = _issue_cookie(
            session_factory, user_id=user_id, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        for _ in range(3):
            r_ok = client.post("/api/v1/me/export")
            assert r_ok.status_code == 202, r_ok.text

        r = client.post("/api/v1/me/export")
        assert r.status_code == 429, r.text
        assert r.json()["detail"]["error"] == "rate_limited"


# ---------------------------------------------------------------------------
# Polling — GET /me/export/{id}
# ---------------------------------------------------------------------------


class TestGetExport:
    def test_get_returns_callers_own_export(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user_workspace: tuple[str, str],
    ) -> None:
        user_id, _ = seed_user_workspace
        cookie_value = _issue_cookie(
            session_factory, user_id=user_id, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        post = client.post("/api/v1/me/export")
        assert post.status_code == 202, post.text
        export_id = post.json()["id"]

        r = client.get(f"/api/v1/me/export/{export_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == export_id
        assert body["status"] == "completed"
        assert body["download_url"] is not None

    def test_get_other_users_export_is_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user_workspace: tuple[str, str],
    ) -> None:
        """Requesting an export id that belongs to another user must
        return 404 — the route does not leak existence across
        identities.
        """
        user_id, workspace_id = seed_user_workspace
        # Seed a SECOND user in the same workspace and mint an export
        # under that identity. Then call GET /me/export/{id} with
        # ``user_id``'s session cookie.
        now = SystemClock().now()
        other_id = new_ulid()
        with session_factory() as s, tenant_agnostic():
            s.add(
                User(
                    id=other_id,
                    email=f"other-{other_id[-6:].lower()}@example.com",
                    email_lower=f"other-{other_id[-6:].lower()}@example.com",
                    display_name="Other User",
                    created_at=now,
                )
            )
            s.add(
                UserWorkspace(
                    user_id=other_id,
                    workspace_id=workspace_id,
                    source="workspace_grant",
                    added_at=now,
                )
            )
            s.commit()

        # Mint an export for ``other_id``.
        other_cookie = _issue_cookie(
            session_factory, user_id=other_id, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, other_cookie)
        post = client.post("/api/v1/me/export")
        assert post.status_code == 202, post.text
        other_export_id = post.json()["id"]

        # Now switch to ``user_id`` and try to read ``other_id``'s export.
        client.cookies.clear()
        cookie_value = _issue_cookie(
            session_factory, user_id=user_id, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r = client.get(f"/api/v1/me/export/{other_export_id}")
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["error"] == "export_not_found"

    def test_get_without_session_is_401(self, client: TestClient) -> None:
        client.cookies.clear()
        r = client.get("/api/v1/me/export/anything")
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["error"] == "session_required"


# ---------------------------------------------------------------------------
# Pruning helpers — keep linters quiet without dropping the imports.
# ---------------------------------------------------------------------------


# ``SessionRow`` is imported solely to keep the metadata graph aware of
# the ``session`` table — the export builder scans it. Suppress F401 by
# re-exporting the symbol; we don't want to add a noqa to every line.
__all__ = ["SessionRow"]
