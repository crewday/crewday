"""Integration test for :mod:`app.api.v1.auth.tokens` — end-to-end flow.

Exercises ``POST → GET → DELETE`` + a gated-route Bearer verify against
a real engine (SQLite by default; Postgres when
``CREWDAY_TEST_DB=postgres``), driving the FastAPI router with a
pinned :class:`WorkspaceContext` via the standard permission stack.

The flow lands:

* One :class:`ApiToken` row per mint.
* A :class:`~app.adapters.db.audit.models.AuditLog` trail:
  ``api_token.minted`` → ``api_token.revoked`` (and ``revoked_noop``
  on the idempotent retry).
* An HTTP 204 on revoke; 404 on an unknown ``token_id``.

See ``docs/specs/03-auth-and-tokens.md`` §"API tokens" and
``docs/specs/12-rest-api.md`` §"Auth / tokens".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import ApiToken, User
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import current_workspace_context
from app.api.deps import db_session as _db_session_dep
from app.api.v1.auth.tokens import build_tokens_router
from app.auth.tokens import verify as verify_token
from app.tenancy import PrincipalKind, WorkspaceContext, tenant_agnostic
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Per-test session factory that commits on clean exit."""
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def seeded_ctx(
    session_factory: sessionmaker[Session],
) -> Iterator[WorkspaceContext]:
    """Seed a user + workspace + owners membership, yield a matching ctx.

    The permission gate on ``api_tokens.manage`` walks the user's
    owners-group membership to decide; seeding via
    :func:`bootstrap_workspace` lands the group + member rows so the
    default-allow (owners, managers) branch fires.

    Each test run gets a uniquely-named slug + email (derived from a
    fresh ULID) so sibling integration tests don't collide on the
    case-insensitive email unique index when they share the same
    session-scoped engine. Teardown drops every row we touched so
    the next test starts from a clean slate.
    """
    from app.util.ulid import new_ulid as _new_ulid

    tag = _new_ulid()[-8:].lower()
    email = f"mgr-{tag}@example.com"
    slug = f"ws-tok-{tag}"

    with session_factory() as s:
        user = bootstrap_user(s, email=email, display_name="Manager")
        ws = bootstrap_workspace(
            s,
            slug=slug,
            name="Tokens",
            owner_user_id=user.id,
        )
        s.commit()
        user_id, ws_id, ws_slug = user.id, ws.id, ws.slug

    ctx = build_workspace_context(
        workspace_id=ws_id,
        workspace_slug=ws_slug,
        actor_id=user_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    yield ctx

    # Scoped cleanup — delete every row we seeded so concurrent /
    # sibling integration tests see a clean state. We walk the
    # workspace children first (the FK cascades would handle most of
    # this, but ``user_workspace`` / ``role_grant`` rows are
    # workspace-scoped and need the tenant filter bypassed).
    with session_factory() as s:
        with tenant_agnostic():
            for tok in s.scalars(
                select(ApiToken).where(ApiToken.workspace_id == ws_id)
            ).all():
                s.delete(tok)
            for audit in s.scalars(
                select(AuditLog).where(AuditLog.workspace_id == ws_id)
            ).all():
                s.delete(audit)
            for grant in s.scalars(
                select(RoleGrant).where(RoleGrant.workspace_id == ws_id)
            ).all():
                s.delete(grant)
            for member in s.scalars(
                select(PermissionGroupMember).where(
                    PermissionGroupMember.workspace_id == ws_id
                )
            ).all():
                s.delete(member)
            for group in s.scalars(
                select(PermissionGroup).where(PermissionGroup.workspace_id == ws_id)
            ).all():
                s.delete(group)
            for uw in s.scalars(
                select(UserWorkspace).where(UserWorkspace.workspace_id == ws_id)
            ).all():
                s.delete(uw)
            ws_row = s.get(Workspace, ws_id)
            if ws_row is not None:
                s.delete(ws_row)
            user_row = s.get(User, user_id)
            if user_row is not None:
                s.delete(user_row)
        s.commit()


@pytest.fixture
def client(
    engine: Engine,
    session_factory: sessionmaker[Session],
    seeded_ctx: WorkspaceContext,
) -> Iterator[TestClient]:
    """FastAPI :class:`TestClient` mounted on the tokens router.

    The permission gate reads :class:`WorkspaceContext` via
    :func:`current_workspace_context`; we override the dep so every
    request sees ``seeded_ctx``. The UoW dep yields a session on the
    shared engine and commits on clean exit — matching the production
    shape.
    """
    app = FastAPI()
    # Wire the same problem+json error handlers the production factory
    # installs (cd-waq3) so domain errors like ``InvalidCursor`` surface
    # as 422 with the canonical type URI rather than bubbling as 500.
    from app.api.errors import add_exception_handlers

    add_exception_handlers(app)
    app.include_router(build_tokens_router(), prefix="/api/v1")

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

    def _ctx() -> WorkspaceContext:
        return seeded_ctx

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = _ctx

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# End-to-end happy path
# ---------------------------------------------------------------------------


class TestTokensHttpFlow:
    """POST → GET → DELETE via the real HTTP router + real DB."""

    def test_mint_then_list_then_revoke(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        # 1. Mint — 201, plaintext returned once.
        r = client.post(
            "/api/v1/auth/tokens",
            json={
                "label": "hermes-scheduler",
                "scopes": {"tasks:read": True, "stays:read": True},
                "expires_at_days": 30,
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["token"].startswith("mip_")
        key_id = body["key_id"]
        assert len(key_id) == 26
        assert body["prefix"]
        assert body["expires_at"] is not None

        # 2. List — returns the row we just inserted, wrapped in the
        # §12 cursor envelope (cd-msu2).
        r = client.get("/api/v1/auth/tokens")
        assert r.status_code == 200, r.text
        envelope = r.json()
        assert envelope["has_more"] is False
        assert envelope["next_cursor"] is None
        rows = envelope["data"]
        assert len(rows) == 1
        assert rows[0]["key_id"] == key_id
        assert rows[0]["label"] == "hermes-scheduler"
        assert rows[0]["prefix"] == body["prefix"]
        assert rows[0]["scopes"] == {"tasks:read": True, "stays:read": True}
        # The hash is never in the list response.
        assert "hash" not in rows[0]
        # §03 "API tokens": plaintext `token` is returned ONLY on the
        # 201 mint response — never on subsequent list reads. cd-rpxd
        # acceptance criterion #3 — regression-pinned here so a future
        # schema edit that re-surfaces the secret fails loudly.
        assert "token" not in rows[0]

        # 3. Verify the plaintext token against the DB directly — this
        # mirrors what the future Bearer-auth middleware will do.
        with session_factory() as s:
            verified = verify_token(s, token=body["token"])
            assert verified.user_id == seeded_ctx.actor_id
            assert verified.workspace_id == seeded_ctx.workspace_id
            assert verified.key_id == key_id

        # 4. Revoke — 204.
        r = client.delete(f"/api/v1/auth/tokens/{key_id}")
        assert r.status_code == 204, r.text

        # 5. Verify fails post-revoke.
        from app.auth.tokens import TokenRevoked

        with session_factory() as s, pytest.raises(TokenRevoked):
            verify_token(s, token=body["token"])

    def test_revoke_unknown_token_is_404(self, client: TestClient) -> None:
        r = client.delete("/api/v1/auth/tokens/01HWA00000000000000000NOPE")
        assert r.status_code == 404
        assert r.json()["error"] == "token_not_found"

    def test_double_revoke_is_idempotent(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        mint_r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "idem", "scopes": {}, "expires_at_days": 7},
        )
        assert mint_r.status_code == 201
        key_id = mint_r.json()["key_id"]

        # First delete — 204.
        r1 = client.delete(f"/api/v1/auth/tokens/{key_id}")
        assert r1.status_code == 204
        # Second delete — still 204 (idempotent).
        r2 = client.delete(f"/api/v1/auth/tokens/{key_id}")
        assert r2.status_code == 204

        # Audit trail has one revoke + one revoked_noop.
        with session_factory() as s:
            audits = s.scalars(
                select(AuditLog).where(
                    AuditLog.workspace_id == seeded_ctx.workspace_id,
                    AuditLog.entity_id == key_id,
                )
            ).all()
            actions = [a.action for a in audits]
            assert "api_token.minted" in actions
            assert "api_token.revoked" in actions
            assert "api_token.revoked_noop" in actions

    def test_sixth_mint_is_422_too_many(
        self,
        client: TestClient,
    ) -> None:
        for i in range(5):
            r = client.post(
                "/api/v1/auth/tokens",
                json={
                    "label": f"t-{i}",
                    "scopes": {},
                    "expires_at_days": 30,
                },
            )
            assert r.status_code == 201, r.text
        r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "6th", "scopes": {}, "expires_at_days": 30},
        )
        assert r.status_code == 422
        assert r.json()["error"] == "too_many_tokens"


# ---------------------------------------------------------------------------
# cd-i1qe — delegated tokens through POST /auth/tokens
# ---------------------------------------------------------------------------


class TestDelegatedTokensHttp:
    """``delegate: true`` mints a delegated row and the response echoes kind."""

    def test_delegated_mint_returns_kind_and_delegate_fk(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        r = client.post(
            "/api/v1/auth/tokens",
            json={
                "label": "chat-agent",
                "delegate": True,
                "scopes": {},
                "expires_at_days": 30,
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["kind"] == "delegated"
        key_id = body["key_id"]
        # The row carries the FK back to the caller.
        with session_factory() as s:
            row = s.get(ApiToken, key_id)
            assert row is not None
            assert row.kind == "delegated"
            assert row.delegate_for_user_id == seeded_ctx.actor_id
            assert row.workspace_id == seeded_ctx.workspace_id
        # GET /auth/tokens surfaces the row with the discriminator.
        r_list = client.get("/api/v1/auth/tokens")
        assert r_list.status_code == 200
        rows = r_list.json()["data"]
        match = next(row for row in rows if row["key_id"] == key_id)
        assert match["kind"] == "delegated"
        assert match["delegate_for_user_id"] == seeded_ctx.actor_id

    def test_delegated_with_nonempty_scopes_is_422(
        self,
        client: TestClient,
    ) -> None:
        """§03: delegated tokens reject non-empty scopes."""
        r = client.post(
            "/api/v1/auth/tokens",
            json={
                "label": "bad",
                "delegate": True,
                "scopes": {"tasks:read": True},
                "expires_at_days": 30,
            },
        )
        assert r.status_code == 422
        assert r.json()["error"] == "delegated_requires_empty_scopes"

    def test_scoped_with_me_scope_is_422_conflict(
        self,
        client: TestClient,
    ) -> None:
        """Mixing me:* with a scoped token body is ``me_scope_conflict``."""
        r = client.post(
            "/api/v1/auth/tokens",
            json={
                "label": "bad",
                "scopes": {"tasks:read": True, "me.tasks:read": True},
                "expires_at_days": 30,
            },
        )
        assert r.status_code == 422
        assert r.json()["error"] == "me_scope_conflict"

    def test_delegated_default_ttl_is_30_days(
        self,
        client: TestClient,
    ) -> None:
        """§03 "Guardrails": delegated tokens default to 30 days."""
        r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "agent", "delegate": True, "scopes": {}},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        # The response shape is ISO-8601; parse and compare deltas.
        expires = datetime.fromisoformat(body["expires_at"])
        now = datetime.now(tz=expires.tzinfo or UTC)
        # 30 days ±10s — comfortably inside a window that survives
        # test-runner clock drift.
        delta = expires - now
        assert timedelta(days=29, hours=23) <= delta <= timedelta(days=30, hours=1)

    def test_scoped_default_ttl_is_90_days(
        self,
        client: TestClient,
    ) -> None:
        r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "agent", "scopes": {}},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        expires = datetime.fromisoformat(body["expires_at"])
        now = datetime.now(tz=expires.tzinfo or UTC)
        delta = expires - now
        assert timedelta(days=89, hours=23) <= delta <= timedelta(days=90, hours=1)


# ---------------------------------------------------------------------------
# cd-tvh — refuse delegated mint from a non-session caller
# ---------------------------------------------------------------------------


class TestDelegatedRequiresSession:
    """§03 "Delegated tokens" + §11 "no transitive delegation".

    A delegated token can only be minted by a passkey **session**:
    the spec text reads "A delegated token can only be created by a
    passkey session — it cannot be created by another token (no
    transitive delegation)". The route guard inspects
    :attr:`WorkspaceContext.principal_kind` and rejects any caller
    that didn't authenticate via the session-cookie branch.

    This suite drives a parallel client whose
    :func:`current_workspace_context` override stamps
    ``principal_kind="token"`` (and a sibling ``principal_kind="system"``)
    so the route can branch deterministically without standing up the
    real bearer-token middleware. The actual middleware → route
    integration is exercised by
    :class:`tests.integration.test_tenancy_middleware_auth.TestBearerTokenEndToEnd`
    — what we pin here is the refusal logic at the routing seam.
    """

    @staticmethod
    def _build_client(
        engine: Engine,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
        *,
        principal_kind: PrincipalKind,
    ) -> TestClient:
        """Mount the tokens router with a ctx that pins ``principal_kind``."""
        from dataclasses import replace

        from app.api.errors import add_exception_handlers
        from app.api.v1.auth.tokens import build_tokens_router

        token_ctx = replace(seeded_ctx, principal_kind=principal_kind)

        app = FastAPI()
        # Mirror the shared ``client`` fixture — production parity for
        # the problem+json envelope (cd-waq3).
        add_exception_handlers(app)
        app.include_router(build_tokens_router(), prefix="/api/v1")

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

        def _ctx() -> WorkspaceContext:
            return token_ctx

        from app.api.deps import db_session as _db_session_dep

        app.dependency_overrides[_db_session_dep] = _session
        app.dependency_overrides[current_workspace_context] = _ctx

        return TestClient(app)

    def test_delegated_mint_from_token_caller_is_422(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        """Token-presented delegated mint → 422 ``delegated_requires_session``."""
        with self._build_client(
            engine,
            session_factory,
            seeded_ctx,
            principal_kind="token",
        ) as client:
            r = client.post(
                "/api/v1/auth/tokens",
                json={
                    "label": "transitive",
                    "delegate": True,
                    "scopes": {},
                    "expires_at_days": 30,
                },
            )
        assert r.status_code == 422, r.text
        assert r.json()["error"] == "delegated_requires_session"

    def test_delegated_mint_from_system_caller_is_422(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        """System-presented mint also refused — only sessions can mint delegated.

        No production caller mints a delegated token from a system
        actor today, but the guard is symmetric on purpose: every
        non-session arm refuses, so a future worker that drifts into
        minting on behalf of a user is caught at the seam.
        """
        with self._build_client(
            engine,
            session_factory,
            seeded_ctx,
            principal_kind="system",
        ) as client:
            r = client.post(
                "/api/v1/auth/tokens",
                json={
                    "label": "system-mint",
                    "delegate": True,
                    "scopes": {},
                    "expires_at_days": 30,
                },
            )
        assert r.status_code == 422, r.text
        assert r.json()["error"] == "delegated_requires_session"

    def test_scoped_mint_from_token_caller_is_allowed(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        """The guard is **delegated-only** — scoped mints from tokens still succeed.

        §03 reserves the no-transitive-delegation rule for delegated
        tokens specifically; a workspace-management agent legitimately
        needs to mint scoped tokens via API automation. Pin the
        non-regression here so a future tightening of the guard fails
        loudly instead of silently breaking the automation surface.
        """
        with self._build_client(
            engine,
            session_factory,
            seeded_ctx,
            principal_kind="token",
        ) as client:
            r = client.post(
                "/api/v1/auth/tokens",
                json={
                    "label": "scoped-from-token",
                    "scopes": {"tasks:read": True},
                    "expires_at_days": 30,
                },
            )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["kind"] == "scoped"

    def test_delegated_mint_from_session_caller_still_allowed(
        self,
        client: TestClient,
    ) -> None:
        """Regression guard — the default fixture stamps ``principal_kind="session"``.

        The sibling :class:`TestDelegatedTokensHttp` already exercises
        the happy path; we re-pin it here next to the refusal cases so
        future readers see the allow / deny pair side by side.
        """
        r = client.post(
            "/api/v1/auth/tokens",
            json={
                "label": "from-session",
                "delegate": True,
                "scopes": {},
                "expires_at_days": 30,
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["kind"] == "delegated"


# ---------------------------------------------------------------------------
# cd-msu2 — cursor pagination on the workspace tokens listing
# ---------------------------------------------------------------------------


class TestTokensCursorPagination:
    """``GET /auth/tokens`` pages through tokens via the §12 envelope.

    Each test mints rows directly through the API so the round-trip
    exercises the same code path the SPA / CLI hit. The per-user cap
    of 5 active workspace tokens (§03 "Guardrails") would otherwise
    block multi-page fixtures, so the corpus is built across multiple
    seeded users (one extra user mints into the same workspace).
    """

    @staticmethod
    def _seed_extra_users(
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
        *,
        count: int,
    ) -> list[str]:
        """Bootstrap ``count`` extra users into ``seeded_ctx``'s workspace.

        Returns their ids so the test can mint tokens on each user's
        behalf via the domain :func:`mint` (the route only mints on
        the caller's id, but the §03 cap is per-user-per-workspace —
        spreading the corpus across users sidesteps the 5-token cap
        without weakening the production route guard).
        """
        from app.util.ulid import new_ulid as _new_ulid

        ids: list[str] = []
        with session_factory() as s:
            for _ in range(count):
                tag = _new_ulid()[-8:].lower()
                user = bootstrap_user(
                    s, email=f"alt-{tag}@example.com", display_name="Alt"
                )
                ids.append(user.id)
            s.commit()
        return ids

    @staticmethod
    def _mint_n(
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
        *,
        owners: list[str],
    ) -> list[str]:
        """Mint one token per owner directly through the domain layer.

        Returns the resulting ``key_id`` list in mint order so the
        caller can correlate against the response. Domain mint avoids
        the route-level cap collapse (each user is independently below
        their 5-token allowance) without simulating multiple sessions.
        """
        from app.auth.tokens import mint as domain_mint

        now = datetime.now(tz=UTC)
        key_ids: list[str] = []
        with session_factory() as s:
            for owner in owners:
                result = domain_mint(
                    s,
                    seeded_ctx,
                    user_id=owner,
                    label=f"page-{owner[-6:]}",
                    scopes={"tasks:read": True},
                    expires_at=now + timedelta(days=30),
                    now=now,
                )
                key_ids.append(result.key_id)
            s.commit()
        return key_ids

    def test_single_page_under_default_limit_no_cursor(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        """Default-limit page returns every row with ``has_more=False``."""
        # Mint 3 rows on the seeded user (well under the 5-token cap).
        for i in range(3):
            r = client.post(
                "/api/v1/auth/tokens",
                json={
                    "label": f"single-{i}",
                    "scopes": {"tasks:read": True},
                    "expires_at_days": 30,
                },
            )
            assert r.status_code == 201, r.text

        r = client.get("/api/v1/auth/tokens")
        assert r.status_code == 200, r.text
        envelope = r.json()
        assert envelope["has_more"] is False
        assert envelope["next_cursor"] is None
        assert len(envelope["data"]) == 3

    def test_multi_page_traversal_no_dupes_no_skips(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        """Walk forward through 2 pages — every row appears exactly once."""
        # Mint 7 rows total so a limit=3 walk produces 3 + 3 + 1.
        # Spread across 2 extra users to clear the per-user 5-token cap.
        extras = self._seed_extra_users(session_factory, seeded_ctx, count=2)
        owners = [seeded_ctx.actor_id] * 4 + [extras[0]] * 2 + [extras[1]] * 1
        seeded_key_ids = self._mint_n(session_factory, seeded_ctx, owners=owners)
        assert len(seeded_key_ids) == 7

        # Page 1.
        r1 = client.get("/api/v1/auth/tokens?limit=3")
        assert r1.status_code == 200, r1.text
        page1 = r1.json()
        assert page1["has_more"] is True
        assert page1["next_cursor"] is not None
        assert len(page1["data"]) == 3

        # Page 2.
        r2 = client.get(f"/api/v1/auth/tokens?limit=3&cursor={page1['next_cursor']}")
        assert r2.status_code == 200, r2.text
        page2 = r2.json()
        assert page2["has_more"] is True
        assert page2["next_cursor"] is not None
        assert len(page2["data"]) == 3

        # Page 3 — last partial page.
        r3 = client.get(f"/api/v1/auth/tokens?limit=3&cursor={page2['next_cursor']}")
        assert r3.status_code == 200, r3.text
        page3 = r3.json()
        assert page3["has_more"] is False
        assert page3["next_cursor"] is None
        assert len(page3["data"]) == 1

        seen = (
            [r["key_id"] for r in page1["data"]]
            + [r["key_id"] for r in page2["data"]]
            + [r["key_id"] for r in page3["data"]]
        )
        # No duplicates and exactly the seeded set surfaces.
        assert len(set(seen)) == len(seen) == 7
        assert set(seen) == set(seeded_key_ids)

    def test_invalid_cursor_is_422_invalid_cursor(
        self,
        client: TestClient,
    ) -> None:
        """A tampered cursor surfaces the §12 ``invalid_cursor`` error."""
        r = client.get("/api/v1/auth/tokens?cursor=garbage")
        assert r.status_code == 422, r.text
        body = r.json()
        # The problem+json envelope sets the canonical ``type`` URI.
        assert body["type"].endswith("/invalid_cursor")

    def test_limit_above_max_is_422(self, client: TestClient) -> None:
        """``limit > 500`` is rejected at the FastAPI Query validator."""
        r = client.get("/api/v1/auth/tokens?limit=501")
        assert r.status_code == 422

    def test_limit_zero_is_422(self, client: TestClient) -> None:
        """``limit < 1`` is rejected at the FastAPI Query validator."""
        r = client.get("/api/v1/auth/tokens?limit=0")
        assert r.status_code == 422
