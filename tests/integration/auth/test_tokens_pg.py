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
from app.events.bus import bus as default_event_bus
from app.events.types import ApiTokenCreated, ApiTokenRevoked, ApiTokenRotated
from app.tenancy import PrincipalKind, WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)
from tests.integration.auth._cleanup import delete_api_tokens_for_scope

pytestmark = pytest.mark.integration

type ApiTokenLifecycleEvent = ApiTokenCreated | ApiTokenRevoked | ApiTokenRotated


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_PINNED = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


def _seed_workspace_cap_rows(
    session: Session, *, workspace_id: str, tag: str
) -> list[str]:
    user_ids: list[str] = []
    with tenant_agnostic():
        for user_index in range(10):
            user = bootstrap_user(
                session,
                email=f"ws-cap-{tag}-{user_index}@example.com",
                display_name=f"Workspace Cap {user_index}",
            )
            user_ids.append(user.id)
            for token_index in range(5):
                token_id = new_ulid()
                kind = "delegated" if token_index == 4 else "scoped"
                session.add(
                    ApiToken(
                        id=token_id,
                        user_id=user.id,
                        workspace_id=workspace_id,
                        kind=kind,
                        delegate_for_user_id=user.id if kind == "delegated" else None,
                        subject_user_id=None,
                        label=f"ws-cap-{user_index}-{token_index}",
                        scope_json={},
                        prefix=token_id[:8],
                        hash=f"seed-hash-{token_id}",
                        expires_at=_PINNED + timedelta(days=90),
                        last_used_at=None,
                        revoked_at=None,
                        created_at=_PINNED,
                    )
                )
        session.flush()
    return user_ids


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
            delete_api_tokens_for_scope(s, workspace_ids=(ws_id,), user_ids=(user_id,))
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


