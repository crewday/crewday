"""Unit tests for :mod:`app.auth.tokens` and :mod:`app.api.v1.auth.tokens`.

Covers the cd-c91 acceptance surface end-to-end at the domain-service
level plus the thin HTTP router on top:

* Mint — happy path, cap, argon2id shape, prefix derivation, one-
  time plaintext, audit row content, scope dict round-trip.
* Verify — happy path, malformed token, unknown ``key_id``, expired,
  revoked, bad secret, ``last_used_at`` debouncing.
* Revoke — happy path, idempotent double-revoke, cross-workspace
  guard, 404 for unknown id.
* List — returns both active and revoked, never the hash, most-recent
  first.

Runs against an in-memory SQLite engine with :class:`Base.metadata`
schema. argon2id hashing is real (no stub) — the test suite exercises
the exact same hasher the production path uses so we don't skip
surface the real behaviour wraps.

See ``docs/specs/03-auth-and-tokens.md`` §"API tokens" and
``docs/specs/15-security-privacy.md`` §"Token hashing".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import ApiToken
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.auth.audit import AGNOSTIC_WORKSPACE_ID
from app.auth.tokens import (
    DelegatingUserArchived,
    DelegatingUserInactive,
    InvalidToken,
    MintedToken,
    SubjectUserArchived,
    SubjectUserInactive,
    TokenExpired,
    TokenKind,
    TokenKindInvalid,
    TokenRevoked,
    TokenShapeError,
    TooManyPersonalTokens,
    TooManyTokens,
    TooManyWorkspaceTokens,
    list_audit,
    list_personal_tokens,
    list_tokens,
    mint,
    revoke,
    revoke_personal,
    rotate,
    verify,
)
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.redact import redact
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

_PINNED = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


def _as_utc(value: datetime) -> datetime:
    """Normalise a SQLite-roundtripped datetime to aware UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def workspace(db_session: Session) -> Workspace:
    """Seed a workspace row for the token FK target."""
    ws_id = new_ulid()
    ws = Workspace(
        id=ws_id,
        slug="ws-tokens",
        name="Tokens WS",
        plan="free",
        quota_json={},
        created_at=_PINNED,
    )
    with tenant_agnostic():
        db_session.add(ws)
        db_session.flush()
    return ws


@pytest.fixture
def user(db_session: Session) -> object:
    """Seed a user row for the token FK target."""
    return bootstrap_user(db_session, email="tok@example.com", display_name="Tok User")


@pytest.fixture
def ctx(workspace: Workspace, user: object) -> WorkspaceContext:
    """Return a :class:`WorkspaceContext` scoped to the seeded workspace + user."""
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=user.id,  # type: ignore[attr-defined]
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )


def _seed_role_grant(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
    grant_role: str = "worker",
    revoked_at: datetime | None = None,
) -> str:
    """Seed one :class:`RoleGrant` row; return its id.

    cd-ljvs makes the verifier consult ``role_grant.revoked_at IS
    NULL`` for delegated / personal tokens — every verify-time test
    on those kinds needs at least one live grant for the principal.
    The helper takes ``revoked_at`` as a keyword so soft-retired
    grants are also expressible (the inactive-gate tests below
    seed both shapes to assert the predicate honours the filter).
    """
    grant_id = new_ulid()
    with tenant_agnostic():
        session.add(
            RoleGrant(
                id=grant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                grant_role=grant_role,
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
                revoked_at=revoked_at,
            )
        )
        session.flush()
    return grant_id


def _ctx_for_actor(workspace: Workspace, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )


def _seed_api_token(
    session: Session,
    *,
    user_id: str,
    workspace_id: str | None,
    kind: TokenKind = "scoped",
    expires_at: datetime | None = _PINNED + timedelta(days=90),
    revoked_at: datetime | None = None,
    delegate_for_user_id: str | None = None,
    subject_user_id: str | None = None,
) -> str:
    token_id = new_ulid()
    scope_json = {"me.tasks:read": True} if kind == "personal" else {}
    with tenant_agnostic():
        session.add(
            ApiToken(
                id=token_id,
                user_id=user_id,
                workspace_id=workspace_id,
                kind=kind,
                delegate_for_user_id=delegate_for_user_id,
                subject_user_id=subject_user_id,
                label=f"seed-{token_id}",
                scope_json=scope_json,
                prefix=token_id[:8],
                hash=f"seed-hash-{token_id}",
                expires_at=expires_at,
                last_used_at=None,
                revoked_at=revoked_at,
                created_at=_PINNED,
            )
        )
        session.flush()
    return token_id


