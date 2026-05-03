"""End-to-end tenancy middleware integration with real auth.

Drives :class:`~app.tenancy.middleware.WorkspaceContextMiddleware` on
a FastAPI test app backed by the shared ``engine`` + ``db_session``
fixtures (SQLite by default; Postgres when
``CREWDAY_TEST_DB=postgres``). Every test seeds a workspace via the
production-shape :func:`tests.factories.identity.bootstrap_workspace`
helper, issues real sessions via :func:`app.auth.session.issue`, and
mints real API tokens via :func:`app.auth.tokens.mint` — no stub
headers.

Covers (cd-9il acceptance):

* Session cookie end-to-end → ctx bound, ``is_owner`` populated.
* Bearer token end-to-end → ctx bound with token's workspace.
* Cross-tenant 404 — byte-identical envelope + ±5 ms timing band
  across slug-miss vs member-miss (spec §15 "Constant-time
  cross-tenant responses").

See ``docs/specs/15-security-privacy.md`` §"Constant-time cross-tenant
responses"; ``docs/specs/03-auth-and-tokens.md`` §"Sessions" + §"API
tokens"; ``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, delete, or_, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.auth.session import SESSION_COOKIE_NAME
from app.auth.session import issue as issue_session
from app.auth.tokens import mint as mint_token
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import get_current
from app.tenancy.middleware import (
    CORRELATION_ID_HEADER,
    WorkspaceContextMiddleware,
)
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.integration.auth._cleanup import delete_api_tokens_for_scope

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_TENANCY_TEST_EMAILS = (
    "owner-int-session@example.com",
    "tok-int-token@example.com",
    "owner-ct-bodies@example.com",
    "outsider-ct-bodies@example.com",
    "owner-ct-timing@example.com",
    "outsider-ct-timing@example.com",
    "del-archived@example.com",
    "pat-archived@example.com",
    "scoped-archived@example.com",
    "sess-archived@example.com",
    "sess-reinstated@example.com",
    "del-inactive@example.com",
    "pat-inactive@example.com",
)
_TENANCY_TEST_SLUGS = (
    "int-owner",
    "int-token",
    "real-ct-ws",
    "timing-ws",
    "int-del-arch",
    "int-pat-arch",
    "int-scoped-arch",
    "int-sess-arch",
    "int-sess-rs",
    "int-del-inactive",
    "int-pat-inactive",
)


@pytest.fixture
def settings() -> Settings:
    """Settings with the Phase-0 stub OFF so the real resolver runs."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-tenancy-middleware-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
        phase0_stub_enabled=False,
    )


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def sweep_tenancy_auth_rows(
    session_factory: sessionmaker[Session],
) -> Iterator[None]:
    yield
    with session_factory() as s, tenant_agnostic():
        user_ids = tuple(
            s.scalars(select(User.id).where(User.email_lower.in_(_TENANCY_TEST_EMAILS)))
        )
        workspace_ids = tuple(
            s.scalars(
                select(Workspace.id).where(Workspace.slug.in_(_TENANCY_TEST_SLUGS))
            )
        )
        delete_api_tokens_for_scope(s, workspace_ids=workspace_ids, user_ids=user_ids)
        s.execute(
            delete(SessionRow).where(
                or_(
                    SessionRow.user_id.in_(user_ids),
                    SessionRow.workspace_id.in_(workspace_ids),
                )
            )
        )
        s.execute(
            delete(AuditLog).where(
                or_(
                    AuditLog.workspace_id.in_(workspace_ids),
                    AuditLog.actor_id.in_(user_ids),
                )
            )
        )
        s.execute(
            delete(PermissionGroupMember).where(
                PermissionGroupMember.workspace_id.in_(workspace_ids)
            )
        )
        s.execute(
            delete(RoleGrant).where(
                or_(
                    RoleGrant.workspace_id.in_(workspace_ids),
                    RoleGrant.user_id.in_(user_ids),
                )
            )
        )
        s.execute(
            delete(PermissionGroup).where(
                PermissionGroup.workspace_id.in_(workspace_ids)
            )
        )
        s.execute(
            delete(UserWorkspace).where(
                or_(
                    UserWorkspace.workspace_id.in_(workspace_ids),
                    UserWorkspace.user_id.in_(user_ids),
                )
            )
        )
        s.execute(delete(Workspace).where(Workspace.id.in_(workspace_ids)))
        s.execute(delete(User).where(User.id.in_(user_ids)))
        s.commit()


