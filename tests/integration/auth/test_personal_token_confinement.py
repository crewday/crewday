"""Integration test — personal access tokens are confined to ``/me`` + their subject.

§03 "Personal access tokens": "a PAT can only read/write the subject's
own rows (the ``me:*`` filter is applied at query time regardless of
scope string)" and its ``me:*`` scopes "imply nothing outside
``me:*``". Enforced centrally in
:meth:`app.tenancy.middleware.WorkspaceContextMiddleware.dispatch`:

* A PAT (``token_kind == "personal"``) that reaches any workspace route
  **outside** the ``/me`` self-service subtree is refused with 403
  ``insufficient_scope`` (§03 "Usage") — before the handler runs, so a
  route with no permission gate of its own (e.g. ``GET /tasks``) cannot
  leak workspace rows to the PAT's manager-graded subject.
* On the ``/me`` subtree the PAT is admitted, and every such route keys
  on ``ctx.actor_id`` — which the middleware pins to the token's
  ``subject_user_id`` (mint sets ``user_id == subject_user_id``). That
  pinning is the structural subject-row filter: a PAT can only ever see
  its own subject, never another user's.

Only personal tokens trip the gate; scoped tokens (and sessions /
delegated / demo / system principals) fall through unchanged — the
contrast test at the bottom pins that so a future refactor that
conflates the surfaces fails loudly.

Drives the real :class:`WorkspaceContextMiddleware` plus two toy
routes with **no** permission gate of their own, so the assertions
prove the *middleware* is the enforcement point.

See ``docs/specs/03-auth-and-tokens.md`` §"Personal access tokens" /
§"Scopes" (esp. lines 855, 962-964) and §"Usage".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.api.deps import current_workspace_context
from app.auth.tokens import mint as mint_token
from app.config import Settings
from app.tenancy import WorkspaceContext
from app.tenancy.middleware import WorkspaceContextMiddleware
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.integration.auth._cleanup import delete_api_tokens_for_scope

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_PROBLEM_TYPE_BASE = "https://crewday.dev/errors/"


# ---------------------------------------------------------------------------
# Fixtures — a real middleware-wired FastAPI app + a tenant-aware factory
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-personal-token-confinement-root-key"),
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
    """FastAPI app with the real middleware + two UNGATED routes.

    Neither route declares a ``Permission`` dependency — the point is
    that the *middleware* confines the PAT. ``/me/whoami`` echoes
    ``ctx.actor_id`` (the structural subject id); ``/tasks`` stands in
    for any non-``/me`` workspace read that has no gate of its own and
    would otherwise hand its whole payload to whoever resolves a ctx.
    """
    app = FastAPI()
    app.add_middleware(WorkspaceContextMiddleware)

    @app.get("/w/{slug}/api/v1/me/whoami")
    def whoami(
        ctx: Annotated[WorkspaceContext, Depends(current_workspace_context)],
    ) -> dict[str, str]:
        # A real ``/me`` route keys every query on ``ctx.actor_id``; we
        # echo it so the test can prove the PAT is pinned to its subject.
        return {"actor_id": ctx.actor_id}

    @app.get("/w/{slug}/api/v1/tasks")
    def list_tasks(
        ctx: Annotated[WorkspaceContext, Depends(current_workspace_context)],
    ) -> dict[str, str]:
        # Stands in for a non-``/me`` workspace route with no gate — a PAT
        # must never reach it (the middleware 403s first).
        return {"leaked": "all-workspace-tasks"}

    return app


def _seed_member(
    session_factory: sessionmaker[Session],
    *,
    slug: str,
    owner_email: str,
    member_email: str,
    member_name: str,
) -> tuple[str, str, str]:
    """Seed a workspace with an owner + a worker member holding a live grant.

    Returns ``(workspace_id, owner_user_id, member_user_id)``. The member
    holds a live ``worker`` role grant and a ``user_workspace`` row, so a
    PAT for them both verifies (``has_live_grants_anywhere``) and resolves
    a live ctx (workspace membership).
    """
    from app.adapters.db.authz.models import RoleGrant
    from app.domain.identity.user_workspace_refresh import reconcile_user_workspace

    with session_factory() as s:
        owner = bootstrap_user(s, email=owner_email, display_name="Owner")
        member = bootstrap_user(s, email=member_email, display_name=member_name)
        ws = bootstrap_workspace(
            s, slug=slug, name=slug.title(), owner_user_id=owner.id
        )
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=ws.id,
                user_id=member.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        s.commit()
        ws_id, owner_id, member_id = ws.id, owner.id, member.id
    with session_factory() as s:
        reconcile_user_workspace(s, now=_PINNED)
        s.commit()
    return ws_id, owner_id, member_id


def _mint_pat(
    session_factory: sessionmaker[Session],
    *,
    subject_user_id: str,
    label: str,
) -> str:
    """Mint a personal access token for ``subject_user_id`` (me:* scope)."""
    with session_factory() as s:
        minted = mint_token(
            s,
            None,
            user_id=subject_user_id,
            label=label,
            scopes={"me.tasks:read": True},
            expires_at=None,
            kind="personal",
            subject_user_id=subject_user_id,
            now=_PINNED,
        )
        s.commit()
        return minted.token


def _mint_scoped(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    slug: str,
    subject_user_id: str,
) -> str:
    """Mint a workspace scoped token (``tasks:read``) for the contrast test."""
    with session_factory() as s:
        ctx = WorkspaceContext(
            workspace_id=workspace_id,
            workspace_slug=slug,
            actor_id=subject_user_id,
            actor_kind="user",
            actor_grant_role="worker",
            actor_was_owner_member=False,
            audit_correlation_id=new_ulid(),
        )
        minted = mint_token(
            s,
            ctx,
            user_id=subject_user_id,
            label="scoped",
            scopes={"tasks:read": True},
            expires_at=None,
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
    """Delete every row this test landed (mirrors the delegated-auth sweep)."""
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
        # api_token deleted first — its ``api_token_request_log`` children
        # cascade (ON DELETE CASCADE) and the PAT rows carry a NULL
        # workspace_id, so we sweep them by subject via ``user_ids``.
        delete_api_tokens_for_scope(s, workspace_ids=(workspace_id,), user_ids=user_ids)
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


class TestPersonalTokenConfinement:
    """A PAT is pinned to the ``/me`` subtree and to its own subject."""

    def test_pat_confined_to_me_subtree_and_subject(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        ws_id, owner_id, alice_id = _seed_member(
            session_factory,
            slug="pat-confine",
            owner_email="owner-pat-confine@example.com",
            member_email="alice-pat-confine@example.com",
            member_name="Alice",
        )
        # A second member so we can prove a PAT is pinned to *its* subject.
        from app.adapters.db.authz.models import RoleGrant
        from app.domain.identity.user_workspace_refresh import (
            reconcile_user_workspace,
        )

        with session_factory() as s:
            bob = bootstrap_user(
                s, email="bob-pat-confine@example.com", display_name="Bob"
            )
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=bob.id,
                    grant_role="worker",
                    scope_property_id=None,
                    created_at=_PINNED,
                    created_by_user_id=None,
                )
            )
            s.commit()
            bob_id = bob.id
        with session_factory() as s:
            reconcile_user_workspace(s, now=_PINNED)
            s.commit()

        try:
            alice_pat = _mint_pat(
                session_factory, subject_user_id=alice_id, label="alice-printer"
            )
            bob_pat = _mint_pat(
                session_factory, subject_user_id=bob_id, label="bob-printer"
            )
            scoped = _mint_scoped(
                session_factory,
                workspace_id=ws_id,
                slug="pat-confine",
                subject_user_id=alice_id,
            )

            app = _build_app()
            with TestClient(app, raise_server_exceptions=False) as client:
                self._assert_pat_admitted_on_me(client, alice_pat, subject=alice_id)
                self._assert_pat_pinned_to_own_subject(client, bob_pat, subject=bob_id)
                self._assert_pat_refused_off_me(client, alice_pat)
                self._assert_scoped_token_not_confined(client, scoped)
        finally:
            _sweep(
                session_factory,
                workspace_id=ws_id,
                user_ids=(owner_id, alice_id, bob_id),
            )

    def _assert_pat_admitted_on_me(
        self, client: TestClient, pat: str, *, subject: str
    ) -> None:
        """A PAT reads its own rows on a ``/me`` route (actor_id == subject)."""
        r = client.get(
            "/w/pat-confine/api/v1/me/whoami",
            headers={"Authorization": f"Bearer {pat}"},
        )
        assert r.status_code == 200, r.text
        # The structural subject-row filter: the ctx the route keys on is
        # pinned to the token's subject, never anyone else.
        assert r.json() == {"actor_id": subject}

    def _assert_pat_pinned_to_own_subject(
        self, client: TestClient, pat: str, *, subject: str
    ) -> None:
        """Bob's PAT resolves to Bob — a PAT can never surface another user."""
        r = client.get(
            "/w/pat-confine/api/v1/me/whoami",
            headers={"Authorization": f"Bearer {pat}"},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"actor_id": subject}

    def _assert_pat_refused_off_me(self, client: TestClient, pat: str) -> None:
        """A PAT off the ``/me`` subtree is refused 403 before the handler runs."""
        r = client.get(
            "/w/pat-confine/api/v1/tasks",
            headers={"Authorization": f"Bearer {pat}"},
        )
        assert r.status_code == 403, r.text
        assert r.headers["content-type"].startswith("application/problem+json")
        assert 'error="insufficient_scope"' in r.headers["WWW-Authenticate"]
        body = r.json()
        # cd-isllv: canonical 403 envelope — the §12-registered ``forbidden``
        # type with ``insufficient_scope`` as the machine ``error`` code, so
        # the middleware off-subtree refusal matches the route-level scope gate.
        assert body["type"] == f"{_PROBLEM_TYPE_BASE}forbidden"
        assert body["error"] == "insufficient_scope"
        # The handler never ran — no workspace payload leaked to the PAT.
        assert "leaked" not in body

    def _assert_scoped_token_not_confined(
        self, client: TestClient, scoped: str
    ) -> None:
        """Regression: a scoped token is NOT confined by the PAT gate.

        The confinement keys on ``token_kind == "personal"``; a scoped
        token (``token_kind == "scoped"``) reaches the ungated ``/tasks``
        route unchanged. Sessions / delegated / demo / system principals
        all leave ``token_kind`` unset and are equally unaffected.
        """
        r = client.get(
            "/w/pat-confine/api/v1/tasks",
            headers={"Authorization": f"Bearer {scoped}"},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"leaked": "all-workspace-tasks"}