@pytest.fixture
def live_grant(workspace: Workspace, user: object, db_session: Session) -> str:
    """Seed one live ``role_grant`` for ``user`` in ``workspace``.

    cd-ljvs: every test that exercises :func:`verify` against a
    delegated or personal token needs at least one live grant for
    the principal — without it the new liveness gate raises
    :class:`DelegatingUserInactive` / :class:`SubjectUserInactive`
    before returning. Returning the grant id keeps test bodies
    free to revoke / mutate the row inline (the inactive-gate
    tests below).
    """
    return _seed_role_grant(
        db_session,
        workspace_id=workspace.id,
        user_id=user.id,  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# ``mint``
# ---------------------------------------------------------------------------


class TestMint:
    """``mint`` returns plaintext once, persists the argon2id hash + audit."""

    def test_happy_path_returns_mip_prefixed_token(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result: MintedToken = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="hermes",
            scopes={"tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        # ``mip_<key_id>_<secret>`` — 4 + 26 + 1 + 52 = 83 chars.
        assert result.token.startswith("mip_")
        _mip, key_id, secret = result.token.split("_", 2)
        assert key_id == result.key_id
        assert len(key_id) == 26  # ULID
        assert len(secret) == 52  # base32(32 bytes) with padding stripped
        assert result.prefix == secret[:8]
        assert result.expires_at == _PINNED + timedelta(days=90)

    def test_row_carries_argon2id_hash(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """Stored ``hash`` is the argon2id PHC string, never plaintext."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="hash-check",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            now=_PINNED,
        )
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        # PHC string shape: ``$argon2id$v=19$m=...,t=...,p=...$<salt>$<digest>``
        assert row.hash.startswith("$argon2id$")
        # Plaintext secret must never appear in the stored hash.
        _mip, _key_id, secret = result.token.split("_", 2)
        assert secret not in row.hash

    def test_prefix_matches_first_8_chars_of_secret(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="prefix-check",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        _mip, _key_id, secret = result.token.split("_", 2)
        assert row.prefix == secret[:8]
        assert row.prefix == result.prefix

    def test_scopes_round_trip(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        scopes = {"tasks:read": True, "stays:read": True}
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="scopes",
            scopes=scopes,
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.scope_json == scopes

    def test_audit_row_carries_no_plaintext(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """The mint audit must contain prefix + label but NEVER the plaintext."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="audit-check",
            scopes={"tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "api_token.minted")
        ).all()
        assert len(audits) == 1
        row = audits[0]
        assert row.entity_kind == "api_token"
        assert row.entity_id == result.key_id
        assert isinstance(row.diff, dict)
        assert row.diff["prefix"] == result.prefix
        assert row.diff["label"] == "audit-check"
        assert row.diff["scopes"] == ["tasks:read"]
        # Plaintext token must not appear anywhere in the diff.
        _mip, _key_id, secret = result.token.split("_", 2)
        serialised = repr(row.diff)
        assert secret not in serialised
        assert result.token not in serialised

    def test_too_many_tokens_raises_on_sixth_mint(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """A 6th live token on the same user + workspace raises TooManyTokens."""
        for i in range(5):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label=f"t-{i}",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )
        with pytest.raises(TooManyTokens):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="6th",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )

    def test_too_many_workspace_tokens_raises_on_51st_mint(
        self, db_session: Session, workspace: Workspace
    ) -> None:
        """A 51st live scoped/delegated token in one workspace raises."""
        for user_index in range(10):
            seeded_user = bootstrap_user(
                db_session,
                email=f"tok-cap-{user_index}@example.com",
                display_name=f"Token Cap {user_index}",
            )
            seeded_ctx = _ctx_for_actor(workspace, seeded_user.id)
            for token_index in range(5):
                kind: TokenKind = "delegated" if token_index == 4 else "scoped"
                mint(
                    db_session,
                    seeded_ctx,
                    user_id=seeded_user.id,
                    label=f"u{user_index}-t{token_index}",
                    scopes={},
                    expires_at=_PINNED + timedelta(days=90),
                    kind=kind,
                    delegate_for_user_id=seeded_user.id
                    if kind == "delegated"
                    else None,
                    now=_PINNED,
                )

        overflow_user = bootstrap_user(
            db_session,
            email="tok-cap-overflow@example.com",
            display_name="Token Cap Overflow",
        )
        overflow_ctx = _ctx_for_actor(workspace, overflow_user.id)
        with pytest.raises(TooManyWorkspaceTokens):
            mint(
                db_session,
                overflow_ctx,
                user_id=overflow_user.id,
                label="51st",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )

    def test_personal_tokens_do_not_count_against_workspace_total(
        self, db_session: Session, workspace: Workspace
    ) -> None:
        """Fifty live PATs do not trip the workspace-wide scoped/delegated cap."""
        for user_index in range(10):
            seeded_user = bootstrap_user(
                db_session,
                email=f"tok-pat-ws-cap-{user_index}@example.com",
                display_name=f"Token PAT Workspace Cap {user_index}",
            )
            for _ in range(5):
                _seed_api_token(
                    db_session,
                    user_id=seeded_user.id,
                    workspace_id=None,
                    kind="personal",
                    subject_user_id=seeded_user.id,
                )

        scoped_user = bootstrap_user(
            db_session,
            email="tok-pat-ws-cap-scoped@example.com",
            display_name="Token PAT Workspace Cap Scoped",
        )
        scoped_ctx = _ctx_for_actor(workspace, scoped_user.id)
        mint(
            db_session,
            scoped_ctx,
            user_id=scoped_user.id,
            label="first-workspace-token",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )

    def test_per_user_cap_takes_precedence_over_workspace_cap(
        self, db_session: Session, workspace: Workspace
    ) -> None:
        """At a full workspace, a user's own 6th token keeps the old error."""
        capped_user_id: str | None = None
        capped_ctx: WorkspaceContext | None = None
        for user_index in range(10):
            seeded_user = bootstrap_user(
                db_session,
                email=f"tok-precedence-{user_index}@example.com",
                display_name=f"Token Precedence {user_index}",
            )
            seeded_ctx = _ctx_for_actor(workspace, seeded_user.id)
            if user_index == 0:
                capped_user_id = seeded_user.id
                capped_ctx = seeded_ctx
            for token_index in range(5):
                mint(
                    db_session,
                    seeded_ctx,
                    user_id=seeded_user.id,
                    label=f"u{user_index}-t{token_index}",
                    scopes={},
                    expires_at=_PINNED + timedelta(days=90),
                    now=_PINNED,
                )

        assert capped_user_id is not None
        assert capped_ctx is not None
        with pytest.raises(TooManyTokens):
            mint(
                db_session,
                capped_ctx,
                user_id=capped_user_id,
                label="still-user-cap",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )

    def test_expired_tokens_do_not_count_against_cap(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """Expired rows don't block a new mint — they're inert."""
        # 5 already-expired tokens.
        for i in range(5):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label=f"exp-{i}",
                scopes={},
                expires_at=_PINNED - timedelta(days=1),
                now=_PINNED - timedelta(days=2),
            )
        # 6th mint against ``now=_PINNED`` — the five earlier rows have
        # ``expires_at`` in the past, so the cap doesn't fire.
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="fresh",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        assert result.key_id

    def test_revoked_tokens_do_not_count_against_cap(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """Revoked rows don't block a new mint."""
        # Mint 5 tokens then revoke them all.
        for i in range(5):
            out = mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label=f"rev-{i}",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )
            revoke(db_session, ctx, token_id=out.key_id, now=_PINNED)
        # 6th mint must succeed.
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="fresh",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        assert result.key_id


# ---------------------------------------------------------------------------
# ``verify``
# ---------------------------------------------------------------------------


class TestVerify:
    """``verify`` resolves user + workspace + scopes or raises."""

    def test_happy_path_returns_user_and_scopes(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        scopes = {"tasks:read": True, "stays:read": True}
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="verify",
            scopes=scopes,
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        verified = verify(db_session, token=result.token, now=_PINNED)
        assert verified.user_id == ctx.actor_id
        assert verified.workspace_id == ctx.workspace_id
        assert verified.scopes == scopes
        assert verified.key_id == result.key_id

    def test_malformed_token_raises_invalid(self, db_session: Session) -> None:
        for bad in [
            "not-a-token",
            "mip_",
            "mip_only-key-id",
            "mip__no-key-id",
            "mip_key_",
        ]:
            with pytest.raises(InvalidToken):
                verify(db_session, token=bad, now=_PINNED)

    def test_unknown_key_id_raises_invalid(self, db_session: Session) -> None:
        """A well-formed token with a ``key_id`` that doesn't exist → InvalidToken."""
        # 26-char ULID shape but no row.
        fake_key = "01HWA00000000000000000XXXX"
        # 52-char base32-looking secret.
        fake_secret = "A" * 52
        with pytest.raises(InvalidToken):
            verify(
                db_session,
                token=f"mip_{fake_key}_{fake_secret}",
                now=_PINNED,
            )

    def test_expired_token_raises(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="expires",
            scopes={},
            expires_at=_PINNED + timedelta(days=1),
            now=_PINNED,
        )
        with pytest.raises(TokenExpired):
            verify(
                db_session,
                token=result.token,
                now=_PINNED + timedelta(days=2),
            )

    def test_revoked_token_raises(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="revoked",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        revoke(db_session, ctx, token_id=result.key_id, now=_PINNED)
        with pytest.raises(TokenRevoked):
            verify(db_session, token=result.token, now=_PINNED)

    def test_bad_secret_raises_invalid(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """A tampered secret collapses into InvalidToken (not TokenRevoked /
        TokenExpired)."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="tamper",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        # Flip the first char of the secret.
        _mip, key_id, secret = result.token.split("_", 2)
        tampered_secret = ("A" if secret[0] != "A" else "B") + secret[1:]
        tampered = f"mip_{key_id}_{tampered_secret}"
        with pytest.raises(InvalidToken):
            verify(db_session, token=tampered, now=_PINNED)

    def test_last_used_at_not_updated_within_debounce(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """Two verifies within 1 min leave ``last_used_at`` on the first write."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="debounce",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        # First verify — ``last_used_at`` is NULL, bump lands.
        t1 = _PINNED + timedelta(hours=1)
        verify(db_session, token=result.token, now=t1)
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.last_used_at is not None
        assert _as_utc(row.last_used_at) == t1

        # Second verify 30s later — within 1min debounce, skip.
        t2 = t1 + timedelta(seconds=30)
        verify(db_session, token=result.token, now=t2)
        db_session.expire(row)
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.last_used_at is not None
        assert _as_utc(row.last_used_at) == t1  # unchanged

    def test_last_used_at_updated_past_debounce(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """Two verifies 90s apart both bump ``last_used_at``."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="debounce2",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        t1 = _PINNED + timedelta(hours=1)
        verify(db_session, token=result.token, now=t1)
        t2 = t1 + timedelta(seconds=90)
        verify(db_session, token=result.token, now=t2)
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.last_used_at is not None
        assert _as_utc(row.last_used_at) == t2

    def test_first_use_null_bumps_last_used(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="null-bump",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        row_before = db_session.get(ApiToken, result.key_id)
        assert row_before is not None
        assert row_before.last_used_at is None

        verify(db_session, token=result.token, now=_PINNED)

        row_after = db_session.get(ApiToken, result.key_id)
        assert row_after is not None
        assert row_after.last_used_at is not None


# ---------------------------------------------------------------------------
# ``revoke``
# ---------------------------------------------------------------------------


class TestRevoke:
    """``revoke`` flips ``revoked_at`` and audits."""

    def test_sets_revoked_at_and_audits(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="revoke-me",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        revoke_time = _PINNED + timedelta(hours=2)
        revoke(db_session, ctx, token_id=result.key_id, now=revoke_time)

        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.revoked_at is not None
        assert _as_utc(row.revoked_at) == revoke_time

        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "api_token.revoked")
        ).all()
        assert len(audits) == 1
        audit = audits[0]
        assert audit.entity_kind == "api_token"
        assert audit.entity_id == result.key_id

    def test_double_revoke_is_idempotent_and_audits_noop(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="idem",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        first = _PINNED + timedelta(hours=1)
        second = _PINNED + timedelta(hours=2)
        revoke(db_session, ctx, token_id=result.key_id, now=first)
        revoke(db_session, ctx, token_id=result.key_id, now=second)

        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.revoked_at is not None
        # ``revoked_at`` stays at the first revocation time.
        assert _as_utc(row.revoked_at) == first

        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "api_token.revoked_noop")
        ).all()
        assert len(audits) == 1

    def test_unknown_token_id_raises_invalid(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(InvalidToken):
            revoke(
                db_session,
                ctx,
                token_id="01HWA00000000000000000NOPE",
                now=_PINNED,
            )

    def test_cross_workspace_revoke_rejected(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        workspace: Workspace,
    ) -> None:
        """A ctx on workspace B cannot revoke a token on workspace A."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="cross",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        # Build a second workspace row + ctx.
        other_id = new_ulid()
        with tenant_agnostic():
            db_session.add(
                Workspace(
                    id=other_id,
                    slug="other-ws",
                    name="Other",
                    plan="free",
                    quota_json={},
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        other_ctx = WorkspaceContext(
            workspace_id=other_id,
            workspace_slug="other-ws",
            actor_id=ctx.actor_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id=new_ulid(),
        )
        with pytest.raises(InvalidToken):
            revoke(db_session, other_ctx, token_id=result.key_id, now=_PINNED)


# ---------------------------------------------------------------------------
# ``rotate``
# ---------------------------------------------------------------------------


class TestRotate:
    """``rotate`` swaps the secret in place and audits the lifecycle event."""

    def test_swaps_secret_keeps_metadata(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        original = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="rotateable",
            scopes={"tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        # Pretend the token has been seen; rotate must clear that
        # signal so the SPA's "stale" heuristic doesn't keep the
        # pre-rotation IP / timestamp.
        with tenant_agnostic():
            row = db_session.get(ApiToken, original.key_id)
            assert row is not None
            old_hash = row.hash
            row.last_used_at = _PINNED + timedelta(hours=1)
            db_session.flush()

        rotate_time = _PINNED + timedelta(hours=2)
        rotated = rotate(db_session, ctx, token_id=original.key_id, now=rotate_time)

        assert rotated.key_id == original.key_id
        assert rotated.token != original.token
        assert rotated.prefix != original.prefix
        # Same row, untouched fields.
        with tenant_agnostic():
            row = db_session.get(ApiToken, original.key_id)
        assert row is not None
        assert row.label == "rotateable"
        assert row.scope_json == {"tasks:read": True}
        assert row.expires_at is not None
        assert row.last_used_at is None
        assert row.prefix == rotated.prefix
        assert row.hash != old_hash
        assert row.previous_hash == old_hash
        assert row.previous_hash_expires_at is not None
        assert _as_utc(row.previous_hash_expires_at) == rotate_time + timedelta(hours=1)

    def test_old_secret_verifies_during_overlap(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        original = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="overlap",
            scopes={"tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        rotate_time = _PINNED + timedelta(hours=2)
        rotated = rotate(db_session, ctx, token_id=original.key_id, now=rotate_time)

        old_verified = verify(
            db_session,
            token=original.token,
            now=rotate_time + timedelta(minutes=30),
        )
        new_verified = verify(
            db_session,
            token=rotated.token,
            now=rotate_time + timedelta(minutes=31),
        )

        assert old_verified.key_id == original.key_id
        assert old_verified.scopes == {"tasks:read": True}
        assert new_verified.key_id == original.key_id

    def test_old_secret_rejected_after_overlap_and_fallback_cleared(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        original = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="overlap-expiry",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        rotate_time = _PINNED + timedelta(hours=2)
        rotate(db_session, ctx, token_id=original.key_id, now=rotate_time)

        with pytest.raises(InvalidToken):
            verify(
                db_session,
                token=original.token,
                now=rotate_time + timedelta(hours=1),
            )

        with tenant_agnostic():
            row = db_session.get(ApiToken, original.key_id)
        assert row is not None
        assert row.previous_hash is None
        assert row.previous_hash_expires_at is None

    def test_writes_rotated_audit_with_old_and_new_prefix(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        original = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="auditable",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        rotated = rotate(
            db_session,
            ctx,
            token_id=original.key_id,
            now=_PINNED + timedelta(hours=1),
        )
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "api_token.rotated")
        ).all()
        assert len(audits) == 1
        diff = dict(audits[0].diff)
        assert diff["old_prefix"] == original.prefix
        assert diff["new_prefix"] == rotated.prefix
        assert diff["token_id"] == original.key_id

    def test_personal_token_refused(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
    ) -> None:
        pat = mint(
            db_session,
            None,
            user_id=user.id,  # type: ignore[attr-defined]
            label="my-pat",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user.id,  # type: ignore[attr-defined]
            now=_PINNED,
        )
        # The workspace-scoped rotate seam must not see PATs even when
        # the manager guesses the id correctly.
        with pytest.raises(InvalidToken):
            rotate(db_session, ctx, token_id=pat.key_id, now=_PINNED)

    def test_revoked_token_refused(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        original = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="revoked",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        revoke(
            db_session, ctx, token_id=original.key_id, now=_PINNED + timedelta(hours=1)
        )
        with pytest.raises(InvalidToken):
            rotate(
                db_session,
                ctx,
                token_id=original.key_id,
                now=_PINNED + timedelta(hours=2),
            )

    def test_expired_token_refused(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        original = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="expired",
            scopes={},
            expires_at=_PINNED + timedelta(days=1),
            now=_PINNED,
        )
        with pytest.raises(InvalidToken):
            rotate(
                db_session,
                ctx,
                token_id=original.key_id,
                now=_PINNED + timedelta(days=2),
            )

    def test_unknown_id_refused(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(InvalidToken):
            rotate(
                db_session,
                ctx,
                token_id="01HWA00000000000000000NOPE",
                now=_PINNED,
            )

    def test_cross_workspace_refused(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
    ) -> None:
        original = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="cross-ws",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        other_id = new_ulid()
        with tenant_agnostic():
            db_session.add(
                Workspace(
                    id=other_id,
                    slug="rotate-other",
                    name="Other",
                    plan="free",
                    quota_json={},
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        other_ctx = WorkspaceContext(
            workspace_id=other_id,
            workspace_slug="rotate-other",
            actor_id=ctx.actor_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id=new_ulid(),
        )
        with pytest.raises(InvalidToken):
            rotate(db_session, other_ctx, token_id=original.key_id, now=_PINNED)


# ---------------------------------------------------------------------------
# ``list_audit``
# ---------------------------------------------------------------------------


class TestListAudit:
    """``list_audit`` projects audit_log rows for one workspace token."""

    def test_returns_mint_event(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        original = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="audit-mint",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        entries = list_audit(db_session, ctx, token_id=original.key_id)
        actions = [e.action for e in entries]
        assert "api_token.minted" in actions

    def test_returns_lifecycle_in_reverse_chronological_order(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        original = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="audit-lifecycle",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        rotate(
            db_session, ctx, token_id=original.key_id, now=_PINNED + timedelta(hours=1)
        )
        revoke(
            db_session, ctx, token_id=original.key_id, now=_PINNED + timedelta(hours=2)
        )

        entries = list_audit(db_session, ctx, token_id=original.key_id)
        actions = [e.action for e in entries]
        # Newest first per spec — revoked → rotated → minted.
        assert actions[0] == "api_token.revoked"
        assert "api_token.rotated" in actions
        assert actions[-1] == "api_token.minted"

    def test_cross_workspace_returns_empty(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
    ) -> None:
        original = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="audit-cross",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        other_id = new_ulid()
        with tenant_agnostic():
            db_session.add(
                Workspace(
                    id=other_id,
                    slug="audit-other",
                    name="Other",
                    plan="free",
                    quota_json={},
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        other_ctx = WorkspaceContext(
            workspace_id=other_id,
            workspace_slug="audit-other",
            actor_id=ctx.actor_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id=new_ulid(),
        )
        assert list_audit(db_session, other_ctx, token_id=original.key_id) == []

    def test_unknown_token_returns_empty(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        assert list_audit(db_session, ctx, token_id="01HWA00000000000000000NOPE") == []


# ---------------------------------------------------------------------------
# ``list_tokens``
# ---------------------------------------------------------------------------


class TestListTokens:
    """``list_tokens`` projects rows onto :class:`TokenSummary`."""

    def test_returns_summaries_with_prefix_never_hash(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="listed",
            scopes={"tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        summaries = list_tokens(db_session, ctx)
        assert len(summaries) == 1
        s = summaries[0]
        assert s.key_id == result.key_id
        assert s.label == "listed"
        assert s.prefix == result.prefix
        assert s.scopes == {"tasks:read": True}
        assert s.revoked_at is None
        # :class:`TokenSummary` doesn't expose ``hash`` at all.
        assert not hasattr(s, "hash")

    def test_includes_revoked_rows(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """A revoked row still appears — the /tokens UI needs the history."""
        active = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="active",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        dead = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="dead",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        revoke(db_session, ctx, token_id=dead.key_id, now=_PINNED)

        summaries = list_tokens(db_session, ctx)
        key_ids = {s.key_id for s in summaries}
        assert {active.key_id, dead.key_id} <= key_ids
        dead_summary = next(s for s in summaries if s.key_id == dead.key_id)
        assert dead_summary.revoked_at is not None

    def test_empty_workspace_returns_empty(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        assert list_tokens(db_session, ctx) == []


# ---------------------------------------------------------------------------
# cd-i1qe — delegated tokens
# ---------------------------------------------------------------------------


class TestMintDelegated:
    """Delegated mint path — workspace-pinned, scope-less, inherits user grants."""

    def test_happy_path_returns_delegated_kind_and_fk(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result: MintedToken = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="chat-agent",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        assert result.kind == "delegated"
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.kind == "delegated"
        assert row.delegate_for_user_id == ctx.actor_id
        assert row.subject_user_id is None
        assert row.workspace_id == ctx.workspace_id
        assert row.scope_json == {}

    def test_delegated_token_verifies_with_delegate_fk(
        self, db_session: Session, ctx: WorkspaceContext, live_grant: str
    ) -> None:
        """``verify`` returns kind + delegate_for_user_id on the happy path."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="chat",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        verified = verify(db_session, token=result.token, now=_PINNED)
        assert verified.kind == "delegated"
        assert verified.delegate_for_user_id == ctx.actor_id
        assert verified.subject_user_id is None
        assert verified.workspace_id == ctx.workspace_id
        # Spec: delegated tokens have empty scopes — authority
        # resolves against the delegating user's grants at request
        # time, not against the token itself.
        assert verified.scopes == {}

    def test_delegated_mint_with_scopes_raises_shape_error(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(TokenShapeError):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="bad",
                scopes={"tasks:read": True},
                expires_at=_PINNED + timedelta(days=30),
                kind="delegated",
                delegate_for_user_id=ctx.actor_id,
                now=_PINNED,
            )

    def test_delegated_mint_without_delegate_fk_raises_shape_error(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(TokenShapeError):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="bad",
                scopes={},
                expires_at=_PINNED + timedelta(days=30),
                kind="delegated",
                delegate_for_user_id=None,
                now=_PINNED,
            )

    def test_delegated_counts_against_workspace_cap(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """Mixed 4 scoped + 1 delegated → 6th mint (either kind) 422s."""
        for i in range(4):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label=f"sc-{i}",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )
        mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="delegate",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        with pytest.raises(TooManyTokens):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="6th",
                scopes={},
                expires_at=_PINNED + timedelta(days=30),
                kind="delegated",
                delegate_for_user_id=ctx.actor_id,
                now=_PINNED,
            )

    def test_audit_row_carries_kind_and_delegate_fk(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="audit-del",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        audits = db_session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "api_token.minted")
            .where(AuditLog.entity_id == result.key_id)
        ).all()
        assert len(audits) == 1
        row = audits[0]
        assert isinstance(row.diff, dict)
        assert row.diff["kind"] == "delegated"
        assert row.diff["delegate_for_user_id"] == ctx.actor_id


# ---------------------------------------------------------------------------
# cd-i1qe — personal access tokens (PATs)
# ---------------------------------------------------------------------------


class TestMintPersonal:
    """PAT mint path — identity-scoped, ``me:*`` scopes, workspace NULL."""

    def test_happy_path_returns_personal_kind_and_workspace_null(
        self, db_session: Session, user: object
    ) -> None:
        """PATs carry workspace_id=NULL, subject_user_id populated."""
        user_id = user.id  # type: ignore[attr-defined]
        result: MintedToken = mint(
            db_session,
            None,
            user_id=user_id,
            label="kitchen-printer",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        assert result.kind == "personal"
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.kind == "personal"
        assert row.workspace_id is None
        assert row.subject_user_id == user_id
        assert row.delegate_for_user_id is None

    def test_personal_token_verifies_with_null_workspace(
        self, db_session: Session, user: object, live_grant: str
    ) -> None:
        user_id = user.id  # type: ignore[attr-defined]
        result = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat",
            scopes={"me.bookings:read": True, "me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        verified = verify(db_session, token=result.token, now=_PINNED)
        assert verified.kind == "personal"
        assert verified.workspace_id is None
        assert verified.subject_user_id == user_id
        assert verified.delegate_for_user_id is None
        assert verified.scopes == {
            "me.bookings:read": True,
            "me.tasks:read": True,
        }

    def test_personal_mint_with_workspace_ctx_raises_shape_error(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(TokenShapeError):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="bad",
                scopes={"me.tasks:read": True},
                expires_at=_PINNED + timedelta(days=90),
                kind="personal",
                subject_user_id=ctx.actor_id,
                now=_PINNED,
            )

    def test_personal_mint_with_workspace_scope_raises_shape_error(
        self, db_session: Session, user: object
    ) -> None:
        user_id = user.id  # type: ignore[attr-defined]
        with pytest.raises(TokenShapeError):
            mint(
                db_session,
                None,
                user_id=user_id,
                label="bad",
                scopes={"tasks:read": True},  # workspace scope!
                expires_at=_PINNED + timedelta(days=90),
                kind="personal",
                subject_user_id=user_id,
                now=_PINNED,
            )

    def test_personal_mint_without_scopes_raises_shape_error(
        self, db_session: Session, user: object
    ) -> None:
        user_id = user.id  # type: ignore[attr-defined]
        with pytest.raises(TokenShapeError):
            mint(
                db_session,
                None,
                user_id=user_id,
                label="bad",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                kind="personal",
                subject_user_id=user_id,
                now=_PINNED,
            )

    def test_sixth_personal_token_raises_too_many_personal(
        self, db_session: Session, user: object
    ) -> None:
        user_id = user.id  # type: ignore[attr-defined]
        for i in range(5):
            mint(
                db_session,
                None,
                user_id=user_id,
                label=f"pat-{i}",
                scopes={"me.tasks:read": True},
                expires_at=_PINNED + timedelta(days=90),
                kind="personal",
                subject_user_id=user_id,
                now=_PINNED,
            )
        with pytest.raises(TooManyPersonalTokens):
            mint(
                db_session,
                None,
                user_id=user_id,
                label="6th",
                scopes={"me.tasks:read": True},
                expires_at=_PINNED + timedelta(days=90),
                kind="personal",
                subject_user_id=user_id,
                now=_PINNED,
            )

    def test_personal_does_not_count_against_workspace_cap(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
    ) -> None:
        """5 PATs + 5 workspace tokens all coexist — separate caps."""
        user_id = user.id  # type: ignore[attr-defined]
        for i in range(5):
            mint(
                db_session,
                None,
                user_id=user_id,
                label=f"pat-{i}",
                scopes={"me.tasks:read": True},
                expires_at=_PINNED + timedelta(days=90),
                kind="personal",
                subject_user_id=user_id,
                now=_PINNED,
            )
        # 5 workspace tokens still fit.
        for i in range(5):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label=f"ws-{i}",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )
        # 6th workspace token now 422s, but PAT cap is independent.
        with pytest.raises(TooManyTokens):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="over",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )


# ---------------------------------------------------------------------------
# cd-i1qe — list / revoke seam narrowing
# ---------------------------------------------------------------------------


class TestListPersonal:
    """``list_personal_tokens`` returns only PATs for the subject."""

    def test_includes_only_personal_tokens(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
    ) -> None:
        user_id = user.id  # type: ignore[attr-defined]
        # 1 PAT + 1 scoped + 1 delegated for the same user.
        pat = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="scoped",
            scopes={"tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="delegated",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        summaries = list_personal_tokens(db_session, subject_user_id=user_id)
        key_ids = {s.key_id for s in summaries}
        assert key_ids == {pat.key_id}
        assert summaries[0].kind == "personal"

    def test_workspace_list_excludes_personal_tokens(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
    ) -> None:
        """``list_tokens`` (workspace view) never surfaces PATs."""
        user_id = user.id  # type: ignore[attr-defined]
        # 1 PAT + 1 scoped — only the scoped row should appear.
        mint(
            db_session,
            None,
            user_id=user_id,
            label="pat",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        scoped = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="scoped",
            scopes={"tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        summaries = list_tokens(db_session, ctx)
        key_ids = {s.key_id for s in summaries}
        assert key_ids == {scoped.key_id}


class TestRevokePersonal:
    """``revoke_personal`` is subject-scoped and refuses workspace tokens."""

    def test_revokes_own_pat(self, db_session: Session, user: object) -> None:
        user_id = user.id  # type: ignore[attr-defined]
        result = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        revoke_personal(
            db_session,
            token_id=result.key_id,
            subject_user_id=user_id,
            now=_PINNED + timedelta(hours=1),
        )
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.revoked_at is not None
        # Re-verify now fails with TokenRevoked.
        with pytest.raises(TokenRevoked):
            verify(db_session, token=result.token, now=_PINNED + timedelta(hours=2))

    def test_revoke_another_users_pat_raises_invalid(
        self, db_session: Session, user: object
    ) -> None:
        """Subject B cannot revoke subject A's PAT."""
        user_a_id = user.id  # type: ignore[attr-defined]
        result = mint(
            db_session,
            None,
            user_id=user_a_id,
            label="a-pat",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_a_id,
            now=_PINNED,
        )
        # Pass a different user id — the row shouldn't match.
        other_id = new_ulid()
        with pytest.raises(InvalidToken):
            revoke_personal(
                db_session,
                token_id=result.key_id,
                subject_user_id=other_id,
                now=_PINNED,
            )

    def test_revoke_personal_on_workspace_token_raises_invalid(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """``revoke_personal`` refuses a workspace-scoped token id."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="scoped",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        with pytest.raises(InvalidToken):
            revoke_personal(
                db_session,
                token_id=result.key_id,
                subject_user_id=ctx.actor_id,
                now=_PINNED,
            )

    def test_workspace_revoke_on_personal_token_raises_invalid(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
    ) -> None:
        """``revoke`` (workspace) refuses to touch a PAT row."""
        user_id = user.id  # type: ignore[attr-defined]
        result = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        with pytest.raises(InvalidToken):
            revoke(db_session, ctx, token_id=result.key_id, now=_PINNED)


class TestKindValidation:
    """Domain vocabulary guards."""

    def test_unknown_kind_raises_token_kind_invalid(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(TokenKindInvalid):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="bad",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                kind="scopped",  # type: ignore[arg-type]
                now=_PINNED,
            )

    def test_scoped_mint_with_me_scope_raises_shape_error(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """Mixing a me:* scope into a scoped token is refused."""
        with pytest.raises(TokenShapeError):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="mix",
                scopes={"tasks:read": True, "me.tasks:read": True},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )


# ---------------------------------------------------------------------------
# cd-t2su — tenant-agnostic audit seam for PAT mint / revoke
# ---------------------------------------------------------------------------


class TestPatAuditSeam:
    """PAT mint + revoke land identity-scope audit rows (cd-t2su).

    Spec §03 "API tokens" requires an audit row for every rotation /
    revocation. PATs have no workspace, so they land on the tenant-
    agnostic seam: ``workspace_id`` is the zero-ULID sentinel and
    ``actor_id`` is the **real subject user** (so the ``/me`` audit
    view can filter per-user without a JSON scan).
    """

    def test_personal_mint_writes_identity_audit_row(
        self, db_session: Session, user: object
    ) -> None:
        user_id = user.id  # type: ignore[attr-defined]
        result = mint(
            db_session,
            None,
            user_id=user_id,
            label="kitchen-printer",
            scopes={"me.tasks:read": True, "me.bookings:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        audits = db_session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "api_token.minted")
            .where(AuditLog.entity_id == result.key_id)
        ).all()
        assert len(audits) == 1
        row = audits[0]
        assert row.entity_kind == "api_token"
        assert row.workspace_id == AGNOSTIC_WORKSPACE_ID
        # The real subject user lands on the row itself so the
        # ``/me`` audit view can filter ``actor_id = <user>`` without
        # a JSON scan into ``diff``.
        assert row.actor_id == user_id
        assert row.actor_kind == "user"
        assert isinstance(row.diff, dict)
        assert row.diff["token_id"] == result.key_id
        assert row.diff["kind"] == "personal"
        assert row.diff["subject_user_id"] == user_id
        # Scope keys serialise as a sorted list so a future readback
        # is stable regardless of dict insertion order.
        assert row.diff["scopes"] == ["me.bookings:read", "me.tasks:read"]
        # Plaintext token must not appear anywhere in the diff.
        _mip, _key_id, secret = result.token.split("_", 2)
        assert secret not in repr(row.diff)
        assert result.token not in repr(row.diff)

    def test_personal_revoke_writes_identity_audit_row(
        self, db_session: Session, user: object
    ) -> None:
        user_id = user.id  # type: ignore[attr-defined]
        result = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        revoke_time = _PINNED + timedelta(hours=2)
        revoke_personal(
            db_session,
            token_id=result.key_id,
            subject_user_id=user_id,
            now=revoke_time,
        )
        audits = db_session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "api_token.revoked")
            .where(AuditLog.entity_id == result.key_id)
        ).all()
        assert len(audits) == 1
        row = audits[0]
        assert row.entity_kind == "api_token"
        assert row.workspace_id == AGNOSTIC_WORKSPACE_ID
        assert row.actor_id == user_id
        assert row.actor_kind == "user"
        assert isinstance(row.diff, dict)
        assert row.diff["token_id"] == result.key_id
        assert row.diff["subject_user_id"] == user_id
        assert row.diff["kind"] == "personal"
        assert row.diff["at"] == revoke_time.isoformat()

    def test_double_revoke_personal_writes_one_audit_row(
        self, db_session: Session, user: object
    ) -> None:
        """Idempotent re-revoke must NOT write a second audit row.

        Matches the workspace-side ``revoke`` precedent: one
        revocation event per token lifetime. Total audit rows for
        the token after mint + double-revoke is exactly two
        (one mint + one revoke).
        """
        user_id = user.id  # type: ignore[attr-defined]
        result = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        first = _PINNED + timedelta(hours=1)
        second = _PINNED + timedelta(hours=2)
        revoke_personal(
            db_session,
            token_id=result.key_id,
            subject_user_id=user_id,
            now=first,
        )
        revoke_personal(
            db_session,
            token_id=result.key_id,
            subject_user_id=user_id,
            now=second,
        )
        # ``revoked_at`` keeps the first time — second call is a no-op.
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.revoked_at is not None
        assert _as_utc(row.revoked_at) == first

        # Exactly one revoke audit row, exactly one mint audit row.
        all_for_token = db_session.scalars(
            select(AuditLog).where(AuditLog.entity_id == result.key_id)
        ).all()
        actions = sorted(r.action for r in all_for_token)
        assert actions == ["api_token.minted", "api_token.revoked"]

    def test_failed_revoke_writes_no_audit_row(
        self, db_session: Session, user: object
    ) -> None:
        """An unknown / cross-user revoke raises and writes nothing."""
        user_id = user.id  # type: ignore[attr-defined]
        # Mint one PAT so the audit table isn't empty for unrelated reasons.
        result = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        with pytest.raises(InvalidToken):
            revoke_personal(
                db_session,
                token_id="01HWA00000000000000000NOPE",
                subject_user_id=user_id,
                now=_PINNED,
            )
        # No revoke row landed; only the mint row from the seed exists.
        revokes = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "api_token.revoked")
        ).all()
        assert revokes == []
        # Mint row still there to confirm the audit table is wired.
        mints = db_session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "api_token.minted")
            .where(AuditLog.entity_id == result.key_id)
        ).all()
        assert len(mints) == 1

    def test_workspace_mint_audit_still_lands_on_real_workspace(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """Regression guard: workspace-scoped mints still land on ``ctx``.

        cd-t2su added a tenant-agnostic branch under ``mint``; this
        test asserts the workspace-scoped branch still writes to the
        caller's real workspace (not the zero-ULID sentinel).
        """
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="ws-token",
            scopes={"tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        audits = db_session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "api_token.minted")
            .where(AuditLog.entity_id == result.key_id)
        ).all()
        assert len(audits) == 1
        row = audits[0]
        # Real workspace, not the agnostic sentinel.
        assert row.workspace_id == ctx.workspace_id
        assert row.workspace_id != AGNOSTIC_WORKSPACE_ID
        assert row.actor_id == ctx.actor_id

    def test_prefix_survives_pii_redactor(self) -> None:
        """The 8-char base32 ``prefix`` field must not be scrubbed.

        The PII redactor (``app.util.redact``) runs over every audit
        ``diff`` before persistence (``app.audit.write_audit``). The
        ``prefix`` value is 8 chars of base32 (alphabet ``A-Z2-7``);
        the credential-shape regexes require 32+ hex / 40+ base64url
        chars, so an 8-char prefix is below every threshold and the
        ``prefix`` key name is not in the sensitive-key token list.
        Confirmed end-to-end here so a future redactor tweak that
        widens the net trips this test instead of silently corrupting
        the ``/me`` audit view.
        """
        sample_diff = {
            "token_id": "01HWA00000000000000000PATX",
            "subject_user_id": "01HWA00000000000000000USRX",
            "label": "kitchen",
            "prefix": "ABCD2345",  # exactly the shape mint() emits
            "scopes": ["me.tasks:read"],
            "kind": "personal",
            "expires_at": "2026-07-20T12:00:00+00:00",
        }
        scrubbed = redact(sample_diff, scope="log")
        assert isinstance(scrubbed, dict)
        assert scrubbed["prefix"] == "ABCD2345"
        # Spot-check that other plain fields also survive — if they
        # don't, the redactor changed shape and this test should be
        # updated alongside the redactor change.
        assert scrubbed["kind"] == "personal"
        assert scrubbed["scopes"] == ["me.tasks:read"]


# ---------------------------------------------------------------------------
# cd-et6y — verify-time delegating / subject user liveness check
# ---------------------------------------------------------------------------


class TestVerifyArchivedUser:
    """``verify`` returns 401-equivalents when delegating / subject user is archived.

    §03 "Delegated tokens" / "Personal access tokens": a delegated
    token whose delegating user is archived returns 401 with a clear
    message; same shape for a PAT whose subject user is archived.
    The token row itself stays live (archive-preserves-rows per
    §05) — only the user-side tombstone gates verification.
    """

    def test_delegated_verify_succeeds_for_live_user(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
        live_grant: str,
    ) -> None:
        """Sanity floor: a live (non-archived) delegating user verifies cleanly."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="chat-live",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        # ``user.archived_at`` is NULL by default — explicit assertion
        # so a future schema change doesn't silently flip the gate.
        assert user.archived_at is None  # type: ignore[attr-defined]
        verified = verify(db_session, token=result.token, now=_PINNED)
        assert verified.kind == "delegated"
        assert verified.delegate_for_user_id == ctx.actor_id

    def test_pat_verify_succeeds_for_live_user(
        self, db_session: Session, user: object, live_grant: str
    ) -> None:
        """Sanity floor: a live (non-archived) subject user verifies cleanly."""
        user_id = user.id  # type: ignore[attr-defined]
        result = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat-live",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        assert user.archived_at is None  # type: ignore[attr-defined]
        verified = verify(db_session, token=result.token, now=_PINNED)
        assert verified.kind == "personal"
        assert verified.subject_user_id == user_id

    def test_delegated_verify_rejects_archived_delegating_user(
        self, db_session: Session, ctx: WorkspaceContext, user: object
    ) -> None:
        """Token row is otherwise live — only ``user.archived_at`` flips the gate."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="chat-archived",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        # Archive the delegating user — token row stays untouched.
        user.archived_at = _PINNED + timedelta(hours=1)  # type: ignore[attr-defined]
        db_session.flush()
        with pytest.raises(DelegatingUserArchived):
            verify(
                db_session,
                token=result.token,
                now=_PINNED + timedelta(hours=2),
            )
        # Token row itself never got revoked / expired — confirm.
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.revoked_at is None

    def test_pat_verify_rejects_archived_subject_user(
        self, db_session: Session, user: object
    ) -> None:
        """PAT verification respects ``user.archived_at`` on the subject."""
        user_id = user.id  # type: ignore[attr-defined]
        result = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat-archived",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        user.archived_at = _PINNED + timedelta(hours=1)  # type: ignore[attr-defined]
        db_session.flush()
        with pytest.raises(SubjectUserArchived):
            verify(
                db_session,
                token=result.token,
                now=_PINNED + timedelta(hours=2),
            )
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.revoked_at is None

    def test_scoped_token_unaffected_by_user_archive(
        self, db_session: Session, ctx: WorkspaceContext, user: object
    ) -> None:
        """``scoped`` tokens inherit no user authority; archive doesn't block them.

        The §03 archive gate is delegated / PAT specific — a scoped
        token's authority is the explicit ``scope_json`` set, not a
        delegating user's grants. Archiving the user who happened to
        mint the scoped token must not retroactively disable the
        token: revocation is the only valid kill path.
        """
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="scoped-archive",
            scopes={"tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        user.archived_at = _PINNED + timedelta(hours=1)  # type: ignore[attr-defined]
        db_session.flush()
        verified = verify(
            db_session,
            token=result.token,
            now=_PINNED + timedelta(hours=2),
        )
        assert verified.kind == "scoped"
        assert verified.scopes == {"tasks:read": True}

    def test_archived_user_with_revoked_token_collapses_to_revoked(
        self, db_session: Session, ctx: WorkspaceContext, user: object
    ) -> None:
        """Revocation precedes the liveness gate.

        When the token is BOTH revoked AND the delegating user is
        archived, the verifier surfaces ``TokenRevoked`` — the
        revocation is the older / lower-level fact, and the spec's
        own ordering "revoked → 401" leads the agent to the same
        recovery path either way.
        """
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="del-revoked-archived",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        revoke(db_session, ctx, token_id=result.key_id, now=_PINNED)
        user.archived_at = _PINNED + timedelta(hours=1)  # type: ignore[attr-defined]
        db_session.flush()
        with pytest.raises(TokenRevoked):
            verify(
                db_session,
                token=result.token,
                now=_PINNED + timedelta(hours=2),
            )

    def test_archived_user_with_expired_token_collapses_to_expired(
        self, db_session: Session, ctx: WorkspaceContext, user: object
    ) -> None:
        """Expiry also precedes the liveness gate — same reasoning as revoke above."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="del-expired-archived",
            scopes={},
            expires_at=_PINNED + timedelta(days=1),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        user.archived_at = _PINNED + timedelta(hours=1)  # type: ignore[attr-defined]
        db_session.flush()
        with pytest.raises(TokenExpired):
            verify(
                db_session,
                token=result.token,
                now=_PINNED + timedelta(days=2),
            )

    def test_bad_secret_with_archived_user_collapses_to_invalid(
        self, db_session: Session, ctx: WorkspaceContext, user: object
    ) -> None:
        """A wrong secret never reaches the liveness gate.

        We must not leak "this token belongs to an archived user" to
        a probe that does NOT prove knowledge of the secret. The
        verifier runs the argon2 check before the archive gate so a
        tampered secret collapses into the opaque
        :class:`InvalidToken` shape, identical to a probe against an
        unknown ``key_id``.
        """
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="del-tamper-archived",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        user.archived_at = _PINNED + timedelta(hours=1)  # type: ignore[attr-defined]
        db_session.flush()
        # Tamper with the secret so argon2 mismatches.
        _mip, key_id, secret = result.token.split("_", 2)
        tampered_secret = ("A" if secret[0] != "A" else "B") + secret[1:]
        tampered = f"mip_{key_id}_{tampered_secret}"
        with pytest.raises(InvalidToken):
            verify(
                db_session,
                token=tampered,
                now=_PINNED + timedelta(hours=2),
            )

    def test_reinstating_user_clears_archive_gate(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
        live_grant: str,
    ) -> None:
        """Setting ``archived_at`` back to NULL re-enables the token (§03 reinstate).

        Spec §03 "Personal access tokens": "Reinstating the user
        reinstates their PATs only if they survived archive (spec is
        archive-preserves-rows)" — once ``archived_at`` clears, the
        token verifies cleanly again. Same shape for delegated.
        """
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="del-reinstate",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        user.archived_at = _PINNED + timedelta(hours=1)  # type: ignore[attr-defined]
        db_session.flush()
        with pytest.raises(DelegatingUserArchived):
            verify(
                db_session,
                token=result.token,
                now=_PINNED + timedelta(hours=2),
            )
        # Reinstate.
        user.archived_at = None  # type: ignore[attr-defined]
        db_session.flush()
        verified = verify(
            db_session,
            token=result.token,
            now=_PINNED + timedelta(hours=3),
        )
        assert verified.kind == "delegated"


class TestVerifyInactiveUser:
    """``verify`` returns 401-equivalents when the principal has no live grants.

    cd-ljvs / §03 "Delegated tokens" / "Personal access tokens": a
    delegated token whose delegating user holds zero live
    ``role_grant`` rows in the token's workspace returns 401
    ``delegating_user_inactive``; same shape for a PAT whose subject
    user holds zero live grants in any workspace —
    ``subject_user_inactive``. The check sits AFTER the archive
    gate so an archived user with no grants surfaces the
    archive-shape error (the lower-level fact). cd-x1xh's
    soft-retire columns are what made this enforceable: before
    ``role_grant.revoked_at`` existed, "no live grants" looked
    identical to "never granted" at the SQL level.
    """

    def test_delegated_verify_succeeds_with_live_grant_in_workspace(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
        live_grant: str,
    ) -> None:
        """Sanity floor: a live grant in the token's workspace verifies."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="del-live",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        verified = verify(db_session, token=result.token, now=_PINNED)
        assert verified.kind == "delegated"
        assert verified.delegate_for_user_id == ctx.actor_id

    def test_delegated_verify_rejects_user_with_only_revoked_grants(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
    ) -> None:
        """Every grant in the workspace soft-retired ⇒ ``DelegatingUserInactive``."""
        # Seed one grant and immediately mark it revoked — represents
        # the soft-retire shape cd-x1xh writes on the revoke path.
        _seed_role_grant(
            db_session,
            workspace_id=ctx.workspace_id,
            user_id=ctx.actor_id,
            revoked_at=_PINNED + timedelta(minutes=5),
        )
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="del-inactive",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        with pytest.raises(DelegatingUserInactive):
            verify(
                db_session,
                token=result.token,
                now=_PINNED + timedelta(hours=1),
            )
        # Token row stays untouched — only the principal's grants
        # gate the verifier; revocation is the only path that flips
        # the row itself.
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.revoked_at is None

    def test_delegated_verify_rejects_when_live_grant_is_in_other_workspace(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
    ) -> None:
        """Delegated check is per-workspace — a grant elsewhere doesn't unblock."""
        # Live grant in a *sibling* workspace; revoked grant in the
        # token's workspace. The delegated token's authority is
        # anchored on its issuing workspace, so the sibling grant
        # must NOT count.
        sibling_ws_id = new_ulid()
        sibling = Workspace(
            id=sibling_ws_id,
            slug="ws-sibling",
            name="Sibling WS",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
        with tenant_agnostic():
            db_session.add(sibling)
            db_session.flush()
        _seed_role_grant(
            db_session,
            workspace_id=sibling_ws_id,
            user_id=ctx.actor_id,
        )
        _seed_role_grant(
            db_session,
            workspace_id=ctx.workspace_id,
            user_id=ctx.actor_id,
            revoked_at=_PINNED + timedelta(minutes=5),
        )
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="del-sibling",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        with pytest.raises(DelegatingUserInactive):
            verify(
                db_session,
                token=result.token,
                now=_PINNED + timedelta(hours=1),
            )

    def test_archived_user_with_no_grants_surfaces_archived(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
    ) -> None:
        """Archive precedes inactive — both gates would fire, archive wins.

        Spec orders the four error codes archive-first (the
        lower-level fact). Reinstating clears the archive flag and
        the verifier then re-evaluates liveness; if grants are
        also missing, the agent gets ``delegating_user_inactive``
        on the next call.
        """
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="del-arch-no-grants",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        # Archive AND seed only a revoked grant — both gates would
        # fire, but archive runs first.
        user.archived_at = _PINNED + timedelta(minutes=5)  # type: ignore[attr-defined]
        _seed_role_grant(
            db_session,
            workspace_id=ctx.workspace_id,
            user_id=ctx.actor_id,
            revoked_at=_PINNED + timedelta(minutes=5),
        )
        with pytest.raises(DelegatingUserArchived):
            verify(
                db_session,
                token=result.token,
                now=_PINNED + timedelta(hours=1),
            )

    def test_pat_verify_succeeds_with_live_grant_anywhere(
        self,
        db_session: Session,
        user: object,
        live_grant: str,
    ) -> None:
        """Sanity floor: a live grant in any workspace lets the PAT verify."""
        user_id = user.id  # type: ignore[attr-defined]
        result = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat-live",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        verified = verify(db_session, token=result.token, now=_PINNED)
        assert verified.kind == "personal"
        assert verified.subject_user_id == user_id

    def test_pat_verify_rejects_user_with_only_revoked_grants_everywhere(
        self,
        db_session: Session,
        workspace: Workspace,
        user: object,
    ) -> None:
        """Every grant soft-retired across every workspace ⇒ ``SubjectUserInactive``."""
        user_id = user.id  # type: ignore[attr-defined]
        # Two workspaces, both with only revoked grants — the PAT
        # check is workspace-agnostic, so both must be soft-retired
        # for the gate to fire.
        sibling_ws_id = new_ulid()
        sibling = Workspace(
            id=sibling_ws_id,
            slug="ws-pat-sib",
            name="Sib",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
        with tenant_agnostic():
            db_session.add(sibling)
            db_session.flush()
        _seed_role_grant(
            db_session,
            workspace_id=workspace.id,
            user_id=user_id,
            revoked_at=_PINNED + timedelta(minutes=5),
        )
        _seed_role_grant(
            db_session,
            workspace_id=sibling_ws_id,
            user_id=user_id,
            revoked_at=_PINNED + timedelta(minutes=5),
        )
        result = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat-inactive",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        with pytest.raises(SubjectUserInactive):
            verify(
                db_session,
                token=result.token,
                now=_PINNED + timedelta(hours=1),
            )

    def test_pat_verify_succeeds_when_grant_is_live_in_any_workspace(
        self,
        db_session: Session,
        workspace: Workspace,
        user: object,
    ) -> None:
        """PAT check is workspace-agnostic — a live grant anywhere unblocks.

        Seed a *revoked* grant in the home workspace and a *live*
        grant in a sibling workspace. The PAT does not pin a
        workspace at issue time, so the sibling grant suffices
        even though the home workspace's grants are all
        soft-retired.
        """
        user_id = user.id  # type: ignore[attr-defined]
        sibling_ws_id = new_ulid()
        sibling = Workspace(
            id=sibling_ws_id,
            slug="ws-pat-live-sib",
            name="LiveSib",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
        with tenant_agnostic():
            db_session.add(sibling)
            db_session.flush()
        _seed_role_grant(
            db_session,
            workspace_id=workspace.id,
            user_id=user_id,
            revoked_at=_PINNED + timedelta(minutes=5),
        )
        _seed_role_grant(
            db_session,
            workspace_id=sibling_ws_id,
            user_id=user_id,
        )
        result = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat-cross-ws",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        verified = verify(db_session, token=result.token, now=_PINNED)
        assert verified.kind == "personal"
        assert verified.subject_user_id == user_id

    def test_scoped_token_unaffected_by_grant_inactivity(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
    ) -> None:
        """Scoped tokens carry their own authority — grant liveness doesn't gate them.

        The §03 inactive gate is delegated / PAT specific. A scoped
        token's authority is the explicit ``scope_json`` set, not a
        delegating user's grants. Soft-retiring every grant on the
        minting user must NOT retroactively disable a scoped token —
        revocation is the only valid kill path (matches the existing
        ``test_scoped_token_unaffected_by_user_archive`` precedent).
        """
        _seed_role_grant(
            db_session,
            workspace_id=ctx.workspace_id,
            user_id=ctx.actor_id,
            revoked_at=_PINNED + timedelta(minutes=5),
        )
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="scoped-inactive",
            scopes={"tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        verified = verify(
            db_session,
            token=result.token,
            now=_PINNED + timedelta(hours=1),
        )
        assert verified.kind == "scoped"
        assert verified.scopes == {"tasks:read": True}