@pytest.fixture
def wire_default_uow(
    engine: Engine,
    session_factory: sessionmaker[Session],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Redirect :func:`make_uow` to the shared test engine.

    The middleware opens a fresh UoW per request via
    :func:`app.adapters.db.session.make_uow`; we swap the module-level
    defaults to land on the test DB. Also monkeypatches the
    middleware's ``get_settings`` to return the stub-off fixture so
    no test implicitly inherits a cached default.
    """
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
    """FastAPI app with the middleware and a ``ping`` route."""
    app = FastAPI()
    app.add_middleware(WorkspaceContextMiddleware)

    @app.get("/w/{slug}/api/v1/ping")
    def scoped_ping(slug: str) -> dict[str, object]:
        ctx = get_current()
        if ctx is None:
            return {"bound": False}
        return {
            "bound": True,
            "workspace_id": ctx.workspace_id,
            "workspace_slug": ctx.workspace_slug,
            "actor_id": ctx.actor_id,
            "actor_kind": ctx.actor_kind,
            "actor_grant_role": ctx.actor_grant_role,
            "actor_was_owner_member": ctx.actor_was_owner_member,
        }

    return app


def _seed(
    session_factory: sessionmaker[Session], *, slug: str, email: str
) -> tuple[str, str]:
    """Seed one user + one workspace + owners group.

    Returns ``(workspace_id, user_id)``.
    """
    with session_factory() as s:
        user = bootstrap_user(s, email=email, display_name=email)
        ws = bootstrap_workspace(s, slug=slug, name=slug.title(), owner_user_id=user.id)
        s.commit()
        return ws.id, user.id


class TestSessionEndToEnd:
    """A full session-cookie roundtrip against the real middleware."""

    def test_session_resolves_owner_context(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        ws_id, user_id = _seed(
            session_factory,
            slug="int-owner",
            email="owner-int-session@example.com",
        )
        with session_factory() as s:
            # Match the TestClient's default ``User-Agent: testclient``
            # header so cd-geqp's fingerprint gate (now wired through
            # the middleware) accepts the cookie on the inbound request.
            issued = issue_session(
                s,
                user_id=user_id,
                has_owner_grant=True,
                ua="testclient",
                ip="127.0.0.1",
                accept_language="",
                now=datetime.now(UTC),
                settings=settings,
            )
            s.commit()

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            client.cookies.set(SESSION_COOKIE_NAME, issued.cookie_value)
            response = client.get("/w/int-owner/api/v1/ping")

        assert response.status_code == 200
        body = response.json()
        assert body["bound"] is True
        assert body["workspace_id"] == ws_id
        assert body["workspace_slug"] == "int-owner"
        assert body["actor_id"] == user_id
        assert body["actor_was_owner_member"] is True
        assert body["actor_grant_role"] == "manager"
        assert CORRELATION_ID_HEADER in response.headers


class TestBearerTokenEndToEnd:
    def test_token_resolves_context(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        ws_id, user_id = _seed(
            session_factory, slug="int-token", email="tok-int-token@example.com"
        )
        with session_factory() as s:
            ctx = WorkspaceContext(
                workspace_id=ws_id,
                workspace_slug="int-token",
                actor_id=user_id,
                actor_kind="user",
                actor_grant_role="manager",
                actor_was_owner_member=True,
                audit_correlation_id=new_ulid(),
            )
            minted = mint_token(
                s,
                ctx,
                user_id=user_id,
                label="int",
                scopes={"tasks.read": True},
                expires_at=None,
                now=datetime.now(UTC),
            )
            s.commit()

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/w/int-token/api/v1/ping",
                headers={"Authorization": f"Bearer {minted.token}"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["bound"] is True
        assert body["workspace_id"] == ws_id
        assert body["actor_id"] == user_id
        assert body["actor_was_owner_member"] is True


class TestCrossTenantConstantTime:
    """§15 constant-time cross-tenant responses."""

    def test_slug_miss_and_member_miss_bodies_match(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        _ws_id, _owner_id = _seed(
            session_factory, slug="real-ct-ws", email="owner-ct-bodies@example.com"
        )
        # The "outsider" is a real logged-in user who is NOT a member
        # of ``real-ct-ws`` — exactly the cross-tenant probe shape §15
        # pins down.
        with session_factory() as s:
            outsider = bootstrap_user(
                s,
                email="outsider-ct-bodies@example.com",
                display_name="Outsider",
            )
            issued = issue_session(
                s,
                user_id=outsider.id,
                has_owner_grant=False,
                ua="curl",
                ip="127.0.0.1",
                now=datetime.now(UTC),
                settings=settings,
            )
            s.commit()

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            client.cookies.set(SESSION_COOKIE_NAME, issued.cookie_value)
            slug_miss = client.get("/w/never-existed/api/v1/ping")
            member_miss = client.get("/w/real-ct-ws/api/v1/ping")

        assert slug_miss.status_code == 404
        assert member_miss.status_code == 404
        # §15 — both branches must emit the same canonical RFC 7807
        # envelope. ``instance`` reflects the URL the caller chose
        # (deterministic from input, not DB state) and varies across
        # these two probes; every other field must match byte-for-byte.
        slug_body = slug_miss.json()
        member_body = member_miss.json()
        assert slug_body.pop("instance") == "/w/never-existed/api/v1/ping"
        assert member_body.pop("instance") == "/w/real-ct-ws/api/v1/ping"
        assert slug_body == member_body
        assert slug_body == {
            "type": "https://crewday.dev/errors/not_found",
            "title": "Not found",
            "status": 404,
        }

    def test_slug_miss_and_member_miss_timings_overlap(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        """Timings fall in the same rough wall-clock band.

        Spec §15 pins ±5 ms on a steady-load harness; here we run both
        branches N times and assert that the means differ by less than
        a generous ceiling. This is a smoke test for the dummy-read
        equaliser — not a rigorous statistical proof, but enough to
        catch a regression that removes the dummy read entirely.
        """
        _ws_id, _owner_id = _seed(
            session_factory, slug="timing-ws", email="owner-ct-timing@example.com"
        )
        with session_factory() as s:
            outsider = bootstrap_user(
                s,
                email="outsider-ct-timing@example.com",
                display_name="TO",
            )
            issued = issue_session(
                s,
                user_id=outsider.id,
                has_owner_grant=False,
                ua="curl",
                ip="127.0.0.1",
                now=datetime.now(UTC),
                settings=settings,
            )
            s.commit()

        app = _build_app()
        samples = 10
        slug_times: list[float] = []
        member_times: list[float] = []
        with TestClient(app, raise_server_exceptions=False) as client:
            # Warmup so lazy import + sqlite page cache don't skew the
            # first sample of whichever branch runs first.
            client.cookies.set(SESSION_COOKIE_NAME, issued.cookie_value)
            client.get("/w/warmup/api/v1/ping")
            client.get("/w/timing-ws/api/v1/ping")
            for _ in range(samples):
                t0 = time.perf_counter()
                client.get("/w/never-exists/api/v1/ping")
                slug_times.append(time.perf_counter() - t0)

                t0 = time.perf_counter()
                client.get("/w/timing-ws/api/v1/ping")
                member_times.append(time.perf_counter() - t0)

        mean_slug = sum(slug_times) / samples
        mean_member = sum(member_times) / samples
        # Generous bound — the middleware stack + test client
        # overhead dwarfs the DB read cost in this smoke test. What
        # we're guarding against is the pathological "slug miss
        # skipped the DB entirely" regression, which would produce a
        # >10x gap on any backend. +/-50 ms is comfortable headroom on
        # CI noise; the real SLO (±5 ms under steady load) lives on
        # the §17 tenant-isolation suite.
        delta = abs(mean_slug - mean_member)
        assert delta < 0.050, (
            f"timing branches diverged: slug_miss={mean_slug:.4f}s, "
            f"member_miss={mean_member:.4f}s, delta={delta:.4f}s"
        )


class TestArchivedDelegatingSubjectUser:
    """cd-et6y — middleware emits 401 with a typed error code.

    §03 "Delegated tokens" / "Personal access tokens": when the
    delegating / subject user is archived, the bearer-token request
    returns ``401`` with a typed code, NOT the constant-time 404
    that "unknown slug / not a member" branches collapse into.
    """

    def test_delegated_token_archived_user_returns_401(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        """Archiving the delegating user gates the delegated token at 401."""
        ws_id, user_id = _seed(
            session_factory,
            slug="int-del-arch",
            email="del-archived@example.com",
        )
        with session_factory() as s:
            ctx = WorkspaceContext(
                workspace_id=ws_id,
                workspace_slug="int-del-arch",
                actor_id=user_id,
                actor_kind="user",
                actor_grant_role="manager",
                actor_was_owner_member=True,
                audit_correlation_id=new_ulid(),
            )
            minted = mint_token(
                s,
                ctx,
                user_id=user_id,
                label="del-int",
                scopes={},
                expires_at=None,
                kind="delegated",
                delegate_for_user_id=user_id,
                now=datetime.now(UTC),
            )
            s.commit()

        # Archive the delegating user out-of-band.
        with session_factory() as s:
            from app.adapters.db.identity.models import User
            from app.tenancy import tenant_agnostic

            with tenant_agnostic():
                row = s.get(User, user_id)
                assert row is not None
                row.archived_at = _PINNED
                s.commit()

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/w/int-del-arch/api/v1/ping",
                headers={"Authorization": f"Bearer {minted.token}"},
            )
        # 401 with the typed error code — distinct from the 404
        # "unknown / not-a-member" envelope.
        assert response.status_code == 401
        assert response.json() == {
            "error": "delegating_user_archived",
            "detail": None,
        }
        assert CORRELATION_ID_HEADER in response.headers

    def test_personal_token_archived_user_returns_401(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        """Archiving the subject user gates the PAT at 401.

        PATs carry ``workspace_id = NULL`` so the bearer-token + slug
        URL must point at a workspace the user IS a member of for the
        404 collision check to be meaningful — otherwise membership
        miss would already 404 for unrelated reasons. Mint the PAT
        for the same user that owns ``int-pat-arch``.
        """
        _ws_id, user_id = _seed(
            session_factory,
            slug="int-pat-arch",
            email="pat-archived@example.com",
        )
        with session_factory() as s:
            minted = mint_token(
                s,
                None,
                user_id=user_id,
                label="pat-int",
                scopes={"me.tasks:read": True},
                expires_at=None,
                kind="personal",
                subject_user_id=user_id,
                now=datetime.now(UTC),
            )
            s.commit()

        with session_factory() as s:
            from app.adapters.db.identity.models import User
            from app.tenancy import tenant_agnostic

            with tenant_agnostic():
                row = s.get(User, user_id)
                assert row is not None
                row.archived_at = _PINNED
                s.commit()

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/w/int-pat-arch/api/v1/ping",
                headers={"Authorization": f"Bearer {minted.token}"},
            )
        assert response.status_code == 401
        assert response.json() == {
            "error": "subject_user_archived",
            "detail": None,
        }
        assert CORRELATION_ID_HEADER in response.headers

    def test_scoped_token_unaffected_by_user_archive(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        """Scoped tokens still resolve when the minting user is archived.

        Scoped tokens carry their authority on ``scope_json``, not on
        a delegating user — archive must not retroactively gate them
        (revocation is the only valid kill path). Asserts the
        middleware does not over-fire 401 on this path.
        """
        ws_id, user_id = _seed(
            session_factory,
            slug="int-scoped-arch",
            email="scoped-archived@example.com",
        )
        with session_factory() as s:
            ctx = WorkspaceContext(
                workspace_id=ws_id,
                workspace_slug="int-scoped-arch",
                actor_id=user_id,
                actor_kind="user",
                actor_grant_role="manager",
                actor_was_owner_member=True,
                audit_correlation_id=new_ulid(),
            )
            minted = mint_token(
                s,
                ctx,
                user_id=user_id,
                label="scoped-int",
                scopes={"tasks:read": True},
                expires_at=None,
                now=datetime.now(UTC),
            )
            s.commit()

        with session_factory() as s:
            from app.adapters.db.identity.models import User
            from app.tenancy import tenant_agnostic

            with tenant_agnostic():
                row = s.get(User, user_id)
                assert row is not None
                row.archived_at = _PINNED
                s.commit()

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/w/int-scoped-arch/api/v1/ping",
                headers={"Authorization": f"Bearer {minted.token}"},
            )
        # The scoped token still resolves a ctx — archive does not
        # gate scoped tokens (the user-archive cascade for scoped
        # tokens lives at user.archive time, not at verify time).
        assert response.status_code == 200
        body = response.json()
        assert body["bound"] is True
        assert body["workspace_id"] == ws_id


class TestArchivedSessionUser:
    """cd-uceg — middleware emits 401 with ``subject_user_archived``.

    §03 "Sessions" mirrors the bearer-token archive gates above for
    cookie-session traffic: a still-live session whose owning user
    has ``archived_at IS NOT NULL`` returns ``401`` with the typed
    wire code rather than the constant-time 404 the unknown-cookie
    path collapses into.
    """

    def test_session_archived_user_returns_401(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        """Archive flips a live session's validate to 401 ``subject_user_archived``."""
        _ws_id, user_id = _seed(
            session_factory,
            slug="int-sess-arch",
            email="sess-archived@example.com",
        )
        with session_factory() as s:
            issued = issue_session(
                s,
                user_id=user_id,
                has_owner_grant=True,
                ua="testclient",
                ip="127.0.0.1",
                accept_language="",
                now=datetime.now(UTC),
                settings=settings,
            )
            s.commit()

        # Archive the session-owning user out-of-band — mirrors the
        # privacy-purge / future deployment-archive flow.
        with session_factory() as s:
            from app.adapters.db.identity.models import User
            from app.tenancy import tenant_agnostic

            with tenant_agnostic():
                row = s.get(User, user_id)
                assert row is not None
                row.archived_at = _PINNED
                s.commit()

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            client.cookies.set(SESSION_COOKIE_NAME, issued.cookie_value)
            response = client.get("/w/int-sess-arch/api/v1/ping")

        assert response.status_code == 401
        assert response.json() == {
            "error": "subject_user_archived",
            "detail": None,
        }
        assert CORRELATION_ID_HEADER in response.headers

    def test_session_clearing_archive_re_admits_request(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        """Reinstate (``archived_at`` → NULL) re-admits the same cookie.

        §03 "Personal access tokens" rule mirrored to the cookie
        side: the archive flag is reversible; sessions resume on the
        next request without re-issuing.
        """
        ws_id, user_id = _seed(
            session_factory,
            slug="int-sess-rs",
            email="sess-reinstated@example.com",
        )
        with session_factory() as s:
            issued = issue_session(
                s,
                user_id=user_id,
                has_owner_grant=True,
                ua="testclient",
                ip="127.0.0.1",
                accept_language="",
                now=datetime.now(UTC),
                settings=settings,
            )
            s.commit()

        with session_factory() as s:
            from app.adapters.db.identity.models import User
            from app.tenancy import tenant_agnostic

            with tenant_agnostic():
                row = s.get(User, user_id)
                assert row is not None
                row.archived_at = _PINNED
                s.commit()

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            client.cookies.set(SESSION_COOKIE_NAME, issued.cookie_value)
            blocked = client.get("/w/int-sess-rs/api/v1/ping")
            assert blocked.status_code == 401

            with session_factory() as s, tenant_agnostic():
                row = s.get(User, user_id)
                assert row is not None
                row.archived_at = None
                s.commit()

            response = client.get("/w/int-sess-rs/api/v1/ping")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["bound"] is True
            assert body["workspace_id"] == ws_id


def _soft_revoke_all_grants(
    session_factory: sessionmaker[Session], *, user_id: str, workspace_id: str | None
) -> None:
    """Soft-retire every live ``role_grant`` for ``user_id``.

    cd-ljvs integration helper. Mirrors what ``cd-x1xh``'s
    domain-side ``revoke()`` writes (``revoked_at`` + ``ended_on``)
    without going through the owner-authority gate — the integration
    test only cares about the resulting "no live grants" state.
    Pass ``workspace_id=None`` to retire across every workspace
    (PAT-shape) or a specific id to retire only one workspace's
    grants (delegated-shape).
    """
    from app.adapters.db.authz.models import RoleGrant
    from app.tenancy import tenant_agnostic

    with session_factory() as s, tenant_agnostic():
        stmt = s.query(RoleGrant).filter(
            RoleGrant.user_id == user_id,
            RoleGrant.revoked_at.is_(None),
        )
        if workspace_id is not None:
            stmt = stmt.filter(RoleGrant.workspace_id == workspace_id)
        now = datetime.now(UTC)
        for row in stmt.all():
            row.revoked_at = now
            row.ended_on = now.date()
        s.commit()


class TestInactiveDelegatingSubjectUser:
    """cd-ljvs — middleware emits 401 with the inactive-shape error code.

    §03 "Delegated tokens" / "Personal access tokens": when the
    delegating / subject user has lost every non-revoked grant
    (cd-x1xh soft-retired every row), the bearer-token request
    returns ``401`` with ``delegating_user_inactive`` /
    ``subject_user_inactive`` — the agent gets a clear "grant a
    fresh role" signal, distinct from the archived-user gate
    above. Order: archive-first, then inactive — both gates would
    fire when archived AND no grants, archive wins.
    """

    def test_delegated_token_inactive_user_returns_401(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        """Soft-retiring every grant in the workspace gates the token at 401."""
        ws_id, user_id = _seed(
            session_factory,
            slug="int-del-inactive",
            email="del-inactive@example.com",
        )
        with session_factory() as s:
            ctx = WorkspaceContext(
                workspace_id=ws_id,
                workspace_slug="int-del-inactive",
                actor_id=user_id,
                actor_kind="user",
                actor_grant_role="manager",
                actor_was_owner_member=True,
                audit_correlation_id=new_ulid(),
            )
            minted = mint_token(
                s,
                ctx,
                user_id=user_id,
                label="del-int-inactive",
                scopes={},
                expires_at=None,
                kind="delegated",
                delegate_for_user_id=user_id,
                now=datetime.now(UTC),
            )
            s.commit()

        # Soft-retire every live grant for the delegating user in
        # the token's workspace. Mirrors cd-x1xh's domain revoke
        # without going through the owner-authority gate.
        _soft_revoke_all_grants(session_factory, user_id=user_id, workspace_id=ws_id)

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/w/int-del-inactive/api/v1/ping",
                headers={"Authorization": f"Bearer {minted.token}"},
            )
        assert response.status_code == 401
        assert response.json() == {
            "error": "delegating_user_inactive",
            "detail": None,
        }
        assert CORRELATION_ID_HEADER in response.headers

    def test_personal_token_inactive_user_returns_401(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        """Soft-retiring every grant across every workspace gates the PAT at 401."""
        _ws_id, user_id = _seed(
            session_factory,
            slug="int-pat-inactive",
            email="pat-inactive@example.com",
        )
        with session_factory() as s:
            minted = mint_token(
                s,
                None,
                user_id=user_id,
                label="pat-int-inactive",
                scopes={"me.tasks:read": True},
                expires_at=None,
                kind="personal",
                subject_user_id=user_id,
                now=datetime.now(UTC),
            )
            s.commit()

        # PAT check is workspace-agnostic — retire across every
        # workspace.
        _soft_revoke_all_grants(session_factory, user_id=user_id, workspace_id=None)

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/w/int-pat-inactive/api/v1/ping",
                headers={"Authorization": f"Bearer {minted.token}"},
            )
        assert response.status_code == 401
        assert response.json() == {
            "error": "subject_user_inactive",
            "detail": None,
        }
        assert CORRELATION_ID_HEADER in response.headers
