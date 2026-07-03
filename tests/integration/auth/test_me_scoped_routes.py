"""Integration test — PAT-reachable ``/w/<slug>/api/v1/me`` data routes (cd-fktzw).

Drives the real :class:`WorkspaceContextMiddleware` in front of the real
:func:`app.api.v1.me_data.build_me_data_router` router, so the assertions
prove the end-to-end PAT surface §03 "Personal access tokens" documents:

* a PAT with ``me.tasks:read`` reads **only its own subject's** tasks
  (``GET /me/tasks`` → 200; another member's task never appears);
* a PAT cannot exceed its ``me.*`` scope — ``me.tasks:read`` 403s on
  ``GET`` / ``POST /me/expenses`` with the ``insufficient_scope``
  envelope + ``WWW-Authenticate`` challenge (§03 "Usage");
* the right scope admits the route — a ``me.expenses:read`` PAT reaches
  ``GET /me/expenses`` (200); a ``me.profile:read`` PAT reaches
  ``GET /me/profile`` (200, its own row).

The bare-host cookie ``/me`` surfaces are untouched by this task; the
scope gate's no-op for session principals is pinned by the unit test
``tests/unit/authz/test_require_me_scope.py``.

Mirrors the fixture + seed + sweep harness of
``test_personal_token_confinement.py`` (the sibling cd-yhya8 test).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.tasks.models import Occurrence
from app.api.errors import add_exception_handlers
from app.api.v1.me_data import build_me_data_router
from app.auth.tokens import mint as mint_token
from app.config import Settings
from app.tenancy.middleware import WorkspaceContextMiddleware
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.integration.auth._cleanup import delete_api_tokens_for_scope

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_SLUG = "me-scoped"


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-me-scoped-routes-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
        phase0_stub_enabled=False,
    )


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture
def wire_default_uow(
    engine: Engine,
    session_factory: sessionmaker[Session],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Redirect ``make_uow`` and the middleware's settings to the test fixtures."""
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = session_factory
    monkeypatch.setattr("app.tenancy.middleware.get_settings", lambda: settings)
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


def _build_app() -> FastAPI:
    """Real middleware in front of the real ``/me`` data router."""
    app = FastAPI()
    app.add_middleware(WorkspaceContextMiddleware)
    app.include_router(build_me_data_router(), prefix="/w/{slug}/api/v1")
    # RFC 7807 envelope wiring so a route-level ``InsufficientScope`` /
    # domain error renders as ``application/problem+json`` (§12), matching
    # production; without it FastAPI would emit a plain ``{"detail": ...}``.
    add_exception_handlers(app)
    return app


def _seed_two_members(
    session_factory: sessionmaker[Session],
) -> tuple[str, str, str, str]:
    """Seed a workspace with an owner + two worker members (Alice, Bob).

    Returns ``(workspace_id, owner_id, alice_id, bob_id)``. Each worker
    holds a live ``worker`` grant + a reconciled ``user_workspace`` row so
    their PATs resolve a live ctx.
    """
    from app.adapters.db.authz.models import RoleGrant
    from app.domain.identity.user_workspace_refresh import reconcile_user_workspace

    with session_factory() as s:
        owner = bootstrap_user(
            s, email="owner-me-scoped@example.com", display_name="Owner"
        )
        alice = bootstrap_user(
            s, email="alice-me-scoped@example.com", display_name="Alice"
        )
        bob = bootstrap_user(s, email="bob-me-scoped@example.com", display_name="Bob")
        ws = bootstrap_workspace(
            s, slug=_SLUG, name="Me Scoped", owner_user_id=owner.id
        )
        for member_id in (alice.id, bob.id):
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws.id,
                    user_id=member_id,
                    grant_role="worker",
                    scope_property_id=None,
                    created_at=_PINNED,
                    created_by_user_id=None,
                )
            )
        s.commit()
        ws_id, owner_id, alice_id, bob_id = ws.id, owner.id, alice.id, bob.id
    with session_factory() as s:
        reconcile_user_workspace(s, now=_PINNED)
        s.commit()
    return ws_id, owner_id, alice_id, bob_id


def _seed_task(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    assignee_user_id: str,
    title: str,
) -> str:
    """Insert one ad-hoc, non-personal occurrence assigned to ``assignee``."""
    task_id = new_ulid()
    with session_factory() as s:
        s.add(
            Occurrence(
                id=task_id,
                workspace_id=workspace_id,
                assignee_user_id=assignee_user_id,
                title=title,
                state="pending",
                is_personal=False,
                starts_at=_PINNED,
                ends_at=_PINNED + timedelta(hours=1),
                created_at=_PINNED,
            )
        )
        s.commit()
    return task_id


def _mint_pat(
    session_factory: sessionmaker[Session],
    *,
    subject_user_id: str,
    label: str,
    scopes: dict[str, bool],
) -> str:
    """Mint a personal access token for ``subject_user_id`` with ``scopes``."""
    with session_factory() as s:
        minted = mint_token(
            s,
            None,
            user_id=subject_user_id,
            label=label,
            scopes=scopes,
            expires_at=None,
            kind="personal",
            subject_user_id=subject_user_id,
            now=_PINNED,
        )
        s.commit()
        return minted.token


