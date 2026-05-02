"""HTTP-level coverage for ``POST /w/<slug>/api/v1/users/invite`` (cd-m966k).

The integration suite at ``tests/integration/identity/test_membership.py``
drives :func:`app.domain.identity.membership.invite` directly; this
file exercises the thin HTTP layer in :mod:`app.api.v1.users` so the
exception mapping at the router boundary keeps its shape under
regression. Specifically, cd-m966k pinned that the magic-link
:class:`~app.auth._throttle.RateLimited` exception (raised under the
hood when the per-IP / per-email request budget is exhausted) maps to
**429 ``rate_limited``** rather than bubbling up as a 500.

See ``docs/specs/12-rest-api.md`` §"Errors" and
``docs/specs/03-auth-and-tokens.md`` §"Rate limiting and abuse
controls".
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import Workspace
from app.api.deps import current_workspace_context
from app.api.deps import db_session as _db_session_dep
from app.api.v1.users import build_users_router
from app.auth._throttle import Throttle
from app.config import Settings
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests._fakes.mailer import InMemoryMailer
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
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture(autouse=True)
def _redirect_default_uow(
    engine: Engine,
    session_factory: sessionmaker[Session],
) -> Iterator[None]:
    """Bind :func:`app.adapters.db.session.make_uow` to the test engine.

    ``post_invite`` opens its own ``with make_uow() as session:`` block
    (cd-9slq outbox ordering) rather than using the ``db_session``
    dependency. ``make_uow`` reads ``_default_sessionmaker_`` at call
    time, so we redirect that module-level state here for the duration
    of the test and restore on teardown — same shape as
    ``tests/integration/auth/test_outbox_ordering.py``.
    """
    import app.adapters.db.session as session_module

    original_engine = session_module._default_engine
    original_factory = session_module._default_sessionmaker_
    session_module._default_engine = engine
    session_module._default_sessionmaker_ = session_factory
    try:
        yield
    finally:
        session_module._default_engine = original_engine
        session_module._default_sessionmaker_ = original_factory


@pytest.fixture
def settings() -> Settings:
    """Pin a fixed ``root_key`` + ``public_url`` so magic-link wires work."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-users-invite-route-root-key"),
        public_url="https://test.crew.day",
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def seeded(
    session_factory: sessionmaker[Session],
) -> Iterator[WorkspaceContext]:
    """Seed an owner workspace and yield a manager-grade ctx."""
    tag = new_ulid()[-8:].lower()
    slug = f"inv-{tag}"
    with session_factory() as s:
        owner = bootstrap_user(
            s, email=f"owner-{tag}@example.com", display_name="Owner"
        )
        ws = bootstrap_workspace(
            s,
            slug=slug,
            name="Invite Route",
            owner_user_id=owner.id,
        )
        s.commit()
        owner_id, ws_id, ws_slug = owner.id, ws.id, ws.slug

    ctx = build_workspace_context(
        workspace_id=ws_id,
        workspace_slug=ws_slug,
        actor_id=owner_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    yield ctx

    # The integration ``engine`` fixture is session-scoped, so each
    # POST /users/invite leaves Invite + MagicLinkNonce + AuditLog rows
    # behind. Sweep the rows this test seeded so siblings sharing the
    # same engine see a clean slate. Mirrors the cleanup shape in
    # :func:`tests.integration.auth.test_outbox_ordering.factory`.
    from sqlalchemy import select

    from app.adapters.db.audit.models import AuditLog
    from app.adapters.db.identity.models import Invite, MagicLinkNonce

    with session_factory() as s, tenant_agnostic():
        invite_rows = (
            s.scalars(select(Invite).where(Invite.workspace_id == ws_id))
        ).all()
        invite_ids = {inv.id for inv in invite_rows}
        if invite_ids:
            for nonce in s.scalars(
                select(MagicLinkNonce).where(
                    MagicLinkNonce.subject_id.in_(invite_ids),
                    MagicLinkNonce.purpose == "grant_invite",
                )
            ).all():
                s.delete(nonce)
        for invite in invite_rows:
            s.delete(invite)
        for audit in s.scalars(
            select(AuditLog).where(AuditLog.workspace_id == ws_id)
        ).all():
            s.delete(audit)
        ws_row = s.get(Workspace, ws_id)
        if ws_row is not None:
            s.delete(ws_row)
        # The invite path resolves-or-creates an invitee user row keyed
        # off the email. Delete any users this test seeded under the
        # ``-{tag}@example.com`` suffix so a re-run with the same tag
        # (rare but possible if ULIDs collide on lower 8 chars) doesn't
        # inherit a stale row.
        for u in s.scalars(
            select(User).where(User.email_lower.like(f"%-{tag}@example.com"))
        ).all():
            s.delete(u)
        owner_row = s.get(User, owner_id)
        if owner_row is not None:
            s.delete(owner_row)
        s.commit()


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def mailer() -> InMemoryMailer:
    return InMemoryMailer()


@pytest.fixture
def client(
    session_factory: sessionmaker[Session],
    seeded: WorkspaceContext,
    mailer: InMemoryMailer,
    throttle: Throttle,
    settings: Settings,
) -> Iterator[TestClient]:
    """``TestClient`` mounted on a freshly built users router.

    Production runs :class:`app.tenancy.middleware.TenancyMiddleware`
    in front of every workspace route to resolve + bind the workspace
    context. We don't need it here: ``post_invite`` reads ``ctx`` only
    via the :func:`current_workspace_context` DI (overridden below)
    and the ORM queries on the invite path are either tenant-agnostic
    (``_resolve_inviter_display_name``) or hit non-tenant-scoped tables
    (``_resolve_workspace_name`` against ``Workspace``). Same harness
    shape as :class:`tests.integration.auth.test_outbox_ordering.\
TestInviteCommitFailureNoEmailLeak`.
    """
    ctx = seeded
    app = FastAPI()
    app.include_router(
        build_users_router(
            mailer=mailer,
            throttle=throttle,
            settings=settings,
            base_url=settings.public_url,
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

    def _ctx() -> WorkspaceContext:
        return ctx

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = _ctx

    with TestClient(app) as c:
        yield c


def _invite_payload(ctx: WorkspaceContext, *, email: str) -> dict[str, object]:
    """Return a minimal valid ``POST /users/invite`` body."""
    return {
        "email": email,
        "display_name": "Invitee",
        "grants": [
            {
                "scope_kind": "workspace",
                "scope_id": ctx.workspace_id,
                "grant_role": "worker",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRateLimit:
    """Magic-link throttle on ``POST /users/invite`` maps to 429 (cd-m966k)."""

    def test_rate_limit_returns_429_rate_limited(
        self,
        client: TestClient,
        seeded: WorkspaceContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Saturating the per-IP request bucket yields 429 ``rate_limited``.

        The throttle is hardcoded to 5 hits / 60 s per ``(ip, email_hash)``
        key in :mod:`app.auth._throttle`; we monkey-patch the module-
        level :data:`_REQUEST_LIMIT` down to 1 so the second invite
        trips the limiter without driving five real invites through the
        DB. The throttle docstring documents this exact escape hatch:
        the constants are ``Final`` "so tests can monkey-patch them to
        tight values without re-plumbing the service".

        Assertions pin the wire shape — 429 status, ``error =
        rate_limited`` envelope — that the SPA + e2e suites read.
        """
        import app.auth._throttle as throttle_mod

        # Tight cap so one call exhausts the bucket; the second hits 429.
        monkeypatch.setattr(throttle_mod, "_REQUEST_LIMIT", 1)

        ctx = seeded
        body = _invite_payload(ctx, email="rate-limited@example.com")

        # First call mints the invite + magic link → 201.
        first = client.post("/api/v1/users/invite", json=body)
        assert first.status_code == 201, first.text

        # Second call (same email + same source IP) trips the per-IP
        # bucket inside ``magic_link.request_link`` and surfaces as
        # :class:`RateLimited`. The router maps it to 429.
        second = client.post("/api/v1/users/invite", json=body)
        assert second.status_code == 429, second.text
        detail = second.json().get("detail")
        assert detail == {"error": "rate_limited"}, second.text


class TestErrorMapping:
    """Direct unit coverage for :func:`_http_for_invite`'s branches."""

    def test_invite_body_invalid_maps_to_422(self) -> None:
        """``InviteBodyInvalid`` keeps its 422 ``invalid_body`` envelope."""
        from app.api.v1.users import _http_for_invite
        from app.domain.identity import membership

        http = _http_for_invite(membership.InviteBodyInvalid("missing email"))
        assert http.status_code == 422
        assert isinstance(http.detail, dict)
        assert http.detail["error"] == "invalid_body"

    def test_rate_limited_maps_to_429(self) -> None:
        """``RateLimited`` from the magic-link throttle maps to 429."""
        from app.api.v1.users import _http_for_invite
        from app.auth._throttle import RateLimited

        http = _http_for_invite(RateLimited("per-IP request budget exceeded"))
        assert http.status_code == 429
        assert http.detail == {"error": "rate_limited"}
        # ``RateLimited`` carries no retry-after hint today, so the
        # router does not fabricate one. The OpenAPI schema's global
        # ``Retry-After`` documentation still applies for the day a
        # future variant lands a real hint.
        assert http.headers in (None, {})

    def test_unknown_exception_falls_back_to_500(self) -> None:
        """An unmapped exception still routes through the 500 fallback."""
        from app.api.v1.users import _http_for_invite

        http = _http_for_invite(RuntimeError("something exploded"))
        assert http.status_code == 500
        assert http.detail == {"error": "internal"}
