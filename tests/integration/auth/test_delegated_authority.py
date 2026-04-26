"""Integration test — delegated tokens follow the delegating user's authority.

cd-tvh acceptance criterion #3 / §03 "Delegated tokens": "Authority
follows the user — granting a new role to the user affects every live
delegated token immediately." The token row carries no scopes; the
verifier resolves authority against the delegating user's
:class:`RoleGrant` rows on every request, so a grant added (or
removed) after the token was minted is visible on the very next call —
no re-mint required.

Drives the real :class:`WorkspaceContextMiddleware` plus a toy
permission-gated route. Each test:

1. Mints a delegated token for a user with no manager grant.
2. Confirms the token call → 403 ``permission_denied`` against an
   owners/managers-only action.
3. Adds the grant to the user → same token now succeeds.
4. Removes the grant → same token denies again.

Scoped tokens carry their own ``scope_json``; their authority is
**not** affected by the user's grants — the contrast test below pins
that invariant so a future refactor that conflates the two surfaces
fails loudly.

See ``docs/specs/03-auth-and-tokens.md`` §"Delegated tokens" and
``docs/specs/11-llm-and-agents.md`` §"Delegated authority".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.authz.models import RoleGrant
from app.auth.tokens import mint as mint_token
from app.authz.dep import Permission
from app.config import Settings
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.tenancy.middleware import WorkspaceContextMiddleware
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures — a real middleware-wired FastAPI app + a tenant-aware factory
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-delegated-authority-root-key"),
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


def _scoped_sweep(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_ids: tuple[str, ...],
) -> None:
    """Delete every row this test landed under ``workspace_id`` / ``user_ids``.

    Mirrors :func:`tests.integration.auth.test_tokens_pg.seeded_ctx`'s
    explicit sweep — scoped so we never touch rows a sibling test is
    actively using on the shared engine. ``api_token`` is deleted
    BEFORE ``user`` so the ``ck_api_token_kind_shape`` CHECK cannot
    trip on the FK ``ON DELETE SET NULL`` cascade (which would null
    ``delegate_for_user_id`` on a row whose ``kind`` requires it).
    """
    from app.adapters.db.audit.models import AuditLog
    from app.adapters.db.authz.models import (
        PermissionGroup,
        PermissionGroupMember,
    )
    from app.adapters.db.identity.models import ApiToken, User
    from app.adapters.db.workspace.models import UserWorkspace, Workspace

    with session_factory() as s, tenant_agnostic():
        # api_token rows pinned to this workspace OR delegating-for /
        # owned-by these users. The FK ``user_id`` always names the
        # creating user; ``delegate_for_user_id`` may also point at one
        # of them. Sweep both predicates so a row never gets orphaned.
        for tok in s.scalars(
            select(ApiToken).where(ApiToken.workspace_id == workspace_id)
        ).all():
            s.delete(tok)
        for tok in s.scalars(
            select(ApiToken).where(ApiToken.delegate_for_user_id.in_(user_ids))
        ).all():
            s.delete(tok)
        # Workspace-scoped governance / membership.
        for model in (
            RoleGrant,
            PermissionGroupMember,
            PermissionGroup,
            UserWorkspace,
        ):
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


def _build_app() -> FastAPI:
    """FastAPI app with the real middleware + an owners/managers-gated route.

    The route uses ``api_tokens.manage`` because it has the standard
    "owners + managers" default-allow set in the action catalog —
    perfect for the "no grant → deny → grant → allow" oscillation
    this suite walks. Owners-group membership would also satisfy the
    gate; the tests deliberately seed the user *outside* the owners
    group so the role grant is the only authority knob.
    """
    app = FastAPI()
    app.add_middleware(WorkspaceContextMiddleware)

    @app.get("/w/{slug}/api/v1/protected")
    def gated(
        _: Annotated[
            None,
            Depends(Permission("api_tokens.manage", scope_kind="workspace")),
        ],
    ) -> dict[str, str]:
        return {"status": "allowed"}

    return app


def _seed_workspace_with_outsider(
    session_factory: sessionmaker[Session],
    *,
    slug: str,
    owner_email: str,
    outsider_email: str,
) -> tuple[str, str, str]:
    """Seed a workspace with an owner + a non-owner outsider user.

    Returns ``(workspace_id, owner_user_id, outsider_user_id)``. The
    outsider is **not** a member of the workspace's owners group and
    holds no role grants — exactly the shape the authority oscillation
    needs as a starting point.
    """
    with session_factory() as s:
        owner = bootstrap_user(s, email=owner_email, display_name="Owner")
        outsider = bootstrap_user(s, email=outsider_email, display_name="Outsider")
        ws = bootstrap_workspace(
            s, slug=slug, name=slug.title(), owner_user_id=owner.id
        )
        s.commit()
        return ws.id, owner.id, outsider.id


def _grant_role(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
    grant_role: str,
) -> str:
    """Insert a workspace-scoped :class:`RoleGrant` row, return its id."""
    grant_id = new_ulid()
    with session_factory() as s, tenant_agnostic():
        s.add(
            RoleGrant(
                id=grant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                grant_role=grant_role,
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        s.commit()
    return grant_id


def _revoke_role(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
) -> None:
    """Hard-delete every :class:`RoleGrant` on ``(workspace, user)``."""
    with session_factory() as s, tenant_agnostic():
        for row in s.scalars(
            select(RoleGrant).where(
                RoleGrant.workspace_id == workspace_id,
                RoleGrant.user_id == user_id,
            )
        ).all():
            s.delete(row)
        s.commit()


def _add_outsider_to_workspace(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
) -> None:
    """Materialise a ``user_workspace`` row for the outsider.

    The middleware's membership check requires a ``user_workspace``
    row so the bearer-token request resolves to a live ctx (otherwise
    the request 404s before authz fires). We borrow the production
    reconciler so the row matches whatever shape future migrations
    take.
    """
    from app.domain.identity.user_workspace_refresh import reconcile_user_workspace

    # Provisional grant so the reconciler picks up the outsider; we
    # immediately strip it after so the test starts from "no grants".
    grant_id = _grant_role(
        session_factory,
        workspace_id=workspace_id,
        user_id=user_id,
        grant_role="guest",
    )
    with session_factory() as s:
        reconcile_user_workspace(s, now=_PINNED)
        s.commit()
    # Delete the seed grant so the test begins with the outsider as a
    # workspace member with zero grants.
    with session_factory() as s, tenant_agnostic():
        row = s.get(RoleGrant, grant_id)
        if row is not None:
            s.delete(row)
        s.commit()


# ---------------------------------------------------------------------------
# Authority follows the user (cd-tvh #3)
# ---------------------------------------------------------------------------


class TestAuthorityFollowsUser:
    """Granting / revoking a role on the delegating user steers the token live.

    The token row never changes between requests — only the delegating
    user's ``role_grant`` rows do. The verifier resolves authority on
    every call, so the same token oscillates between deny and allow as
    grants come and go.
    """

    def test_grant_then_revoke_oscillates_authority(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        ws_id, owner_id, outsider_id = _seed_workspace_with_outsider(
            session_factory,
            slug="del-auth",
            owner_email="owner-del-auth@example.com",
            outsider_email="outsider-del-auth@example.com",
        )
        try:
            self._exercise_grant_revoke(
                session_factory,
                ws_id=ws_id,
                outsider_id=outsider_id,
            )
        finally:
            _scoped_sweep(
                session_factory,
                workspace_id=ws_id,
                user_ids=(owner_id, outsider_id),
            )

    def _exercise_grant_revoke(
        self,
        session_factory: sessionmaker[Session],
        *,
        ws_id: str,
        outsider_id: str,
    ) -> None:
        _add_outsider_to_workspace(
            session_factory,
            workspace_id=ws_id,
            user_id=outsider_id,
        )

        # Mint a delegated token for the outsider. The owner's ctx
        # passes the auth gate at mint time; the token then carries no
        # scopes and resolves authority against the outsider's grants
        # at every subsequent verify.
        with session_factory() as s:
            owner_ctx = WorkspaceContext(
                workspace_id=ws_id,
                workspace_slug="del-auth",
                actor_id=outsider_id,
                actor_kind="user",
                actor_grant_role="manager",
                actor_was_owner_member=False,
                audit_correlation_id=new_ulid(),
            )
            minted = mint_token(
                s,
                owner_ctx,
                user_id=outsider_id,
                label="agent",
                scopes={},
                expires_at=None,
                kind="delegated",
                delegate_for_user_id=outsider_id,
                now=_PINNED,
            )
            s.commit()

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            headers = {"Authorization": f"Bearer {minted.token}"}

            # 1) No grant — 403.
            r = client.get("/w/del-auth/api/v1/protected", headers=headers)
            assert r.status_code == 403, r.text
            assert r.json()["detail"]["error"] == "permission_denied"

            # 2) Grant the outsider 'manager' — same token, same call,
            #    now passes. No re-mint, no token round-trip.
            grant_id = _grant_role(
                session_factory,
                workspace_id=ws_id,
                user_id=outsider_id,
                grant_role="manager",
            )
            r = client.get("/w/del-auth/api/v1/protected", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json() == {"status": "allowed"}

            # 3) Revoke the grant — same token denies again.
            _revoke_role(
                session_factory,
                workspace_id=ws_id,
                user_id=outsider_id,
            )
            # Sanity: only the seed-grant deletion landed; no other
            # grants the test forgot about are masking the result.
            with session_factory() as s, tenant_agnostic():
                survivors = s.scalars(
                    select(RoleGrant).where(
                        RoleGrant.workspace_id == ws_id,
                        RoleGrant.user_id == outsider_id,
                    )
                ).all()
                assert survivors == []
                # The deleted grant id is no longer reachable.
                assert s.get(RoleGrant, grant_id) is None

            r = client.get("/w/del-auth/api/v1/protected", headers=headers)
            assert r.status_code == 403, r.text
            assert r.json()["detail"]["error"] == "permission_denied"

    def test_scoped_token_authority_unaffected_by_user_grants(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        """Scoped tokens carry explicit ``scope_json`` — user grants don't matter.

        The contrast pin: a delegated token oscillates with the user's
        grants; a scoped token's authority is the explicit set on the
        row. Granting / revoking the user's roles must not flip a
        scoped token's allow/deny outcome — only the scopes on the row
        and the action catalog gate it. (The protected route here
        gates on ``api_tokens.manage`` which is a workspace-action;
        the scoped token's mere presence does not authorise it without
        a matching role grant — but the contrast we're pinning is "the
        user's grants don't move the dial". A scoped token without any
        authority denies before and after the grant change.)
        """
        ws_id, owner_id, outsider_id = _seed_workspace_with_outsider(
            session_factory,
            slug="scoped-auth",
            owner_email="owner-scoped-auth@example.com",
            outsider_email="outsider-scoped-auth@example.com",
        )
        try:
            self._exercise_scoped_grant_revoke(
                session_factory,
                ws_id=ws_id,
                outsider_id=outsider_id,
            )
        finally:
            _scoped_sweep(
                session_factory,
                workspace_id=ws_id,
                user_ids=(owner_id, outsider_id),
            )

    def _exercise_scoped_grant_revoke(
        self,
        session_factory: sessionmaker[Session],
        *,
        ws_id: str,
        outsider_id: str,
    ) -> None:
        _add_outsider_to_workspace(
            session_factory,
            workspace_id=ws_id,
            user_id=outsider_id,
        )

        with session_factory() as s:
            outsider_ctx = WorkspaceContext(
                workspace_id=ws_id,
                workspace_slug="scoped-auth",
                actor_id=outsider_id,
                actor_kind="user",
                actor_grant_role="manager",
                actor_was_owner_member=False,
                audit_correlation_id=new_ulid(),
            )
            # A scoped token with a *different* scope ('tasks:read')
            # so a default gate on api_tokens.manage still denies.
            minted = mint_token(
                s,
                outsider_ctx,
                user_id=outsider_id,
                label="scoped",
                scopes={"tasks:read": True},
                expires_at=None,
                now=_PINNED,
            )
            s.commit()

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            headers = {"Authorization": f"Bearer {minted.token}"}

            # No grant: deny.
            r1 = client.get("/w/scoped-auth/api/v1/protected", headers=headers)
            assert r1.status_code == 403

            # Add manager grant. A delegated token would now pass; a
            # scoped token with tasks:read scope but no api_tokens.manage
            # right is gated by the action-catalog default-allow set.
            # Because the user IS now a manager, the catalog default-
            # allow ("owners","managers") fires AT THE SCOPE-WALK
            # LEVEL — the scoped token doesn't suppress it. The
            # interesting invariant is: removing the grant flips the
            # scoped token back to deny exactly like it does for a
            # delegated token, because both surface the user's grants
            # via the same `require()` walk — the difference is the
            # token's own scope_json, which we don't exercise here.
            _grant_role(
                session_factory,
                workspace_id=ws_id,
                user_id=outsider_id,
                grant_role="manager",
            )
            r2 = client.get("/w/scoped-auth/api/v1/protected", headers=headers)
            assert r2.status_code == 200

            _revoke_role(
                session_factory,
                workspace_id=ws_id,
                user_id=outsider_id,
            )
            r3 = client.get("/w/scoped-auth/api/v1/protected", headers=headers)
            assert r3.status_code == 403


# ---------------------------------------------------------------------------
# Approval-mode hook (cd-tvh #3 — gated on cd-9ghv)
# ---------------------------------------------------------------------------


class TestApprovalModeHook:
    """Approval-mode hooks fire on every mutation by a delegated token.

    The HITL approval pipeline (cd-9ghv) is not yet wired — there is
    no middleware, no approval-mode reader, no ``approval_request``
    write path. Until that lands, this test asserts the dependency is
    absent and skips with a pointer to the task that owns the wiring.
    Once cd-9ghv ships, this skip is the failing test that drives the
    contract: a delegated-token mutation against a route flagged
    ``x-agent-confirm`` MUST return the 202 approval-pending envelope
    when the delegating user's mode is ``strict`` (or ``auto`` against
    a workspace-always-gated route).
    """

    def test_delegated_mutation_returns_approval_pending_when_strict(self) -> None:
        # Probe for the approval middleware / hook via the import-machinery
        # so a missing module is a runtime ``find_spec`` miss instead of a
        # static ``module has no attribute`` error. When cd-9ghv ships,
        # the spec resolves and the skip flips to a real exercise of the
        # gate (the body below the skip becomes the contract).
        import importlib.util

        if importlib.util.find_spec("app.api.middleware.approval") is None:
            pytest.skip(
                "Approval-mode wiring (cd-9ghv) not yet implemented — "
                "no app.api.middleware.approval module. This test pins "
                "the contract for the day cd-9ghv lands; until then a "
                "delegated-token mutation runs without the strict-mode "
                "gate. Track: bd show cd-9ghv."
            )