def _sweep(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_ids: tuple[str, ...],
) -> None:
    """Delete every row this test landed."""
    from sqlalchemy import select

    from app.adapters.db.audit.models import AuditLog
    from app.adapters.db.authz.models import (
        PermissionGroup,
        PermissionGroupMember,
        RoleGrant,
    )
    from app.adapters.db.identity.models import User
    from app.adapters.db.workspace.models import UserWorkspace, Workspace
    from app.tenancy import tenant_agnostic

    with session_factory() as s, tenant_agnostic():
        delete_api_tokens_for_scope(s, workspace_ids=(workspace_id,), user_ids=user_ids)
        for row in s.scalars(
            select(Occurrence).where(Occurrence.workspace_id == workspace_id)
        ).all():
            s.delete(row)
        for model in (RoleGrant, PermissionGroupMember, PermissionGroup, UserWorkspace):
            for row in s.scalars(
                select(model).where(model.workspace_id == workspace_id)
            ).all():
                s.delete(row)
        for audit in s.scalars(
            select(AuditLog).where(AuditLog.workspace_id == workspace_id)
        ).all():
            s.delete(audit)
        ws = s.get(Workspace, workspace_id)
        if ws is not None:
            s.delete(ws)
        for user_id in user_ids:
            user_row = s.get(User, user_id)
            if user_row is not None:
                s.delete(user_row)
        s.commit()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestMeScopedRoutes:
    """A PAT exercises exactly the ``me.*`` scopes it was minted with."""

    def test_me_data_routes(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        ws_id, owner_id, alice_id, bob_id = _seed_two_members(session_factory)
        alice_task = _seed_task(
            session_factory,
            workspace_id=ws_id,
            assignee_user_id=alice_id,
            title="Alice task",
        )
        bob_task = _seed_task(
            session_factory,
            workspace_id=ws_id,
            assignee_user_id=bob_id,
            title="Bob task",
        )
        try:
            tasks_pat = _mint_pat(
                session_factory,
                subject_user_id=alice_id,
                label="alice-tasks",
                scopes={"me.tasks:read": True},
            )
            expenses_pat = _mint_pat(
                session_factory,
                subject_user_id=alice_id,
                label="alice-expenses",
                scopes={"me.expenses:read": True},
            )
            profile_pat = _mint_pat(
                session_factory,
                subject_user_id=alice_id,
                label="alice-profile",
                scopes={"me.profile:read": True},
            )

            app = _build_app()
            with TestClient(app, raise_server_exceptions=False) as client:
                self._assert_reads_only_own_tasks(
                    client, tasks_pat, own=alice_task, other=bob_task
                )
                self._assert_scope_exceeded_on_expenses(client, tasks_pat)
                self._assert_expenses_scope_admits_list(client, expenses_pat)
                self._assert_profile_scope_reads_own(
                    client, profile_pat, subject_id=alice_id
                )
                self._assert_profile_scope_cannot_read_tasks(client, profile_pat)
        finally:
            _sweep(
                session_factory,
                workspace_id=ws_id,
                user_ids=(owner_id, alice_id, bob_id),
            )

    def _assert_reads_only_own_tasks(
        self, client: TestClient, pat: str, *, own: str, other: str
    ) -> None:
        r = client.get(f"/w/{_SLUG}/api/v1/me/tasks", headers=_bearer(pat))
        assert r.status_code == 200, r.text
        ids = {row["id"] for row in r.json()["data"]}
        assert own in ids
        # The structural self-key: another subject's task never surfaces.
        assert other not in ids

    def _assert_scope_exceeded_on_expenses(self, client: TestClient, pat: str) -> None:
        # A me.tasks:read PAT cannot reach the expenses surface at all. The
        # GET gate names me.expenses:read; the POST gate names the :write it
        # demands — proving read cannot masquerade as write.
        for method, want_scope in (
            ("get", "me.expenses:read"),
            ("post", "me.expenses:write"),
        ):
            call = getattr(client, method)
            kwargs: dict[str, Any] = {"headers": _bearer(pat)}
            if method == "post":
                kwargs["json"] = {}
            r = call(f"/w/{_SLUG}/api/v1/me/expenses", **kwargs)
            assert r.status_code == 403, (method, r.text)
            assert r.headers["content-type"].startswith("application/problem+json")
            challenge = r.headers["WWW-Authenticate"]
            # RFC 6750 challenge names the scope the PAT lacks (§03 "Usage").
            assert 'error="insufficient_scope"' in challenge
            assert f'scope="{want_scope}"' in challenge
            body = r.json()
            # Route-level gate maps through HTTPException, so ``type`` is the
            # status-derived ``forbidden`` while ``error`` carries the precise
            # discriminator agents switch on (matches the scoped-token gate).
            assert body["error"] == "insufficient_scope"
            assert body["scope"] == want_scope

    def _assert_expenses_scope_admits_list(self, client: TestClient, pat: str) -> None:
        # The matching scope admits the route — no claims seeded, so empty.
        r = client.get(f"/w/{_SLUG}/api/v1/me/expenses", headers=_bearer(pat))
        assert r.status_code == 200, r.text
        assert r.json()["data"] == []

    def _assert_profile_scope_reads_own(
        self, client: TestClient, pat: str, *, subject_id: str
    ) -> None:
        r = client.get(f"/w/{_SLUG}/api/v1/me/profile", headers=_bearer(pat))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == subject_id
        assert body["display_name"] == "Alice"

    def _assert_profile_scope_cannot_read_tasks(
        self, client: TestClient, pat: str
    ) -> None:
        # me.profile:read is not me.tasks:read — the gate refuses.
        r = client.get(f"/w/{_SLUG}/api/v1/me/tasks", headers=_bearer(pat))
        assert r.status_code == 403, r.text
        assert 'error="insufficient_scope"' in r.headers["WWW-Authenticate"]
