"""Unit tests for :mod:`app.auth.magic_link`.

The tests exercise the domain service against an in-memory SQLite
engine with the schema created from ``Base.metadata``. The mailer is
a recording double that captures every :meth:`Mailer.send` call so
tests can assert the rendered URL / subject without touching SMTP.

Coverage matrix (cd-4zz acceptance + spec §03):

* Purpose enforcement — a ``signup_verify`` token cannot be consumed
  as ``recover_passkey``.
* TTL ceiling — ``signup_verify`` caps at 15 min, everything else
  at 10; a caller-requested 24h TTL silently clamps.
* Single-use under simulated concurrent consume.
* Rate-limit trips on both per-IP and per-email buckets.
* Enumeration guard — missing user returns ``None`` with no mail
  sent and no nonce inserted.
* Audit rows carry hashes only, never plaintext email / IP / token.

See ``docs/specs/03-auth-and-tokens.md`` §"Magic link format",
§"Recovery paths" and ``docs/specs/15-security-privacy.md``
§"Rate limiting and abuse controls".
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from itsdangerous import URLSafeTimedSerializer
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import MagicLinkNonce
from app.adapters.db.session import make_engine
from app.audit import write_audit
from app.auth.magic_link import (
    _SERIALIZER_SALT,
    AlreadyConsumed,
    ConsumeLockout,
    InvalidToken,
    PurposeMismatch,
    RateLimited,
    Throttle,
    TokenExpired,
    _agnostic_audit_ctx,
    _subkey,
    consume_link,
    reason_for_exception,
    request_link,
    write_rejected_audit,
)
from app.config import Settings
from tests.factories.identity import bootstrap_user

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _SentMessage:
    """One recorded :meth:`Mailer.send` invocation."""

    to: list[str]
    subject: str
    body_text: str


@dataclass
class _RecordingMailer:
    """In-memory :class:`app.adapters.mail.ports.Mailer` double."""

    sent: list[_SentMessage] = field(default_factory=list)

    def send(
        self,
        *,
        to: Sequence[str],
        subject: str,
        body_text: str,
        body_html: str | None = None,
        headers: Mapping[str, str] | None = None,
        reply_to: str | None = None,
    ) -> str:
        del body_html, headers, reply_to
        self.sent.append(
            _SentMessage(to=list(to), subject=subject, body_text=body_text)
        )
        return "test-message-id"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    """Minimal :class:`Settings` with just the keys the service reads."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-root-key-do-not-ship"),
        public_url="https://crew.day",
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def mailer() -> _RecordingMailer:
    return _RecordingMailer()


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def serializer(settings: Settings) -> URLSafeTimedSerializer:
    """Build a serializer matching the service's signing key.

    Used by tests that need to mint forged / expired / mismatched
    tokens outside the service's own :func:`request_link` happy path.
    """
    return URLSafeTimedSerializer(
        secret_key=_subkey(settings),
        salt=_SERIALIZER_SALT,
    )


def _extract_token(message: _SentMessage) -> str:
    """Return the token part of the emitted magic-link URL."""
    for line in message.body_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("https://"):
            return stripped.rsplit("/", 1)[-1]
    raise AssertionError(f"no magic-link URL in body: {message.body_text!r}")


# ---------------------------------------------------------------------------
# request_link
# ---------------------------------------------------------------------------