@pytest.fixture
def captured_api_token_events() -> Iterator[list[ApiTokenLifecycleEvent]]:
    """Capture token lifecycle events emitted by the production router."""
    events: list[ApiTokenLifecycleEvent] = []
    default_event_bus._reset_for_tests()
    default_event_bus.subscribe(ApiTokenCreated)(events.append)
    default_event_bus.subscribe(ApiTokenRevoked)(events.append)
    default_event_bus.subscribe(ApiTokenRotated)(events.append)
    try:
        yield events
    finally:
        default_event_bus._reset_for_tests()


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

    def test_workspace_cap_maps_to_422_too_many_workspace_tokens(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        tag = seeded_ctx.workspace_id[-8:].lower()
        with session_factory() as s:
            seeded_user_ids = _seed_workspace_cap_rows(
                s, workspace_id=seeded_ctx.workspace_id, tag=tag
            )
            s.commit()
        try:
            r = client.post(
                "/api/v1/auth/tokens",
                json={"label": "workspace-cap", "scopes": {}, "expires_at_days": 30},
            )
            assert r.status_code == 422
            assert r.json()["error"] == "too_many_workspace_tokens"
        finally:
            with session_factory() as s:
                with tenant_agnostic():
                    for token in s.scalars(
                        select(ApiToken).where(ApiToken.user_id.in_(seeded_user_ids))
                    ).all():
                        s.delete(token)
                    for user_id in seeded_user_ids:
                        row = s.get(User, user_id)
                        if row is not None:
                            s.delete(row)
                s.commit()


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


# ---------------------------------------------------------------------------
# cd-8i9tr — POST revoke alias (the SPA's preferred verb)
# ---------------------------------------------------------------------------


class TestPostRevokeAlias:
    """``POST /auth/tokens/{id}/revoke`` mirrors the DELETE behaviour."""

    def test_post_revoke_returns_204(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        mint_r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "post-revoke", "scopes": {}, "expires_at_days": 7},
        )
        assert mint_r.status_code == 201
        key_id = mint_r.json()["key_id"]

        r = client.post(f"/api/v1/auth/tokens/{key_id}/revoke")
        assert r.status_code == 204, r.text

        with session_factory() as s:
            row = s.get(ApiToken, key_id)
            assert row is not None
            assert row.revoked_at is not None

    def test_post_revoke_unknown_is_404(self, client: TestClient) -> None:
        r = client.post("/api/v1/auth/tokens/01HWA00000000000000000NOPE/revoke")
        assert r.status_code == 404
        assert r.json()["error"] == "token_not_found"

    def test_post_revoke_is_idempotent(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        mint_r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "post-idem", "scopes": {}, "expires_at_days": 7},
        )
        assert mint_r.status_code == 201
        key_id = mint_r.json()["key_id"]
        # First and second call both return 204; the audit trail
        # carries one ``revoked`` and one ``revoked_noop`` event.
        assert client.post(f"/api/v1/auth/tokens/{key_id}/revoke").status_code == 204
        assert client.post(f"/api/v1/auth/tokens/{key_id}/revoke").status_code == 204

        with session_factory() as s:
            actions = [
                a.action
                for a in s.scalars(
                    select(AuditLog).where(
                        AuditLog.workspace_id == seeded_ctx.workspace_id,
                        AuditLog.entity_id == key_id,
                    )
                ).all()
            ]
        assert "api_token.revoked" in actions
        assert "api_token.revoked_noop" in actions

    def test_mint_revoke_rotate_publish_sse_lifecycle_events(
        self,
        client: TestClient,
        captured_api_token_events: list[ApiTokenLifecycleEvent],
    ) -> None:
        mint_r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "publish-mint", "scopes": {}, "expires_at_days": 7},
        )
        assert mint_r.status_code == 201, mint_r.text
        minted_id = mint_r.json()["key_id"]

        revoke_r = client.delete(f"/api/v1/auth/tokens/{minted_id}")
        assert revoke_r.status_code == 204, revoke_r.text

        second_revoke_r = client.delete(f"/api/v1/auth/tokens/{minted_id}")
        assert second_revoke_r.status_code == 204, second_revoke_r.text

        rotate_seed_r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "publish-rotate", "scopes": {}, "expires_at_days": 7},
        )
        assert rotate_seed_r.status_code == 201, rotate_seed_r.text
        rotated_id = rotate_seed_r.json()["key_id"]

        rotate_r = client.post(f"/api/v1/auth/tokens/{rotated_id}/rotate")
        assert rotate_r.status_code == 200, rotate_r.text

        failed_rotate_r = client.post(f"/api/v1/auth/tokens/{minted_id}/rotate")
        assert failed_rotate_r.status_code == 404, failed_rotate_r.text

        assert [
            (type(event).name, event.id) for event in captured_api_token_events
        ] == [
            ("api_token.created", minted_id),
            ("api_token.revoked", minted_id),
            ("api_token.created", rotated_id),
            ("api_token.rotated", rotated_id),
        ]
        assert all(event.workspace_id for event in captured_api_token_events)
        assert [
            event.kind
            for event in captured_api_token_events
            if isinstance(event, ApiTokenCreated)
        ] == ["scoped", "scoped"]


# ---------------------------------------------------------------------------
# cd-8i9tr — secret rotation
# ---------------------------------------------------------------------------


