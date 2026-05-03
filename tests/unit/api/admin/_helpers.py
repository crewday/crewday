"""Shared fixtures + factory helpers for the cd-jlms admin unit tests.

The cd-jlms admin surface ships six families (workspaces, signup,
settings, admins, audit, usage); each ships its own
``test_<family>.py`` so failures stay narrow. Without a shared
helper module each test file would re-implement the same
in-memory engine + admin-grant fixtures, duplicating the cd-yj4k
test_me.py shape five times. This module is the cd-jlms
extraction — it carries every helper the family tests share, in
the same shape ``test_me.py`` declared inline so a future merge
back into ``test_me.py`` (when the helpers stabilise) is a
straight refactor.

Only used by sibling tests. Not imported from production code.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

# Eagerly load the ORM packages the production factory pulls so
# ``Base.metadata.create_all`` below stays consistent regardless
# of pytest collection order — same shape ``test_me.py`` follows.
import app.adapters.db.payroll.models
import app.adapters.db.places.models
import app.adapters.db.secrets.models  # noqa: F401
from app.adapters.db.authz.models import DeploymentOwner, RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.api.admin import admin_router
from app.api.deps import db_session as db_session_dep
from app.api.errors import add_exception_handlers
from app.api.transport.admin_sse import router as admin_sse_router
from app.auth.session import issue
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

__all__ = [
    "PINNED",
    "TEST_ACCEPT_LANGUAGE",
    "TEST_UA",
    "build_client",
    "engine_fixture",
    "grant_deployment_admin",
    "grant_deployment_owner",
    "install_admin_cookie",
    "issue_session",
    "seed_admin",
    "seed_user",
    "seed_workspace",
    "settings_fixture",
]


PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
TEST_UA = "pytest-admin-cd-jlms"
TEST_ACCEPT_LANGUAGE = "en"


def settings_fixture(label: str) -> Settings:
    """Return a pinned :class:`Settings` for the given test module.

    ``label`` is folded into the root key so two test modules
    running in the same process don't share session pepper bytes
    by accident. Mirrors the cd-yj4k pattern of one fixture per
    test module — the cd-jlms tests share this builder so the
    pinned shape stays in lockstep.
    """
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr(f"unit-test-admin-{label}-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


def engine_fixture() -> Iterator[Engine]:
    """Yield a fresh in-memory SQLite engine with the full schema."""
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def build_client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Build a :class:`TestClient` mounting :data:`admin_router`.

    Mirrors the cd-yj4k ``client`` fixture verbatim so the wire
    shape (cookies, headers, exception handlers) stays in lockstep
    across families. Patches :func:`app.auth.session.get_settings`
    so the seed-side and validate-side peppers match.
    """
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    app = FastAPI()
    app.state.settings = settings
    app.include_router(admin_sse_router, prefix="/admin")
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
            "User-Agent": TEST_UA,
            "Accept-Language": TEST_ACCEPT_LANGUAGE,
        },
    ) as c:
        yield c


def seed_user(session: Session, *, email: str, display_name: str) -> str:
    """Insert a :class:`User` row; return its id."""
    user = bootstrap_user(session, email=email, display_name=display_name)
    return user.id


def seed_workspace(
    session: Session,
    *,
    slug: str,
    name: str | None = None,
    plan: str = "free",
    quota_json: dict[str, object] | None = None,
    settings_json: dict[str, object] | None = None,
    created_at: datetime | None = None,
) -> str:
    """Insert a :class:`Workspace` row; return its id."""
    workspace_id = new_ulid()
    with tenant_agnostic():
        session.add(
            Workspace(
                id=workspace_id,
                slug=slug,
                name=name or slug.title(),
                plan=plan,
                quota_json=dict(quota_json or {}),
                settings_json=dict(settings_json or {}),
                created_at=created_at or PINNED,
            )
        )
        session.flush()
    return workspace_id


def grant_deployment_admin(
    session: Session,
    *,
    user_id: str,
    created_at: datetime | None = None,
    created_by_user_id: str | None = None,
    grant_role: str = "manager",
) -> str:
    """Plant a deployment-scope :class:`RoleGrant`; return its id."""
    grant_id = new_ulid()
    with tenant_agnostic():
        session.add(
            RoleGrant(
                id=grant_id,
                workspace_id=None,
                user_id=user_id,
                grant_role=grant_role,
                scope_kind="deployment",
                created_at=created_at or PINNED,
                created_by_user_id=created_by_user_id,
            )
        )
        session.flush()
    return grant_id


def grant_deployment_owner(
    session: Session,
    *,
    user_id: str,
    added_at: datetime | None = None,
    added_by_user_id: str | None = None,
) -> None:
    """Plant a deployment-owner membership row."""
    with tenant_agnostic():
        session.add(
            DeploymentOwner(
                user_id=user_id,
                added_at=added_at or PINNED,
                added_by_user_id=added_by_user_id,
            )
        )
        session.flush()


def issue_session(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    settings: Settings,
) -> str:
    """Issue a :class:`Session` row; return the cookie value."""
    with session_factory() as s:
        result = issue(
            s,
            user_id=user_id,
            has_owner_grant=False,
            ua=TEST_UA,
            ip="127.0.0.1",
            accept_language=TEST_ACCEPT_LANGUAGE,
            settings=settings,
        )
        s.commit()
        return result.cookie_value


def seed_admin(
    session_factory: sessionmaker[Session],
    *,
    settings: Settings,
    email: str = "ada@example.com",
    display_name: str = "Ada",
    owner: bool = False,
) -> tuple[str, str]:
    """Seed an admin user + grant; return ``(user_id, cookie_value)``.

    Bundles the recurring "make me an admin and a session" pair
    every cd-jlms test repeats. Keeps each test's setup narrow
    enough to fit in 88 columns even after the cookie install.

    Idempotent on ``email``: when the integration suite shares a DB
    across tests in the same xdist worker, re-seeding the same admin
    must not trip the ``user.email_lower`` UNIQUE constraint.
    """
    with session_factory() as s, tenant_agnostic():
        existing = s.scalar(
            select(User).where(User.email_lower == canonicalise_email(email))
        )
        if existing is not None:
            user_id = existing.id
        else:
            user_id = seed_user(s, email=email, display_name=display_name)
        existing_grant = s.scalar(
            select(RoleGrant).where(
                RoleGrant.user_id == user_id,
                RoleGrant.scope_kind == "deployment",
            )
        )
        if existing_grant is None:
            grant_deployment_admin(s, user_id=user_id)
        existing_owner = s.scalar(
            select(DeploymentOwner).where(DeploymentOwner.user_id == user_id)
        )
        if owner and existing_owner is None:
            grant_deployment_owner(s, user_id=user_id)
        s.commit()
    cookie = issue_session(session_factory, user_id=user_id, settings=settings)
    return user_id, cookie


def install_admin_cookie(
    client: TestClient,
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> str:
    """Seed an admin + install their session cookie on ``client``.

    Convenience wrapper around :func:`seed_admin`; returns the
    user id so tests that need it can pin assertions to the
    actor. Mirrors the cd-yj4k cookie-install boilerplate without
    re-spelling it in every test body.
    """
    from app.auth.session import SESSION_COOKIE_NAME

    user_id, cookie = seed_admin(session_factory, settings=settings)
    client.cookies.set(SESSION_COOKIE_NAME, cookie)
    return user_id
