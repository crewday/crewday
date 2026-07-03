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
from typing import Annotated, cast

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.identity.models import User
from app.adapters.db.llm.models import ApprovalRequest
from app.adapters.db.messaging.models import Notification
from app.adapters.db.session import FilteredSession
from app.api.middleware.approval import AgentApprovalMiddleware
from app.auth.tokens import mint as mint_token
from app.authz.dep import Permission
from app.config import Settings
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.tenancy.middleware import WorkspaceContextMiddleware
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.integration.auth._cleanup import delete_api_tokens_for_scope

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_PROBLEM_TYPE_BASE = "https://crewday.dev/errors/"


def _assert_delegating_user_inactive(response: Response, *, instance: str) -> None:
    assert response.status_code == 401, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    error_id = body.pop("error_id")
    assert isinstance(error_id, str)
    assert error_id
    assert body == {
        "type": f"{_PROBLEM_TYPE_BASE}delegating_user_inactive",
        "title": "Unauthorized",
        "status": 401,
        "instance": instance,
        "user_message": "Unauthorized",
        "detail": "Unauthorized",
    }


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
    _session_mod._default_sessionmaker_ = cast(
        "sessionmaker[FilteredSession]", session_factory
    )
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
    ``delegate_for_user_id`` / ``subject_user_id`` on a row whose
    ``kind`` requires it).
    """
    from app.adapters.db.audit.models import AuditLog
    from app.adapters.db.authz.models import (
        PermissionGroup,
        PermissionGroupMember,
    )
    from app.adapters.db.identity.models import User
    from app.adapters.db.workspace.models import UserWorkspace, Workspace

    with session_factory() as s, tenant_agnostic():
        delete_api_tokens_for_scope(
            s,
            workspace_ids=(workspace_id,),
            user_ids=user_ids,
        )
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


def _build_approval_app() -> FastAPI:
    """FastAPI app with the real tenancy + approval middleware chain.

    Mirrors the production ``factory.py`` ordering: the approval gate
    is registered *inner* of :class:`WorkspaceContextMiddleware` so the
    tenancy resolver stamps ``request.state`` with the delegated
    :class:`ActorIdentity` before the gate reads it. A plain cancel
    route stands in for the real handler — the middleware short-circuits
    with the 409 pending envelope before the route is ever reached, so a
    ``200 executed`` would prove the gate failed to fire.
    """
    app = FastAPI()
    app.add_middleware(AgentApprovalMiddleware)
    app.add_middleware(WorkspaceContextMiddleware)

    @app.post("/w/{slug}/api/v1/tasks/{task_id}/cancel")
    def cancel(slug: str, task_id: str) -> dict[str, str]:
        return {"status": "executed"}

    return app


def _set_agent_approval_mode(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    mode: str,
) -> None:
    """Set the delegating user's per-user agent approval mode (§11)."""
    with session_factory() as s, tenant_agnostic():
        user = s.get(User, user_id)
        assert user is not None
        user.agent_approval_mode = mode
        s.commit()


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

            # 1) No grant — 401 ``delegating_user_inactive``.
            #    cd-ljvs: the verifier refuses the token before the
            #    action catalog ever runs because the delegating user
            #    holds zero live grants in the workspace. Pre-cd-ljvs
            #    this returned 403 ``permission_denied`` from the
            #    action-catalog seam; spec §03 "Delegated tokens"
            #    pins the 401 verify-time gate, which is what the
            #    agent now sees.
            r = client.get("/w/del-auth/api/v1/protected", headers=headers)
            _assert_delegating_user_inactive(
                r,
                instance="/w/del-auth/api/v1/protected",
            )

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

            # 3) Revoke the grant — same token denies again with the
            #    cd-ljvs verify-time gate (``_revoke_role`` here hard-
            #    deletes; in production cd-x1xh's soft-revoke shape
            #    sets ``revoked_at`` instead. Both shapes collapse
            #    "no live grants" to the same 401).
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
            _assert_delegating_user_inactive(
                r,
                instance="/w/del-auth/api/v1/protected",
            )

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
            # A scoped token whose scope ('tasks:read') does NOT cover
            # the action's mapped scope. ``api_tokens.manage`` requires
            # ``admin:rotate`` (§03 "Guardrails" / app.authz.scopes), so
            # the scope gate (cd-7t1f1) denies with ``insufficient_scope``
            # regardless of the user's grants.
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

            # Add a manager grant. A *delegated* token would now pass —
            # its authority tracks the user's grants. A *scoped* token
            # does NOT: the ``admin:rotate`` scope it was never issued is
            # missing, so the scope gate refuses it with
            # ``insufficient_scope`` even though the role walk would now
            # allow the user. The user's grants do not move the dial for
            # a scoped token — exactly what this test's name pins.
            _grant_role(
                session_factory,
                workspace_id=ws_id,
                user_id=outsider_id,
                grant_role="manager",
            )
            r2 = client.get("/w/scoped-auth/api/v1/protected", headers=headers)
            assert r2.status_code == 403
            # This harness builds a bare ``FastAPI()`` without
            # ``add_exception_handlers``, so FastAPI's default handler
            # renders ``HTTPException.detail`` verbatim under ``detail``
            # (in production, app/api/errors.py spreads the dict to
            # top-level §12 fields instead). The WWW-Authenticate header
            # below is the spec-pinned invariant and holds on both paths.
            assert r2.json()["detail"]["error"] == "insufficient_scope"
            assert r2.json()["detail"]["scope"] == "admin:rotate"
            assert 'error="insufficient_scope"' in r2.headers["WWW-Authenticate"]
            assert 'scope="admin:rotate"' in r2.headers["WWW-Authenticate"]

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
    """A delegated-token mutation under user ``strict`` mode is queued, not run.

    cd-9ghv shipped ``app.api.middleware.approval``. When the
    delegating user is in ``strict`` mode, a delegated token's mutating
    write must not execute directly: :class:`AgentApprovalMiddleware`
    intercepts the call, writes an ``ApprovalRequest`` row, notifies the
    workspace owners / managers, and returns the §11 ``409
    approval_required`` pending envelope. The route handler is never
    invoked. See ``docs/specs/11-llm-and-agents.md`` §"Agent action
    approval" (source ``user_strict_mutation``).
    """

    def test_delegated_mutation_returns_approval_pending_when_strict(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        ws_id, owner_id, outsider_id = _seed_workspace_with_outsider(
            session_factory,
            slug="strict-appr",
            owner_email="owner-strict-appr@example.com",
            outsider_email="outsider-strict-appr@example.com",
        )
        try:
            self._exercise_strict_gate(
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

    def _exercise_strict_gate(
        self,
        session_factory: sessionmaker[Session],
        *,
        ws_id: str,
        outsider_id: str,
    ) -> None:
        # The delegating user must hold a live grant so the verifier
        # admits the token — a grantless delegated token 401s at verify
        # time (cd-ljvs, see TestAuthorityFollowsUser). A manager grant
        # clears that gate; the strict-mode knob below is the only thing
        # that turns a successful write into a queued approval.
        _add_outsider_to_workspace(
            session_factory,
            workspace_id=ws_id,
            user_id=outsider_id,
        )
        _grant_role(
            session_factory,
            workspace_id=ws_id,
            user_id=outsider_id,
            grant_role="manager",
        )
        _set_agent_approval_mode(
            session_factory,
            user_id=outsider_id,
            mode="strict",
        )

        with session_factory() as s:
            owner_ctx = WorkspaceContext(
                workspace_id=ws_id,
                workspace_slug="strict-appr",
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

        task_id = new_ulid()
        app = _build_approval_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.post(
                f"/w/strict-appr/api/v1/tasks/{task_id}/cancel",
                headers={"Authorization": f"Bearer {minted.token}"},
                json={"reason_md": "Guest cancelled the stay"},
            )

        # 1) The §11 409 pending envelope — the handler never runs, so
        #    the "executed" body is proof-of-failure if it ever appears.
        assert r.status_code == 409, r.text
        assert r.headers["content-type"].startswith("application/problem+json")
        body = r.json()
        assert body["type"] == f"{_PROBLEM_TYPE_BASE}approval_required"
        assert body["status"] == 409
        assert body["error"] == "approval_required"
        assert body["approval_status"] == "pending"
        approval_id = body["approval_id"]
        assert isinstance(approval_id, str) and approval_id
        assert body["approval_request_id"] == approval_id
        assert body["expires_at"]

        # 2) The ApprovalRequest row landed with the resolved payload.
        with session_factory() as s, tenant_agnostic():
            row = s.get(ApprovalRequest, approval_id)
            assert row is not None
            assert row.workspace_id == ws_id
            assert row.requester_actor_id == outsider_id
            assert row.for_user_id == outsider_id
            assert row.status == "pending"
            assert row.resolved_user_mode == "strict"
            assert row.inline_channel == "desk_only"
            assert row.expires_at is not None
            assert row.action_json["tool_name"] == "cancel_task"
            assert row.action_json["pre_approval_source"] == "user_strict_mutation"
            assert row.action_json["card_summary"] == "Cancel task?"
            assert row.action_json["requested_by_token_id"] == minted.key_id
            assert row.action_json["tool_input"] == {
                "workspace_slug": "strict-appr",
                "task_id": task_id,
                "reason_md": "Guest cancelled the stay",
            }

            # 3) The owner / manager fan-out fired (lines 221-233): one
            #    APPROVAL_NEEDED notification per resolved decider.
            notifs = s.scalars(
                select(Notification).where(
                    Notification.workspace_id == ws_id,
                    Notification.kind == "approval_needed",
                )
            ).all()
            assert notifs, "expected an approval_needed fan-out notification"
            # The delegating user is a resolved manager, so they are one
            # of the notified deciders; owners are also fanned to.
            assert outsider_id in {n.recipient_user_id for n in notifs}

    def test_delegated_strict_mutation_without_reason_is_rejected(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        """A strict-mode cancel missing ``reason_md`` is a 422, not a queue.

        The gate resolves the target but cannot build the approval
        action without the required ``reason_md`` payload field, so it
        returns the §12 ``422 validation`` envelope and writes no
        ``ApprovalRequest`` row.
        """
        ws_id, owner_id, outsider_id = _seed_workspace_with_outsider(
            session_factory,
            slug="strict-noreason",
            owner_email="owner-strict-noreason@example.com",
            outsider_email="outsider-strict-noreason@example.com",
        )
        try:
            _add_outsider_to_workspace(
                session_factory, workspace_id=ws_id, user_id=outsider_id
            )
            _grant_role(
                session_factory,
                workspace_id=ws_id,
                user_id=outsider_id,
                grant_role="manager",
            )
            _set_agent_approval_mode(
                session_factory, user_id=outsider_id, mode="strict"
            )
            with session_factory() as s:
                owner_ctx = WorkspaceContext(
                    workspace_id=ws_id,
                    workspace_slug="strict-noreason",
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

            app = _build_approval_app()
            with TestClient(app, raise_server_exceptions=False) as client:
                r = client.post(
                    f"/w/strict-noreason/api/v1/tasks/{new_ulid()}/cancel",
                    headers={"Authorization": f"Bearer {minted.token}"},
                    json={"note": "missing the required reason_md"},
                )

            assert r.status_code == 422, r.text
            assert r.json()["type"] == f"{_PROBLEM_TYPE_BASE}validation"
            with session_factory() as s, tenant_agnostic():
                assert (
                    s.scalars(
                        select(ApprovalRequest).where(
                            ApprovalRequest.workspace_id == ws_id
                        )
                    ).all()
                    == []
                )
        finally:
            _scoped_sweep(
                session_factory,
                workspace_id=ws_id,
                user_ids=(owner_id, outsider_id),
            )