class TestRotateHttp:
    """``POST /auth/tokens/{id}/rotate`` swaps the secret in place."""

    def test_rotate_returns_new_plaintext_same_key_id(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        mint_r = client.post(
            "/api/v1/auth/tokens",
            json={
                "label": "rotateme",
                "scopes": {"tasks:read": True},
                "expires_at_days": 30,
            },
        )
        assert mint_r.status_code == 201
        original = mint_r.json()
        key_id = original["key_id"]

        r = client.post(f"/api/v1/auth/tokens/{key_id}/rotate")
        assert r.status_code == 200, r.text
        rotated = r.json()
        assert rotated["key_id"] == key_id
        assert rotated["token"] != original["token"]
        assert rotated["prefix"] != original["prefix"]
        assert rotated["expires_at"] == original["expires_at"]

        # The OLD plaintext remains valid during the 1h overlap; the
        # NEW one verifies cleanly against the primary hash.
        from app.auth.tokens import verify as verify_token

        with session_factory() as s:
            verified = verify_token(s, token=rotated["token"])
            assert verified.key_id == key_id
        with session_factory() as s:
            verified = verify_token(s, token=original["token"])
            assert verified.key_id == key_id

    def test_rotate_unknown_is_404(self, client: TestClient) -> None:
        r = client.post("/api/v1/auth/tokens/01HWA00000000000000000NOPE/rotate")
        assert r.status_code == 404
        assert r.json()["error"] == "token_not_found"

    def test_rotate_revoked_is_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        mint_r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "rev-then-rot", "scopes": {}, "expires_at_days": 7},
        )
        assert mint_r.status_code == 201
        key_id = mint_r.json()["key_id"]
        # Revoke first…
        assert client.delete(f"/api/v1/auth/tokens/{key_id}").status_code == 204
        # …then a rotate should collapse to 404 (not 409 / 422 — we
        # don't leak which mode fired).
        r = client.post(f"/api/v1/auth/tokens/{key_id}/rotate")
        assert r.status_code == 404
        assert r.json()["error"] == "token_not_found"

    def test_rotate_writes_rotated_audit_with_prefix_change(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        seeded_ctx: WorkspaceContext,
    ) -> None:
        mint_r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "auditable", "scopes": {}, "expires_at_days": 30},
        )
        assert mint_r.status_code == 201
        original = mint_r.json()
        key_id = original["key_id"]
        rot_r = client.post(f"/api/v1/auth/tokens/{key_id}/rotate")
        assert rot_r.status_code == 200
        rotated = rot_r.json()

        with session_factory() as s:
            audits = s.scalars(
                select(AuditLog).where(
                    AuditLog.workspace_id == seeded_ctx.workspace_id,
                    AuditLog.entity_id == key_id,
                    AuditLog.action == "api_token.rotated",
                )
            ).all()
        assert len(audits) == 1
        diff = dict(audits[0].diff)
        assert diff["old_prefix"] == original["prefix"]
        assert diff["new_prefix"] == rotated["prefix"]


# ---------------------------------------------------------------------------
# cd-8i9tr — per-token audit timeline
# ---------------------------------------------------------------------------


class TestAuditTimelineHttp:
    """``GET /auth/tokens/{id}/audit`` surfaces the lifecycle trail."""

    def test_audit_returns_minted_event(
        self,
        client: TestClient,
        seeded_ctx: WorkspaceContext,
    ) -> None:
        mint_r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "audit-mint", "scopes": {}, "expires_at_days": 30},
        )
        assert mint_r.status_code == 201
        key_id = mint_r.json()["key_id"]

        r = client.get(f"/api/v1/auth/tokens/{key_id}/audit")
        assert r.status_code == 200, r.text
        entries = r.json()
        assert isinstance(entries, list)
        assert len(entries) >= 1
        actions = [e["action"] for e in entries]
        assert "api_token.minted" in actions
        first = entries[0]
        # The wire shape mirrors the SPA's `ApiTokenAuditEntry`.
        assert set(first.keys()) == {"at", "action", "actor_id", "correlation_id"}
        assert first["actor_id"] == seeded_ctx.actor_id

    def test_audit_returns_full_lifecycle_newest_first(
        self,
        client: TestClient,
        seeded_ctx: WorkspaceContext,
    ) -> None:
        mint_r = client.post(
            "/api/v1/auth/tokens",
            json={"label": "lifecycle", "scopes": {}, "expires_at_days": 30},
        )
        assert mint_r.status_code == 201
        key_id = mint_r.json()["key_id"]
        assert client.post(f"/api/v1/auth/tokens/{key_id}/rotate").status_code == 200
        assert client.delete(f"/api/v1/auth/tokens/{key_id}").status_code == 204

        r = client.get(f"/api/v1/auth/tokens/{key_id}/audit")
        assert r.status_code == 200
        actions = [e["action"] for e in r.json()]
        # Newest first.
        assert actions[0] == "api_token.revoked"
        assert "api_token.rotated" in actions
        assert actions[-1] == "api_token.minted"

    def test_audit_unknown_token_returns_empty_list(self, client: TestClient) -> None:
        # The seam is "no events yet, not a 404" so a follow-up ``mint``
        # immediately starts a clean trail. cd-8i9tr chose this shape so
        # the SPA can pre-render the panel header without branching on
        # 404 vs. empty array.
        r = client.get("/api/v1/auth/tokens/01HWA00000000000000000NOPE/audit")
        assert r.status_code == 200
        assert r.json() == []