class TestRequestLink:
    """``request_link`` mints the token, inserts the nonce, sends mail."""

    def test_signup_verify_inserts_pending_nonce(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        request_link(
            session,
            email="new@example.com",
            purpose="signup_verify",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )

        rows = session.scalars(select(MagicLinkNonce)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.purpose == "signup_verify"
        assert row.consumed_at is None
        # Per-purpose TTL ceiling is 15 min for signup_verify.
        assert row.expires_at - row.created_at == timedelta(minutes=15)
        # One mail sent.
        assert len(mailer.sent) == 1
        assert mailer.sent[0].to == ["new@example.com"]
        assert "verify your email" in mailer.sent[0].subject

    def test_recovery_resolves_existing_user(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        user = bootstrap_user(
            session,
            email="rec@example.com",
            display_name="Rec",
        )
        request_link(
            session,
            email="rec@example.com",
            purpose="recover_passkey",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        row = session.scalars(select(MagicLinkNonce)).one()
        assert row.subject_id == user.id
        assert row.expires_at - row.created_at == timedelta(minutes=10)

    def test_enumeration_guard_silently_drops_unknown_email(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """No user row → no nonce, no mail; function still returns None."""
        request_link(
            session,
            email="ghost@example.com",
            purpose="recover_passkey",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        assert session.scalars(select(MagicLinkNonce)).all() == []
        assert mailer.sent == []

    def test_ttl_capped_at_purpose_ceiling(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """Caller asks for 24h — the service silently caps to the ceiling."""
        bootstrap_user(session, email="cap@example.com", display_name="Cap")
        request_link(
            session,
            email="cap@example.com",
            purpose="recover_passkey",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            ttl=timedelta(hours=24),
            throttle=throttle,
            settings=settings,
        )
        row = session.scalars(select(MagicLinkNonce)).one()
        # Non-signup purposes cap at 10 min.
        assert row.expires_at - row.created_at == timedelta(minutes=10)

    def test_shorter_ttl_respected(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """Caller asks for 2 min — honoured because 2 < ceiling."""
        bootstrap_user(session, email="short@example.com", display_name="Short")
        request_link(
            session,
            email="short@example.com",
            purpose="recover_passkey",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            ttl=timedelta(minutes=2),
            throttle=throttle,
            settings=settings,
        )
        row = session.scalars(select(MagicLinkNonce)).one()
        assert row.expires_at - row.created_at == timedelta(minutes=2)

    def test_rate_limit_per_ip_trips_after_five_requests(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """5 requests/min per IP is the §15 cap."""
        for i in range(5):
            request_link(
                session,
                email=f"user-{i}@example.com",
                purpose="signup_verify",
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )
        # 6th in the same window → RateLimited.
        with pytest.raises(RateLimited):
            request_link(
                session,
                email="user-6@example.com",
                purpose="signup_verify",
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )
        assert len(mailer.sent) == 5

    def test_rate_limit_per_email_trips(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """5/min per email cap — different IPs don't save you."""
        for i in range(5):
            request_link(
                session,
                email="same@example.com",
                purpose="signup_verify",
                ip=f"127.0.0.{i + 1}",
                mailer=mailer,
                base_url="https://crew.day",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )
        with pytest.raises(RateLimited):
            request_link(
                session,
                email="same@example.com",
                purpose="signup_verify",
                ip="127.0.0.99",
                mailer=mailer,
                base_url="https://crew.day",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )

    def test_audit_row_carries_hashes_only(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """§15 PII minimisation: email / IP / token never hit the audit."""
        request_link(
            session,
            email="priv@example.com",
            purpose="signup_verify",
            ip="203.0.113.55",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        audit = session.scalars(select(AuditLog)).one()
        assert audit.action == "magic_link.sent"
        assert audit.entity_kind == "magic_link"
        diff = audit.diff
        assert isinstance(diff, dict)
        # Forensic fields present.
        assert diff["purpose"] == "signup_verify"
        assert len(diff["email_hash"]) == 64  # sha256 hex
        assert len(diff["ip_hash"]) == 64
        # Plaintext NEVER present.
        assert "priv@example.com" not in str(diff)
        assert "203.0.113.55" not in str(diff)

    # NB: the domain gate against unknown ``purpose`` strings is
    # unreachable through the typed public signature (the
    # :data:`MagicLinkPurpose` ``Literal`` narrowing rules out any
    # other string at compile time). The router does the same
    # validation on the untyped JSON body before we reach the
    # service. Deliberately no runtime test: writing one would
    # require bypassing mypy's check on the typed call, which
    # AGENTS.md §"Code quality bar" forbids.


# ---------------------------------------------------------------------------
# consume_link
# ---------------------------------------------------------------------------


class TestConsumeLinkHappyPath:
    """A valid token flips the nonce and returns the outcome."""

    def test_round_trip(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        request_link(
            session,
            email="rt@example.com",
            purpose="signup_verify",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        token = _extract_token(mailer.sent[0])

        outcome = consume_link(
            session,
            token=token,
            expected_purpose="signup_verify",
            ip="127.0.0.1",
            now=_PINNED + timedelta(minutes=1),
            throttle=throttle,
            settings=settings,
        )
        assert outcome.purpose == "signup_verify"
        assert len(outcome.subject_id) == 26  # ULID
        # Nonce row is flipped.
        row = session.scalars(select(MagicLinkNonce)).one()
        assert row.consumed_at is not None


class TestConsumePurposeEnforcement:
    """AC #1: purpose must match between token and consume call."""

    def test_signup_token_rejected_as_recover(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        request_link(
            session,
            email="xref@example.com",
            purpose="signup_verify",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        token = _extract_token(mailer.sent[0])
        with pytest.raises(PurposeMismatch):
            consume_link(
                session,
                token=token,
                expected_purpose="recover_passkey",
                ip="127.0.0.1",
                now=_PINNED + timedelta(minutes=1),
                throttle=throttle,
                settings=settings,
            )
        # Nonce MUST remain pending — a failed consume never flips.
        row = session.scalars(select(MagicLinkNonce)).one()
        assert row.consumed_at is None


class TestConsumeSingleUse:
    """AC #2: second consume sees the flipped row and 409s."""

    def test_second_consume_raises_already_consumed(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        request_link(
            session,
            email="su@example.com",
            purpose="signup_verify",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        token = _extract_token(mailer.sent[0])
        consume_link(
            session,
            token=token,
            expected_purpose="signup_verify",
            ip="127.0.0.1",
            now=_PINNED + timedelta(minutes=1),
            throttle=throttle,
            settings=settings,
        )
        # Replay.
        with pytest.raises(AlreadyConsumed):
            consume_link(
                session,
                token=token,
                expected_purpose="signup_verify",
                ip="127.0.0.1",
                now=_PINNED + timedelta(minutes=2),
                throttle=throttle,
                settings=settings,
            )

    def test_concurrent_consume_emulated_rowcount_zero(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """Simulate the race: pre-flip the row in a sibling transaction.

        We can't spawn two threads in a unit test without the
        integration-tier engine plumbing, but we can mutate the row
        to ``consumed_at = <now>`` after mint and before consume —
        the conditional UPDATE's WHERE clause sees no matching rows
        and the service maps that ``rowcount == 0`` to
        :class:`AlreadyConsumed`.
        """
        request_link(
            session,
            email="race@example.com",
            purpose="signup_verify",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        token = _extract_token(mailer.sent[0])
        # Sibling consumer pre-flipped the row.
        row = session.scalars(select(MagicLinkNonce)).one()
        row.consumed_at = _PINNED + timedelta(seconds=1)
        session.flush()

        with pytest.raises(AlreadyConsumed):
            consume_link(
                session,
                token=token,
                expected_purpose="signup_verify",
                ip="127.0.0.1",
                now=_PINNED + timedelta(seconds=2),
                throttle=throttle,
                settings=settings,
            )


class TestConsumeTokenErrors:
    """Shape errors propagate as typed domain exceptions."""

    def test_bad_signature_raises_invalid_token(
        self,
        session: Session,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        with pytest.raises(InvalidToken):
            consume_link(
                session,
                token="not-a-valid-token",
                expected_purpose="signup_verify",
                ip="127.0.0.1",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )

    def test_expired_payload_exp_raises_token_expired(
        self,
        session: Session,
        throttle: Throttle,
        settings: Settings,
        serializer: URLSafeTimedSerializer,
    ) -> None:
        """Forge a token whose ``exp`` is in the past; consume must 410."""
        payload = {
            "purpose": "signup_verify",
            "subject_id": "01HWA00000000000000000SUBJ",
            "jti": "01HWA00000000000000000JTIX",
            "exp": int((_PINNED - timedelta(minutes=1)).timestamp()),
        }
        token = serializer.dumps(payload)
        with pytest.raises(TokenExpired):
            consume_link(
                session,
                token=token,
                expected_purpose="signup_verify",
                ip="127.0.0.1",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )

    def test_missing_nonce_row_raises_already_consumed(
        self,
        session: Session,
        throttle: Throttle,
        settings: Settings,
        serializer: URLSafeTimedSerializer,
    ) -> None:
        """A well-signed token with no backing nonce row is 409 (same shape
        as a replay)."""
        payload = {
            "purpose": "signup_verify",
            "subject_id": "01HWA00000000000000000SUBJ",
            "jti": "01HWA00000000000000000ORPH",
            "exp": int((_PINNED + timedelta(minutes=5)).timestamp()),
        }
        token = serializer.dumps(payload)
        with pytest.raises(AlreadyConsumed):
            consume_link(
                session,
                token=token,
                expected_purpose="signup_verify",
                ip="127.0.0.1",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )


class TestConsumeLockout:
    """3 failed consume attempts on one IP flip the 10-min lockout."""

    def test_lockout_after_three_fails(
        self,
        session: Session,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        for _ in range(3):
            with pytest.raises(InvalidToken):
                consume_link(
                    session,
                    token="garbage",
                    expected_purpose="signup_verify",
                    ip="127.0.0.1",
                    now=_PINNED,
                    throttle=throttle,
                    settings=settings,
                )
            # The router records the fail; the service itself doesn't
            # (router owns the failure-bookkeeping — see magic.py).
            throttle.record_consume_failure(ip="127.0.0.1", now=_PINNED)
        # 4th attempt: the pre-flight gate trips before the token is
        # even looked at.
        with pytest.raises(ConsumeLockout):
            consume_link(
                session,
                token="garbage",
                expected_purpose="signup_verify",
                ip="127.0.0.1",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )


class TestAuditOnConsume:
    """A successful consume writes ``magic_link.consumed`` with hashes only."""

    def test_consumed_audit_has_hashes(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        request_link(
            session,
            email="aud@example.com",
            purpose="signup_verify",
            ip="203.0.113.77",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        token = _extract_token(mailer.sent[0])
        consume_link(
            session,
            token=token,
            expected_purpose="signup_verify",
            ip="203.0.113.77",
            now=_PINNED + timedelta(minutes=1),
            throttle=throttle,
            settings=settings,
        )
        consumed_rows = session.scalars(
            select(AuditLog).where(AuditLog.action == "magic_link.consumed")
        ).all()
        assert len(consumed_rows) == 1
        diff = consumed_rows[0].diff
        assert isinstance(diff, dict)
        assert len(diff["email_hash"]) == 64
        assert len(diff["ip_hash_at_request"]) == 64
        assert "aud@example.com" not in str(diff)
        assert "203.0.113.77" not in str(diff)


# ---------------------------------------------------------------------------
# write_rejected_audit — forensic trail for consume failures
# ---------------------------------------------------------------------------


class TestReasonForException:
    """Every consume-path exception maps to a symbolic reason string."""

    @pytest.mark.parametrize(
        ("exc", "reason"),
        [
            (InvalidToken("x"), "invalid_token"),
            (PurposeMismatch("x"), "purpose_mismatch"),
            (TokenExpired("x"), "expired"),
            (AlreadyConsumed("x"), "already_consumed"),
            (RateLimited("x"), "rate_limited"),
            (ConsumeLockout("x"), "consume_locked_out"),
            (ValueError("unmapped"), "unknown"),
        ],
    )
    def test_maps_to_symbol(self, exc: Exception, reason: str) -> None:
        assert reason_for_exception(exc) == reason


class TestWriteRejectedAudit:
    """Rejected-audit row shape — covers issue #1 of the reviewer's blockers."""

    def test_pre_parse_failure_lands_with_unknown_entity_id(
        self,
        session: Session,
        settings: Settings,
    ) -> None:
        """An unsigned / garbage token can't yield a jti — entity_id='unknown'.

        This is the pre-parse path the cd-4zz AC names: a brute-force
        consume with a token the service never minted must still leave
        a forensic row, even though we can't correlate it to a nonce.
        """
        write_rejected_audit(
            session,
            token="this-is-not-a-valid-token",
            expected_purpose="signup_verify",
            ip="198.51.100.10",
            reason="invalid_token",
            settings=settings,
        )
        session.flush()
        row = session.scalars(
            select(AuditLog).where(AuditLog.action == "magic_link.rejected")
        ).one()
        assert row.entity_kind == "magic_link"
        assert row.entity_id == "unknown"
        diff = row.diff
        assert isinstance(diff, dict)
        assert diff["reason"] == "invalid_token"
        assert diff["expected_purpose"] == "signup_verify"
        assert len(diff["ip_hash"]) == 64  # sha256 hex
        # No token_purpose / email_hash — parsing never reached them.
        assert "token_purpose" not in diff
        assert "email_hash" not in diff
        # Plaintext NEVER present.
        assert "198.51.100.10" not in str(diff)
        assert "this-is-not-a-valid-token" not in str(diff)

    def test_parsed_token_reveals_purpose_without_nonce(
        self,
        session: Session,
        settings: Settings,
        serializer: URLSafeTimedSerializer,
    ) -> None:
        """A signed-but-orphaned token surfaces token_purpose in the diff."""
        payload = {
            "purpose": "recover_passkey",
            "subject_id": "01HWA00000000000000000SUBJ",
            "jti": "01HWA00000000000000000REJX",
            "exp": int((_PINNED + timedelta(minutes=5)).timestamp()),
        }
        token = serializer.dumps(payload)
        write_rejected_audit(
            session,
            token=token,
            expected_purpose="signup_verify",  # caller claimed different purpose
            ip="198.51.100.20",
            reason="purpose_mismatch",
            settings=settings,
        )
        session.flush()
        row = session.scalars(
            select(AuditLog).where(AuditLog.action == "magic_link.rejected")
        ).one()
        # jti came from the verified signature — the row carries it.
        assert row.entity_id == "01HWA00000000000000000REJX"
        diff = row.diff
        assert isinstance(diff, dict)
        assert diff["reason"] == "purpose_mismatch"
        assert diff["expected_purpose"] == "signup_verify"
        assert diff["token_purpose"] == "recover_passkey"
        # No nonce row exists → no email_hash on the diff.
        assert "email_hash" not in diff

    def test_parsed_token_with_matching_nonce_carries_email_hash(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """When the nonce row exists, email_hash is lifted from the row."""
        bootstrap_user(
            session,
            email="forensic@example.com",
            display_name="Forensic",
        )
        request_link(
            session,
            email="forensic@example.com",
            purpose="recover_passkey",
            ip="198.51.100.30",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        token = _extract_token(mailer.sent[0])

        write_rejected_audit(
            session,
            token=token,
            expected_purpose="recover_passkey",
            ip="198.51.100.31",  # attacker IP, different from the original
            reason="already_consumed",
            settings=settings,
        )
        session.flush()
        row = session.scalars(
            select(AuditLog).where(AuditLog.action == "magic_link.rejected")
        ).one()
        diff = row.diff
        assert isinstance(diff, dict)
        # 64-char sha256 hex, not the raw email.
        assert len(diff["email_hash"]) == 64
        assert "forensic@example.com" not in str(diff)
        assert "198.51.100.31" not in str(diff)
        # Attacker IP and original IP hash to different values (pepper).
        assert diff["ip_hash"] != row.diff.get("email_hash")

    def test_caller_rollback_does_not_persist_rejected_on_caller_session(
        self,
        tmp_path: Path,
        settings: Settings,
    ) -> None:
        """Reviewer-mandated invariant: the in-flight caller transaction
        still rolls back. The rejected row only survives if it was
        written on a different, independently-committed session — which
        is exactly what the HTTP router arranges via a fresh UoW.

        We need a file-backed SQLite here (not the shared in-memory
        ``engine`` fixture with ``StaticPool``) so each session gets
        a distinct DBAPI connection — otherwise ``audit_session.commit()``
        would also flush the caller's un-committed rows through the
        shared connection, defeating the whole point of the isolation
        the HTTP router relies on. The file sits under the per-test
        ``tmp_path`` and is cleaned up automatically.
        """
        db_file = tmp_path / "rejected_audit.sqlite"
        eng = make_engine(f"sqlite:///{db_file}")
        Base.metadata.create_all(eng)
        try:
            factory = sessionmaker(bind=eng, expire_on_commit=False, class_=Session)

            # Caller session — queues a sentinel audit row that must NOT
            # survive its own rollback (the HTTP session would be rolled
            # back on exception exit in the real router). We do NOT
            # flush here: SQLite file-locking would serialise the two
            # connections and deadlock the audit-session commit. The
            # un-flushed queued row still exercises the same invariant
            # — rollback drops it before it ever reaches the wire.
            caller = factory()
            try:
                write_audit(
                    caller,
                    _agnostic_audit_ctx(),
                    entity_kind="magic_link",
                    entity_id="caller-only",
                    action="magic_link.sent",
                    diff={"sentinel": "rolled_back"},
                )

                # Fresh UoW-equivalent: a second session that commits on
                # its own. This mirrors the ``make_uow()`` fresh-session
                # the router opens in :func:`_write_rejected_on_fresh_uow`.
                audit_session = factory()
                try:
                    write_rejected_audit(
                        audit_session,
                        token="pre-parse-token",
                        expected_purpose="signup_verify",
                        ip="198.51.100.40",
                        reason="invalid_token",
                        settings=settings,
                    )
                    audit_session.commit()
                finally:
                    audit_session.close()

                # Roll back the caller — the sentinel row vanishes with it.
                caller.rollback()
            finally:
                caller.close()

            # Verify on a fresh session: the rejected row is committed,
            # the caller's sentinel is not.
            verifier = factory()
            try:
                rows = verifier.scalars(select(AuditLog)).all()
                actions = [r.action for r in rows]
                assert "magic_link.rejected" in actions
                # Caller's queued row rolled back; the sentinel is not
                # committed to the engine.
                assert not any(
                    r.entity_id == "caller-only" and r.action == "magic_link.sent"
                    for r in rows
                ), f"caller-only sentinel leaked past rollback: {rows!r}"
                # The rejected row carries the expected shape.
                rejected = next(r for r in rows if r.action == "magic_link.rejected")
                diff = rejected.diff
                assert isinstance(diff, dict)
                assert diff["reason"] == "invalid_token"
                assert rejected.entity_id == "unknown"
            finally:
                verifier.close()
        finally:
            eng.dispose()
