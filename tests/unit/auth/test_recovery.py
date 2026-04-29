"""Unit tests for :mod:`app.auth.recovery`.

Covers the three public entry points:

* :func:`request_recovery` — hit / miss branches, enumeration-timing
  hardening, rate-limit trip, audit shape.
* :func:`verify_recovery` — happy path, wrong-purpose token,
  expired token, deleted-user race.
* :func:`complete_recovery` — revoke-all-passkeys,
  revoke-all-sessions, new-credential insert, atomicity on
  register_finish failure, unknown recovery session, audit shape.

The tests exercise the domain service against an in-memory SQLite
engine with the schema created from ``Base.metadata``. The mailer is
a recording double; the py_webauthn attestation verifier is
monkeypatched so we don't need a real authenticator.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service lost-device
recovery", §"Recovery paths" and ``docs/specs/15-security-privacy.md``
§"Self-service lost-device & email-change abuse mitigations".
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import (
    MagicLinkNonce,
    PasskeyCredential,
    User,
)
from app.adapters.db.identity.models import (
    Session as AuthSession,
)
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.auth import passkey as passkey_module
from app.auth import recovery as recovery_module
from app.auth._throttle import RecoveryRateLimited, Throttle
from app.auth.magic_link import (
    AlreadyConsumed,
    PurposeMismatch,
    TokenExpired,
)
from app.auth.magic_link import (
    Throttle as _Throttle,  # noqa: F401 — kept for symmetry
)
from app.auth.passkey import InvalidRegistration
from app.auth.recovery import (
    RecoverySessionExpired,
    RecoverySessionNotFound,
    complete_recovery,
    is_self_service_recovery_disabled,
    prune_expired_recovery_sessions,
    verify_recovery,
)
from app.auth.recovery import request_recovery as _raw_request_recovery
from app.auth.webauthn import VerifiedRegistration
from app.config import Settings
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

_PINNED = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


def request_recovery(*args: object, **kwargs: object) -> None:
    """Module-local shim: call the real :func:`request_recovery` then deliver.

    cd-9slq reshaped the production :func:`request_recovery` to return
    a :class:`PendingDispatch` whose :meth:`deliver` fires every
    queued SMTP send post-commit. Production callers (the recovery
    HTTP router) sandwich a ``UoW.__exit__`` between the two calls so
    a commit failure short-circuits the send. The unit tests in this
    module exercise the domain function in isolation against an
    in-memory engine — none of them sit behind a UoW that needs that
    ordering — so we shim the import to fold the immediate deliver
    back into a single call. Tests that *do* want to stress the
    deferred-send invariant import :func:`_raw_request_recovery`
    directly.
    """
    dispatch = _raw_request_recovery(*args, **kwargs)  # type: ignore[arg-type]
    dispatch.deliver()


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _SentMessage:
    to: list[str]
    subject: str
    body_text: str


@dataclass
class _ExplodingMailer:
    """:class:`Mailer` double that raises a pre-canned exception.

    Used to exercise the §15 enumeration guard in
    :func:`request_recovery`: the hit branch must swallow
    :class:`MailDeliveryError` so a relay outage never turns into a
    5xx. The miss branch sends no email.
    """

    exc: BaseException

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
        del to, subject, body_text, body_html, headers, reply_to
        raise self.exc


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
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-recovery-root-key"),
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


@pytest.fixture(autouse=True)
def reset_recovery_store() -> Iterator[None]:
    """Clear the process-local recovery-session dict between tests.

    The dict is a module-level primitive by design (matches the
    throttle's shape — see :mod:`app.auth.recovery` docstring); we
    clear it both before and after so a crashing test in the same
    worker can't bleed state into the next case.
    """
    recovery_module._RECOVERY_SESSIONS.clear()
    yield
    recovery_module._RECOVERY_SESSIONS.clear()


@pytest.fixture
def redirect_default_engine(
    engine: Engine,
) -> Iterator[None]:
    """Point :func:`app.adapters.db.session.make_uow` at the test engine.

    The kill-switch branch of :func:`request_recovery` writes its
    ``audit.recovery.disabled_by_workspace`` row on a fresh UoW
    (:func:`make_uow`), which reads the module-level default
    sessionmaker. Without this redirect the fresh UoW opens against
    whatever DB the default factory was last built for, the broad
    ``except Exception`` in the helper swallows the cross-DB failure,
    and the audit assertion reads from the test DB where nothing was
    written. Mirrors the shim used in
    ``tests.unit.auth.test_passkey_login``.
    """
    import app.adapters.db.session as _session_mod

    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_magic_token(message: _SentMessage) -> str:
    """Return the token part of a magic-link URL emitted in a mail body.

    The magic-link URL is ``{base}/auth/magic/<token>`` — the token
    sits as the trailing path segment on its own line.
    """
    for line in message.body_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("https://") and "/auth/magic/" in stripped:
            return stripped.rsplit("/", 1)[-1]
    raise AssertionError(f"no /auth/magic/ URL in body: {message.body_text!r}")


def _extract_recovery_token(message: _SentMessage) -> str:
    """Return the token part of a recovery-URL emitted in a mail body.

    The recovery URL is ``{base}/recover/enroll?token=<token>``.
    """
    for line in message.body_text.splitlines():
        stripped = line.strip()
        if "recover/enroll?token=" in stripped:
            return stripped.rsplit("=", 1)[-1]
    raise AssertionError(f"no /recover/enroll URL in body: {message.body_text!r}")


def _verified_response(
    *,
    credential_id: bytes = b"\xaa" * 32,
    public_key: bytes = b"\xbb" * 64,
) -> VerifiedRegistration:
    from webauthn.helpers.structs import (
        AttestationFormat,
        CredentialDeviceType,
        PublicKeyCredentialType,
    )

    return VerifiedRegistration(
        credential_id=credential_id,
        credential_public_key=public_key,
        sign_count=0,
        aaguid="00000000-0000-0000-0000-000000000000",
        fmt=AttestationFormat.NONE,
        credential_type=PublicKeyCredentialType.PUBLIC_KEY,
        user_verified=True,
        attestation_object=b"\x00",
        credential_device_type=CredentialDeviceType.SINGLE_DEVICE,
        credential_backed_up=False,
    )


def _raw_credential() -> dict[str, Any]:
    return {
        "id": "mock",
        "type": "public-key",
        "response": {
            "clientDataJSON": "mock",
            "attestationObject": "mock",
            "transports": ["internal"],
        },
    }


def _stub_verify(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verified: VerifiedRegistration,
) -> None:
    def _fake(**_: Any) -> VerifiedRegistration:
        return verified

    monkeypatch.setattr(passkey_module, "verify_registration", _fake)


def _stub_verify_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.auth.webauthn import InvalidRegistrationResponse

    def _fake(**_: Any) -> VerifiedRegistration:
        raise InvalidRegistrationResponse("attestation rejected")

    monkeypatch.setattr(passkey_module, "verify_registration", _fake)


def _seed_passkeys(session: Session, *, user_id: str, count: int) -> list[bytes]:
    """Seed ``count`` passkey rows for the user; return their raw ids."""
    ids: list[bytes] = []
    for i in range(count):
        cred_id = bytes([0xA0 + i]) * 32
        session.add(
            PasskeyCredential(
                id=cred_id,
                user_id=user_id,
                public_key=b"\x00" * 32,
                sign_count=0,
                backup_eligible=False,
                created_at=_PINNED,
            )
        )
        ids.append(cred_id)
    session.flush()
    return ids


def _seed_auth_sessions(session: Session, *, user_id: str, count: int) -> list[str]:
    """Seed ``count`` web sessions for the user; return their ids.

    The web sessions need a valid workspace FK, so we seed a
    placeholder workspace first (reused across all sessions).
    """
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=f"ws-{workspace_id[:8].lower()}",
            name="Placeholder",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    ids: list[str] = []
    for _ in range(count):
        sid = new_ulid()
        session.add(
            AuthSession(
                id=sid,
                user_id=user_id,
                workspace_id=workspace_id,
                expires_at=_PINNED + timedelta(days=14),
                last_seen_at=_PINNED,
                created_at=_PINNED,
            )
        )
        ids.append(sid)
    session.flush()
    return ids


# ---------------------------------------------------------------------------
# request_recovery
# ---------------------------------------------------------------------------


class TestRequestRecoveryHitBranch:
    """Hit: the submitted email matches a :class:`User` row."""

    def test_mints_magic_link_and_sends_recovery_template(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        user = bootstrap_user(
            session,
            email="rec@example.com",
            display_name="Recovery User",
        )
        request_recovery(
            session,
            email="rec@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )

        # One nonce row with subject = user's id.
        nonce = session.scalars(select(MagicLinkNonce)).one()
        assert nonce.subject_id == user.id
        assert nonce.purpose == "recover_passkey"

        # Exactly one mail — the recovery template. The capturing mailer
        # swallows the magic-link body, so the recording mailer only
        # sees the recovery template.
        assert len(mailer.sent) == 1
        msg = mailer.sent[0]
        assert msg.to == ["rec@example.com"]
        assert "recover" in msg.subject.lower()
        # Body carries the display name (not the email) + the recovery URL.
        assert "Recovery User" in msg.body_text
        assert "recover/enroll?token=" in msg.body_text
        # Body deliberately flags the destructive side-effect.
        assert "revokes" in msg.body_text.lower()

    def test_audit_row_records_hit_with_hashes(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        user = bootstrap_user(
            session,
            email="aud@example.com",
            display_name="Aud",
        )
        request_recovery(
            session,
            email="aud@example.com",
            ip="203.0.113.9",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )

        rows = session.scalars(
            select(AuditLog).where(AuditLog.action == "recovery.requested")
        ).all()
        assert len(rows) == 1
        audit = rows[0]
        assert audit.entity_kind == "user"
        assert audit.entity_id == user.id
        diff = audit.diff
        assert isinstance(diff, dict)
        assert diff["hit"] is True
        assert len(diff["email_hash"]) == 64  # sha256 hex
        assert len(diff["ip_hash"]) == 64
        # Plaintext NEVER present.
        assert "aud@example.com" not in str(diff)
        assert "203.0.113.9" not in str(diff)


class TestRequestRecoveryMissBranch:
    """Miss: no :class:`User` matches the submitted email."""

    def test_unknown_email_sends_no_mail(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        request_recovery(
            session,
            email="ghost@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )

        # No magic-link nonce and no outbound message — unknown email
        # requests do not mint tokens or relay mail.
        assert session.scalars(select(MagicLinkNonce)).all() == []
        assert mailer.sent == []

    def test_audit_row_records_miss_with_hashes(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        request_recovery(
            session,
            email="ghost@example.com",
            ip="198.51.100.11",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )

        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "recovery.requested")
        ).one()
        diff = audit.diff
        assert isinstance(diff, dict)
        assert diff["hit"] is False
        # Entity id is the zero-ULID sentinel — no user to point at.
        assert audit.entity_id == "00000000000000000000000000"
        assert len(diff["email_hash"]) == 64
        assert len(diff["ip_hash"]) == 64
        assert "ghost@example.com" not in str(diff)
        assert "198.51.100.11" not in str(diff)


class TestRequestRecoveryRateLimit:
    """Per-IP / per-email / global caps on recover-start."""

    def test_per_email_cap_trips_after_three(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        bootstrap_user(session, email="slow@example.com", display_name="Slow")
        for _ in range(3):
            request_recovery(
                session,
                email="slow@example.com",
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )
        with pytest.raises(RecoveryRateLimited) as excinfo:
            request_recovery(
                session,
                email="slow@example.com",
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )
        # Email is the tightest cap (3/hour), so it trips before IP.
        assert excinfo.value.scope == "email"
        assert excinfo.value.retry_after_seconds >= 1

    def test_per_ip_cap_trips_at_ten(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """10/IP/hour — distinct emails, same IP, eleventh trips."""
        for i in range(10):
            request_recovery(
                session,
                email=f"u{i}@example.com",
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )
        with pytest.raises(RecoveryRateLimited) as excinfo:
            request_recovery(
                session,
                email="u10@example.com",
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )
        assert excinfo.value.scope == "ip"

    def test_signup_throttle_isolation(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """Filling the signup-start bucket must not affect recover-start."""
        # Fill signup per-IP bucket to its cap (5 for signup-IP). Use
        # distinct email hashes so the tighter per-email cap (3) doesn't
        # trip first — the assertion we want is "recover isn't
        # poisoned by a maxed signup-IP bucket".
        for i in range(5):
            throttle.check_signup_start(
                ip_hash="ip-hash-signup",
                email_hash=f"email-hash-signup-{i}",
                now=_PINNED,
            )
        # Now drive recover — should pass (distinct bucket prefix).
        # Uses a real user so the hit-branch runs and exercises the
        # full recover-start throttle hit.
        bootstrap_user(session, email="iso@example.com", display_name="Iso")
        request_recovery(
            session,
            email="iso@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # Audit row landed — recover bucket was not poisoned.
        assert (
            session.scalars(
                select(AuditLog).where(AuditLog.action == "recovery.requested")
            ).one()
            is not None
        )


class TestRequestRecoveryEnumerationGuard:
    """Both branches write audit; only known users receive mail."""

    def test_hit_and_miss_both_write_audit_but_only_hit_sends_mail(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        # Hit branch.
        bootstrap_user(session, email="hit@example.com", display_name="Hit")
        request_recovery(
            session,
            email="hit@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # Miss branch — different email, same everything else.
        request_recovery(
            session,
            email="miss@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # Two audit rows, one mail — the miss branch audits without
        # sending a message to the unknown address.
        audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "recovery.requested")
        ).all()
        assert len(audits) == 2
        assert len(mailer.sent) == 1
        # One "hit=True", one "hit=False".
        hits = sorted(
            (a.diff["hit"] for a in audits if isinstance(a.diff, dict)),
        )
        assert hits == [False, True]

    def test_hit_branch_swallows_mail_delivery_error(
        self,
        session: Session,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """§15: a mailer outage must not surface as 5xx on the hit path.

        ``request_recovery`` catches :class:`MailDeliveryError` from
        the hit branch so the caller sees an identical 202 whether the
        relay is up or down. The audit row still commits so operators
        can detect the outage from logs.
        """
        from app.adapters.mail.ports import MailDeliveryError

        bootstrap_user(session, email="hit@example.com", display_name="Hit")
        failing_mailer = _ExplodingMailer(MailDeliveryError("relay down"))
        # Must not raise.
        request_recovery(
            session,
            email="hit@example.com",
            ip="127.0.0.1",
            mailer=failing_mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "recovery.requested")
        ).all()
        assert len(audits) == 1
        assert isinstance(audits[0].diff, dict) and audits[0].diff["hit"] is True

    def test_miss_branch_does_not_touch_mailer(
        self,
        session: Session,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """§15: miss branch sends no email but still audits ``hit=False``."""
        from app.adapters.mail.ports import MailDeliveryError

        failing_mailer = _ExplodingMailer(MailDeliveryError("relay down"))
        # Must not raise.
        request_recovery(
            session,
            email="ghost@example.com",
            ip="127.0.0.1",
            mailer=failing_mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "recovery.requested")
        ).all()
        assert len(audits) == 1
        assert isinstance(audits[0].diff, dict) and audits[0].diff["hit"] is False


class TestRequestRecoveryOutboxOrdering:
    """cd-9slq: SMTP send must run *after* recovery audit + nonce
    are durable.

    Mirrors :class:`tests.unit.auth.test_magic_link.TestRequestLinkOutboxOrdering`
    at the recovery-domain layer. Production callers (the recovery
    HTTP router) sequence ``with UoW: request_recovery() → commit →
    dispatch.deliver()``; a commit failure short-circuits both queued
    sends (the recovery template on the hit branch), so no email
    leaves the host with a rolled-back ``MagicLinkNonce`` /
    ``audit.recovery.requested`` row.
    """

    def test_pending_returned_does_not_send_email_until_deliver(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """``request_recovery`` queues writes + the deferred sends; mailer
        stays untouched until the caller invokes
        :meth:`PendingDispatch.deliver`.
        """
        bootstrap_user(session, email="lazy@example.com", display_name="Lazy")
        dispatch = _raw_request_recovery(
            session,
            email="lazy@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # Nonce + audit queued on the session — visible pre-commit.
        nonces = session.scalars(select(MagicLinkNonce)).all()
        assert len(nonces) == 1
        # Recording mailer is untouched until ``dispatch.deliver()``
        # fires (the in-process capturing mailer fired but doesn't
        # send anything outbound).
        assert mailer.sent == [], (
            f"recording mailer fired before deliver(): {mailer.sent!r}"
        )
        dispatch.deliver()
        assert len(mailer.sent) == 1

    def test_commit_failure_before_deliver_does_not_send_email(
        self,
        session: Session,
        engine: Engine,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """The cd-t2jz repro on the recovery domain: commit fails →
        no email goes out (hit branch).
        """
        bootstrap_user(session, email="outbox@example.com", display_name="Outbox")
        original_commit = session.commit

        def _failing_commit() -> None:
            session.rollback()
            raise RuntimeError("simulated commit failure")

        session.commit = _failing_commit  # type: ignore[method-assign]
        dispatch = None
        try:
            dispatch = _raw_request_recovery(
                session,
                email="outbox@example.com",
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )
            with pytest.raises(RuntimeError, match="simulated commit failure"):
                session.commit()
        finally:
            session.commit = original_commit  # type: ignore[method-assign]

        # Recording mailer was never invoked — the recovery template
        # send is queued post-commit and the failure short-circuits
        # ``dispatch.deliver()`` per cd-9slq.
        assert mailer.sent == [], (
            f"mailer was invoked despite commit failure: {mailer.sent!r}"
        )
        # The rolled-back nonce + audit are gone.
        assert session.scalars(select(MagicLinkNonce)).all() == []
        assert (
            session.scalars(
                select(AuditLog).where(AuditLog.action == "recovery.requested")
            ).all()
            == []
        )
        assert dispatch is not None

    def test_commit_failure_on_miss_branch_does_not_send_email(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """Miss branch: commit failure still sends no email."""
        original_commit = session.commit

        def _failing_commit() -> None:
            session.rollback()
            raise RuntimeError("simulated commit failure")

        session.commit = _failing_commit  # type: ignore[method-assign]
        try:
            _raw_request_recovery(
                session,
                email="ghost@example.com",  # no user row → miss branch
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )
            with pytest.raises(RuntimeError, match="simulated commit failure"):
                session.commit()
        finally:
            session.commit = original_commit  # type: ignore[method-assign]

        # No mail leaves on either branch when commit fails.
        assert mailer.sent == [], (
            f"miss-branch mailer was invoked despite commit failure: {mailer.sent!r}"
        )
        # The rolled-back audit row is gone.
        assert (
            session.scalars(
                select(AuditLog).where(AuditLog.action == "recovery.requested")
            ).all()
            == []
        )


# ---------------------------------------------------------------------------
# verify_recovery
# ---------------------------------------------------------------------------


class TestVerifyRecoveryHappyPath:
    """A valid recovery token mints a recovery session + audits."""

    def test_round_trip(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        user = bootstrap_user(
            session,
            email="verify@example.com",
            display_name="Verify",
        )
        request_recovery(
            session,
            email="verify@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # Pull the magic token from the persisted nonce row so we don't
        # have to read through the capturing-mailer path.
        nonce = session.scalars(select(MagicLinkNonce)).one()
        # The recovery body carries the token in the query-string.
        token_in_mail = _extract_recovery_token(mailer.sent[0])
        del nonce  # subject_id verified by the service call below

        ssn = verify_recovery(
            session,
            token=token_in_mail,
            ip="127.0.0.1",
            now=_PINNED + timedelta(minutes=1),
            throttle=throttle,
            settings=settings,
        )
        assert ssn.user_id == user.id
        assert len(ssn.recovery_session_id) == 26  # ULID
        assert len(ssn.email_hash) == 64
        assert len(ssn.ip_hash) == 64

        # Recovery session stored for later finish.
        assert ssn.recovery_session_id in recovery_module._RECOVERY_SESSIONS

        # Audit row lands.
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "recovery.verified")
        ).one()
        assert audit.entity_id == user.id


class TestVerifyRecoveryTokenErrors:
    """Typed domain errors bubble through unchanged."""

    def test_wrong_purpose_token_raises_purpose_mismatch(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        # Mint a signup-verify token instead of a recover one.
        from app.auth.magic_link import request_link

        bootstrap_user(session, email="xref@example.com", display_name="Xref")
        # cd-9i7z: ``request_link`` now returns a deferred-send
        # :class:`PendingMagicLink`; fire the send immediately so
        # the test's recording mailer captures the body.
        pending = request_link(
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
        assert pending is not None
        pending.deliver()
        token = _extract_magic_token(mailer.sent[0])
        with pytest.raises(PurposeMismatch):
            verify_recovery(
                session,
                token=token,
                ip="127.0.0.1",
                now=_PINNED + timedelta(minutes=1),
                throttle=throttle,
                settings=settings,
            )

    def test_expired_token_raises_token_expired(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        bootstrap_user(session, email="exp@example.com", display_name="Exp")
        request_recovery(
            session,
            email="exp@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        token = _extract_recovery_token(mailer.sent[0])
        # Advance past the 10-min recover-purpose TTL ceiling.
        with pytest.raises(TokenExpired):
            verify_recovery(
                session,
                token=token,
                ip="127.0.0.1",
                now=_PINNED + timedelta(hours=1),
                throttle=throttle,
                settings=settings,
            )

    def test_deleted_user_between_request_and_verify_raises_not_found(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """Edge case: user row disappeared after magic-link request.

        Surfaces as :class:`RecoverySessionNotFound` — the router
        collapses this onto 404 and we don't leak the deletion
        through a distinct error code.
        """
        user = bootstrap_user(session, email="gone@example.com", display_name="Gone")
        request_recovery(
            session,
            email="gone@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        token = _extract_recovery_token(mailer.sent[0])
        # Nuke the user.
        session.delete(user)
        session.flush()
        with pytest.raises(RecoverySessionNotFound):
            verify_recovery(
                session,
                token=token,
                ip="127.0.0.1",
                now=_PINNED + timedelta(minutes=1),
                throttle=throttle,
                settings=settings,
            )

    def test_replay_raises_already_consumed(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        bootstrap_user(session, email="replay@example.com", display_name="Replay")
        request_recovery(
            session,
            email="replay@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        token = _extract_recovery_token(mailer.sent[0])
        verify_recovery(
            session,
            token=token,
            ip="127.0.0.1",
            now=_PINNED + timedelta(minutes=1),
            throttle=throttle,
            settings=settings,
        )
        with pytest.raises(AlreadyConsumed):
            verify_recovery(
                session,
                token=token,
                ip="127.0.0.1",
                now=_PINNED + timedelta(minutes=2),
                throttle=throttle,
                settings=settings,
            )


# ---------------------------------------------------------------------------
# complete_recovery
# ---------------------------------------------------------------------------


def _set_up_verified_recovery(
    session: Session,
    *,
    mailer: _RecordingMailer,
    throttle: Throttle,
    settings: Settings,
    email: str = "finish@example.com",
    display_name: str = "Finish",
) -> tuple[User, str]:
    """Helper: run the full request + verify flow so a recovery session exists.

    Returns the user and the recovery-session id. Exercises the
    request + verify paths against the DB to avoid synthetic dict
    pokes that could drift from the real service flow.
    """
    user = bootstrap_user(session, email=email, display_name=display_name)
    request_recovery(
        session,
        email=email,
        ip="127.0.0.1",
        mailer=mailer,
        base_url="https://crew.day",
        now=_PINNED,
        throttle=throttle,
        settings=settings,
    )
    token = _extract_recovery_token(mailer.sent[-1])
    ssn = verify_recovery(
        session,
        token=token,
        ip="127.0.0.1",
        now=_PINNED + timedelta(minutes=1),
        throttle=throttle,
        settings=settings,
    )
    return user, ssn.recovery_session_id


class TestCompleteRecoveryHappyPath:
    """The final step: revoke old creds + sessions, register new passkey."""

    def test_revokes_all_passkeys_and_sessions_and_inserts_new(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user, recovery_id = _set_up_verified_recovery(
            session, mailer=mailer, throttle=throttle, settings=settings
        )
        # Seed 3 existing passkeys + 2 existing sessions.
        _seed_passkeys(session, user_id=user.id, count=3)
        _seed_auth_sessions(session, user_id=user.id, count=2)

        # Mint the recovery-start challenge.
        opts = passkey_module.register_start_recovery(session, user_id=user.id)

        verified = _verified_response(credential_id=b"\xee" * 32)
        _stub_verify(monkeypatch, verified=verified)

        result = complete_recovery(
            session,
            recovery_session_id=recovery_id,
            challenge_id=opts.challenge_id,
            credential=_raw_credential(),
            ip="127.0.0.1",
            now=_PINNED + timedelta(minutes=2),
            settings=settings,
        )

        assert result.user_id == user.id
        assert result.revoked_credential_count == 3
        assert result.revoked_session_count == 2
        assert len(result.new_credential_id) > 0

        # Every prior passkey gone.
        remaining = session.scalars(
            select(PasskeyCredential).where(PasskeyCredential.user_id == user.id)
        ).all()
        assert len(remaining) == 1  # only the new one
        assert remaining[0].id == verified.credential_id

        # Every prior web session INVALIDATED (cd-geqp) — rows survive
        # for forensics but carry ``invalidated_at`` / ``invalidation_
        # cause = "recovery_consumed"`` so :func:`validate` refuses
        # them.
        prior_rows = session.scalars(
            select(AuthSession).where(AuthSession.user_id == user.id)
        ).all()
        assert len(prior_rows) == 2
        for row in prior_rows:
            assert row.invalidated_at is not None
            assert row.invalidation_cause == "recovery_consumed"

        # Recovery session consumed.
        assert recovery_id not in recovery_module._RECOVERY_SESSIONS

        # Audit row landed.
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "recovery.completed")
        ).one()
        assert audit.entity_id == user.id
        diff = audit.diff
        assert isinstance(diff, dict)
        assert diff["revoked_credential_count"] == 3
        assert diff["revoked_session_count"] == 2
        assert len(diff["ip_hash_at_completion"]) == 64
        assert len(diff["email_hash"]) == 64
        # Plaintext never present.
        assert "finish@example.com" not in str(diff)
        assert "127.0.0.1" not in str(diff)


class TestCompleteRecoveryAtomicity:
    """If register_finish raises, NOTHING should be persisted."""

    def test_rollback_restores_all_prior_rows(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user, recovery_id = _set_up_verified_recovery(
            session, mailer=mailer, throttle=throttle, settings=settings
        )
        # Seed 4 existing passkeys.
        old_ids = _seed_passkeys(session, user_id=user.id, count=4)
        _seed_auth_sessions(session, user_id=user.id, count=2)
        session.commit()

        opts = passkey_module.register_start_recovery(session, user_id=user.id)
        session.commit()
        _stub_verify_raises(monkeypatch)  # force :class:`InvalidRegistration`

        with pytest.raises(InvalidRegistration):
            complete_recovery(
                session,
                recovery_session_id=recovery_id,
                challenge_id=opts.challenge_id,
                credential=_raw_credential(),
                ip="127.0.0.1",
                now=_PINNED + timedelta(minutes=2),
                settings=settings,
            )
        # Caller's UoW owns the rollback — simulate it.
        session.rollback()

        # All old passkey rows still there.
        remaining_ids = {
            row.id
            for row in session.scalars(
                select(PasskeyCredential).where(PasskeyCredential.user_id == user.id)
            ).all()
        }
        assert remaining_ids == set(old_ids)

        # Recovery session NOT consumed — it stays live for a retry.
        assert recovery_id in recovery_module._RECOVERY_SESSIONS


class TestCompleteRecoveryUnknownSession:
    """Unknown / expired recovery session → 404-equivalent."""

    def test_unknown_recovery_session_raises(
        self,
        session: Session,
        settings: Settings,
    ) -> None:
        with pytest.raises(RecoverySessionNotFound):
            complete_recovery(
                session,
                recovery_session_id="01HWA00000000000000000NONE",
                challenge_id="01HWA00000000000000000CHG0",
                credential=_raw_credential(),
                ip="127.0.0.1",
                settings=settings,
            )

    def test_expired_recovery_session_raises(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        _user, recovery_id = _set_up_verified_recovery(
            session, mailer=mailer, throttle=throttle, settings=settings
        )
        # Advance past the 15-min recovery-session TTL.
        with pytest.raises(RecoverySessionExpired):
            complete_recovery(
                session,
                recovery_session_id=recovery_id,
                challenge_id="01HWA00000000000000000CHGx",
                credential=_raw_credential(),
                ip="127.0.0.1",
                now=_PINNED + timedelta(hours=1),
                settings=settings,
            )
        # Expired row evicted in passing — a retry sees "not found".
        with pytest.raises(RecoverySessionNotFound):
            complete_recovery(
                session,
                recovery_session_id=recovery_id,
                challenge_id="01HWA00000000000000000CHGx",
                credential=_raw_credential(),
                ip="127.0.0.1",
                now=_PINNED + timedelta(hours=2),
                settings=settings,
            )


class TestPruneExpiredRecoverySessions:
    """GC helper drops expired rows from the in-memory store."""

    def test_prune_drops_expired_keeps_live(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        # One live, one expired.
        _set_up_verified_recovery(
            session,
            mailer=mailer,
            throttle=throttle,
            settings=settings,
            email="live@example.com",
            display_name="Live",
        )
        _set_up_verified_recovery(
            session,
            mailer=mailer,
            throttle=throttle,
            settings=settings,
            email="stale@example.com",
            display_name="Stale",
        )
        # Advance well past the TTL for BOTH sessions.
        dropped = prune_expired_recovery_sessions(now=_PINNED + timedelta(hours=2))
        assert dropped == 2
        assert recovery_module._RECOVERY_SESSIONS == {}

    def test_prune_keeps_unexpired(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        _set_up_verified_recovery(
            session,
            mailer=mailer,
            throttle=throttle,
            settings=settings,
        )
        dropped = prune_expired_recovery_sessions(now=_PINNED + timedelta(minutes=5))
        assert dropped == 0
        assert len(recovery_module._RECOVERY_SESSIONS) == 1


# ---------------------------------------------------------------------------
# Cross-cutting: audit PII minimisation
# ---------------------------------------------------------------------------


class TestAuditPIIMinimisation:
    """Every audit row across the three actions carries hashes only."""

    def test_full_flow_audits_carry_no_plaintext(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user, recovery_id = _set_up_verified_recovery(
            session,
            mailer=mailer,
            throttle=throttle,
            settings=settings,
            email="pii@example.com",
            display_name="PII Watch",
        )
        opts = passkey_module.register_start_recovery(session, user_id=user.id)
        _stub_verify(monkeypatch, verified=_verified_response())
        complete_recovery(
            session,
            recovery_session_id=recovery_id,
            challenge_id=opts.challenge_id,
            credential=_raw_credential(),
            ip="203.0.113.77",
            now=_PINNED + timedelta(minutes=2),
            settings=settings,
        )
        rows = session.scalars(
            select(AuditLog).where(AuditLog.action.like("recovery.%"))
        ).all()
        assert len(rows) == 3
        for row in rows:
            body = str(row.diff)
            assert "pii@example.com" not in body
            assert "203.0.113.77" not in body
            assert "127.0.0.1" not in body  # request IP


# ---------------------------------------------------------------------------
# Workspace kill-switch (auth.self_service_recovery_enabled)
# ---------------------------------------------------------------------------


def _seed_workspace(
    session: Session,
    *,
    slug: str,
    settings_json: dict[str, Any] | None = None,
) -> str:
    """Seed one :class:`Workspace` row; return its id.

    Kept narrow: kill-switch tests only care about the ``settings_json``
    blob and the workspace id, so the helper intentionally sidesteps
    the richer :func:`tests.factories.identity.bootstrap_workspace`
    which also seeds the ``owners`` permission group + membership.
    Those rows would pull in ``user_workspace`` / ``permission_group``
    /  ``permission_group_member`` (and their FKs), none of which the
    kill-switch helper reads.
    """
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=slug.replace("-", " ").title(),
            plan="free",
            quota_json={},
            settings_json=settings_json if settings_json is not None else {},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


def _seed_role_grant(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
    grant_role: str = "worker",
) -> str:
    """Seed one :class:`RoleGrant` row; return its id."""
    grant_id = new_ulid()
    session.add(
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
    session.flush()
    return grant_id


class TestIsSelfServiceRecoveryDisabled:
    """Unit coverage of the ``auth.self_service_recovery_enabled`` gate.

    The helper walks the user's grants → workspaces and returns
    ``True`` iff any workspace has the flag explicitly set to
    ``False``. Covers the absent-key default, missing-grant empty
    path, single-workspace flip, multi-workspace most-restrictive-
    wins, and the "non-bool value is ignored (fail-open)" fallback.
    """

    def test_no_grants_returns_false(
        self,
        session: Session,
    ) -> None:
        """A user with no grants is never kill-switched — there's no
        workspace to impose the setting on them."""
        user = bootstrap_user(session, email="nog@example.com", display_name="NoG")
        assert is_self_service_recovery_disabled(session, user_id=user.id) is False

    def test_default_absent_key_returns_false(
        self,
        session: Session,
    ) -> None:
        """An empty ``settings_json`` → flag defaults to ``True`` →
        helper returns ``False`` (not disabled)."""
        user = bootstrap_user(session, email="dflt@example.com", display_name="Dflt")
        ws_id = _seed_workspace(session, slug="default-settings")
        _seed_role_grant(session, workspace_id=ws_id, user_id=user.id)
        assert is_self_service_recovery_disabled(session, user_id=user.id) is False

    def test_explicit_true_returns_false(
        self,
        session: Session,
    ) -> None:
        """An operator who wrote the flag ``True`` explicitly matches
        the catalog default; the helper returns ``False``."""
        user = bootstrap_user(session, email="on@example.com", display_name="On")
        ws_id = _seed_workspace(
            session,
            slug="explicit-true",
            settings_json={"auth.self_service_recovery_enabled": True},
        )
        _seed_role_grant(session, workspace_id=ws_id, user_id=user.id)
        assert is_self_service_recovery_disabled(session, user_id=user.id) is False

    def test_single_workspace_false_returns_true(
        self,
        session: Session,
    ) -> None:
        """The baseline kill-switch: one workspace flips the flag and
        the user is disabled."""
        user = bootstrap_user(session, email="off@example.com", display_name="Off")
        ws_id = _seed_workspace(
            session,
            slug="single-off",
            settings_json={"auth.self_service_recovery_enabled": False},
        )
        _seed_role_grant(session, workspace_id=ws_id, user_id=user.id)
        assert is_self_service_recovery_disabled(session, user_id=user.id) is True

    def test_most_restrictive_wins(
        self,
        session: Session,
    ) -> None:
        """One-of-many workspaces flipped is enough (§03 "Workspace
        kill-switch")."""
        user = bootstrap_user(session, email="mrw@example.com", display_name="MRW")
        ok_ws = _seed_workspace(session, slug="mrw-ok")
        flipped_ws = _seed_workspace(
            session,
            slug="mrw-off",
            settings_json={"auth.self_service_recovery_enabled": False},
        )
        third_ws = _seed_workspace(
            session,
            slug="mrw-also-ok",
            settings_json={"auth.self_service_recovery_enabled": True},
        )
        _seed_role_grant(session, workspace_id=ok_ws, user_id=user.id)
        _seed_role_grant(session, workspace_id=flipped_ws, user_id=user.id)
        _seed_role_grant(
            session, workspace_id=third_ws, user_id=user.id, grant_role="manager"
        )
        assert is_self_service_recovery_disabled(session, user_id=user.id) is True

    def test_other_user_grants_do_not_leak(
        self,
        session: Session,
    ) -> None:
        """The helper scopes the walk to its ``user_id`` argument — a
        sibling user in a kill-switched workspace must not disable the
        target user."""
        target = bootstrap_user(session, email="t@example.com", display_name="T")
        other = bootstrap_user(session, email="o@example.com", display_name="O")
        flipped_ws = _seed_workspace(
            session,
            slug="leak-off",
            settings_json={"auth.self_service_recovery_enabled": False},
        )
        ok_ws = _seed_workspace(session, slug="leak-ok")
        # Other user is kill-switched; target user is in a healthy workspace.
        _seed_role_grant(session, workspace_id=flipped_ws, user_id=other.id)
        _seed_role_grant(session, workspace_id=ok_ws, user_id=target.id)
        assert is_self_service_recovery_disabled(session, user_id=target.id) is False
        assert is_self_service_recovery_disabled(session, user_id=other.id) is True

    def test_non_bool_value_is_ignored(
        self,
        session: Session,
    ) -> None:
        """A corrupt / typo'd payload (``"false"`` string vs ``False``
        bool) fails open — we only disable on the explicit operator
        choice, not on a truthy-adjacent value. The ``is False``
        check in the helper enforces strict bool semantics."""
        user = bootstrap_user(session, email="corrupt@example.com", display_name="C")
        ws_id = _seed_workspace(
            session,
            slug="corrupt-payload",
            # String "false" rather than the bool — a misuse the admin
            # UI should reject, but the resolver treats as "not
            # explicitly False" → fail-open.
            settings_json={"auth.self_service_recovery_enabled": "false"},
        )
        _seed_role_grant(session, workspace_id=ws_id, user_id=user.id)
        assert is_self_service_recovery_disabled(session, user_id=user.id) is False

    def test_multiple_grants_same_workspace_deduped_behaviour(
        self,
        session: Session,
    ) -> None:
        """Two grants in the same kill-switched workspace still disable
        the user (the SELECT returns the same ``settings_json`` twice
        — the helper short-circuits on the first ``False``)."""
        user = bootstrap_user(session, email="dup@example.com", display_name="Dup")
        ws_id = _seed_workspace(
            session,
            slug="dup-off",
            settings_json={"auth.self_service_recovery_enabled": False},
        )
        # Two distinct grants (a workspace-scope worker + a property-
        # scope client is valid v1; we fake the second with a worker
        # grant on NULL scope to stay within the v1 slice — the
        # kill-switch helper doesn't care about role shape).
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="worker"
        )
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="manager"
        )
        assert is_self_service_recovery_disabled(session, user_id=user.id) is True


class TestRequestRecoveryKillSwitch:
    """The kill-switch gate is wired into :func:`request_recovery`.

    Covers the full behavioural contract: 202 response (same wire
    shape), no magic-link nonce, no mailer send, and the
    ``recovery.disabled_by_workspace`` audit on a fresh UoW.
    """

    def test_kill_switched_user_no_mail_no_nonce(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        redirect_default_engine: None,
    ) -> None:
        user = bootstrap_user(
            session, email="ks@example.com", display_name="Kill Switch"
        )
        ws_id = _seed_workspace(
            session,
            slug="ks-off",
            settings_json={"auth.self_service_recovery_enabled": False},
        )
        _seed_role_grant(session, workspace_id=ws_id, user_id=user.id)
        session.commit()

        request_recovery(
            session,
            email="ks@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # No nonce row, no mail — the whole hit branch was skipped.
        assert session.scalars(select(MagicLinkNonce)).all() == []
        assert mailer.sent == []

    def test_kill_switched_user_writes_disabled_audit_on_fresh_uow(
        self,
        engine: Engine,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        redirect_default_engine: None,
    ) -> None:
        """The audit lands on a fresh UoW (not the caller's session).

        We commit the caller's session so the user / workspace / grant
        rows are visible to the fresh UoW, then call
        :func:`request_recovery`. The kill-switch branch opens its
        own ``make_uow`` — the audit row lands on the shared engine
        and we read it back through a sibling session.
        """
        user = bootstrap_user(session, email="ksa@example.com", display_name="Audit")
        ws_id = _seed_workspace(
            session,
            slug="ksa-off",
            settings_json={"auth.self_service_recovery_enabled": False},
        )
        _seed_role_grant(session, workspace_id=ws_id, user_id=user.id)
        session.commit()

        request_recovery(
            session,
            email="ksa@example.com",
            ip="198.51.100.22",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )

        # Sibling session so we see rows the fresh UoW committed.
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as reader:
            rows = reader.scalars(
                select(AuditLog).where(
                    AuditLog.action == "recovery.disabled_by_workspace"
                )
            ).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.entity_kind == "user"
        assert row.entity_id == user.id
        assert isinstance(row.diff, dict)
        assert row.diff["reason"] == "workspace_kill_switch"
        assert len(row.diff["email_hash"]) == 64  # sha256 hex
        assert len(row.diff["ip_hash"]) == 64
        # No plaintext PII in the audit payload.
        assert "ksa@example.com" not in str(row.diff)
        assert "198.51.100.22" not in str(row.diff)
        # Caller's UoW wrote NO ``recovery.requested`` row — the
        # kill-switch branch short-circuits before that audit.
        assert (
            session.scalars(
                select(AuditLog).where(AuditLog.action == "recovery.requested")
            ).all()
            == []
        )

    def test_kill_switched_user_does_not_raise(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        redirect_default_engine: None,
    ) -> None:
        """The enumeration-guard contract: 202-on-the-wire requires
        :func:`request_recovery` to complete without raising on
        every branch. :func:`request_recovery` is typed ``-> None``,
        so we assert the call returns (no exception) and leave the
        domain-state assertions to the sibling "no_mail_no_nonce"
        case above; strict mypy flags ``assert result is None`` as
        a no-op when the callee is declared to return ``None``.
        """
        user = bootstrap_user(session, email="ret@example.com", display_name="Ret")
        ws_id = _seed_workspace(
            session,
            slug="ret-off",
            settings_json={"auth.self_service_recovery_enabled": False},
        )
        _seed_role_grant(session, workspace_id=ws_id, user_id=user.id)
        session.commit()

        request_recovery(
            session,
            email="ret@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )

    def test_user_with_all_flags_true_takes_happy_path(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """A user whose every workspace carries the flag ``True`` flows
        through the normal hit branch — magic-link nonce, recovery
        mail, ``recovery.requested`` audit."""
        user = bootstrap_user(session, email="happy@example.com", display_name="Happy")
        ws_id = _seed_workspace(
            session,
            slug="happy-on",
            settings_json={"auth.self_service_recovery_enabled": True},
        )
        _seed_role_grant(session, workspace_id=ws_id, user_id=user.id)

        request_recovery(
            session,
            email="happy@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # Full happy path: nonce + mail + normal audit.
        assert len(session.scalars(select(MagicLinkNonce)).all()) == 1
        assert len(mailer.sent) == 1
        audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "recovery.requested")
        ).all()
        assert len(audits) == 1

    def test_user_with_archived_grant_is_not_blocked(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """Grants whose workspace has been soft-retired don't count.

        §03 "Workspace kill-switch" is clear: only **non-archived**
        grants feed the most-restrictive-wins decision. The v1
        ``role_grant`` schema doesn't carry ``revoked_at`` — revocation
        hard-deletes the row today (see
        :mod:`app.domain.identity.role_grants`). The forward-compat
        contract we test here is the behavioural one: a user whose
        ONLY kill-switched workspace is represented by a deleted grant
        must flow through the happy path. cd-x1xh (role_grant soft-
        retire columns) extends the WHERE clause to filter on
        ``revoked_at IS NULL`` without changing this test.
        """
        user = bootstrap_user(session, email="arc@example.com", display_name="Arc")
        active_ws = _seed_workspace(session, slug="arc-active")
        archived_ws = _seed_workspace(
            session,
            slug="arc-archived",
            settings_json={"auth.self_service_recovery_enabled": False},
        )
        # Active grant in the healthy workspace.
        _seed_role_grant(session, workspace_id=active_ws, user_id=user.id)
        # Grant in the kill-switched workspace, then "archive" it the
        # v1 way — delete the row. When cd-x1xh lands ``revoked_at``,
        # this will become a soft-revoke + WHERE-clause filter; the
        # assertion stays the same.
        arc_grant_id = _seed_role_grant(
            session, workspace_id=archived_ws, user_id=user.id
        )
        session.execute(delete(RoleGrant).where(RoleGrant.id == arc_grant_id))
        session.flush()

        request_recovery(
            session,
            email="arc@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # Happy path — magic link minted + mailed.
        assert len(session.scalars(select(MagicLinkNonce)).all()) == 1
        assert len(mailer.sent) == 1
        assert (
            session.scalars(
                select(AuditLog).where(
                    AuditLog.action == "recovery.disabled_by_workspace"
                )
            ).all()
            == []
        )

    def test_unknown_email_still_uses_normal_miss_branch(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """The kill-switch gate runs AFTER user lookup; an unknown
        email never reaches the helper and falls through to the
        normal miss branch (enumeration guard)."""
        # Seed a kill-switched workspace so the helper *would* disable
        # recovery for any user grant-mapped to it — but the inbound
        # email doesn't match any user, so the gate is never consulted.
        ws_id = _seed_workspace(
            session,
            slug="miss-ws-off",
            settings_json={"auth.self_service_recovery_enabled": False},
        )
        del ws_id  # Seed only; no grants.

        request_recovery(
            session,
            email="ghost-miss@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # Normal miss: no mail, zero nonces, zero disabled-by-workspace
        # audits, one hit=False audit.
        assert mailer.sent == []
        assert session.scalars(select(MagicLinkNonce)).all() == []
        audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "recovery.requested")
        ).all()
        assert len(audits) == 1
        assert isinstance(audits[0].diff, dict)
        assert audits[0].diff["hit"] is False
        assert (
            session.scalars(
                select(AuditLog).where(
                    AuditLog.action == "recovery.disabled_by_workspace"
                )
            ).all()
            == []
        )


# ---------------------------------------------------------------------------
# Step-up gate (cd-gh7l) — break-glass code is required for managers /
# owners-group members. Workers / clients / guests bypass the gate
# entirely; the gate runs AFTER the kill-switch check.
# ---------------------------------------------------------------------------


def _seed_break_glass_code_for_user(
    session: Session,
    *,
    user_id: str,
    workspace_id: str,
    plaintext: str,
    used_at: datetime | None = None,
) -> str:
    """Seed one :class:`BreakGlassCode` row hashed under the live argon2id.

    Returns the row id. Wrapping in ``tenant_agnostic`` mirrors the
    redemption-path posture inside :mod:`app.auth.break_glass`.
    """
    from app.adapters.db.identity.models import BreakGlassCode
    from app.auth import break_glass as bg_module
    from app.tenancy import tenant_agnostic

    code_id = new_ulid()
    code_hash = bg_module._HASHER.hash(plaintext)
    with tenant_agnostic():
        session.add(
            BreakGlassCode(
                id=code_id,
                workspace_id=workspace_id,
                user_id=user_id,
                hash=code_hash,
                hash_params={
                    "time_cost": 3,
                    "memory_cost": 65536,
                    "parallelism": 4,
                },
                created_at=_PINNED,
                used_at=used_at,
                consumed_magic_link_id=None,
            )
        )
        session.flush()
    return code_id


def _seed_owners_group_for_user(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
) -> None:
    """Seed an ``owners``-group row + membership in one workspace."""
    from app.adapters.db.authz.models import (
        PermissionGroup,
        PermissionGroupMember,
    )

    group_id = new_ulid()
    session.add(
        PermissionGroup(
            id=group_id,
            workspace_id=workspace_id,
            slug="owners",
            name="Owners",
            system=True,
            capabilities_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    session.add(
        PermissionGroupMember(
            group_id=group_id,
            user_id=user_id,
            workspace_id=workspace_id,
            added_at=_PINNED,
            added_by_user_id=None,
        )
    )
    session.flush()


@pytest.fixture(autouse=True)
def reset_break_glass_rate_limit() -> Iterator[None]:
    """Clear the per-user break-glass redemption counters between cases.

    The lockout state is process-local; without this fixture a test
    that lands a user in lockout would bleed the state into the next
    case.
    """
    from app.auth.break_glass import reset_rate_limit_for_tests

    reset_rate_limit_for_tests()
    yield
    reset_rate_limit_for_tests()


class TestRequestRecoveryStepUp:
    """Spec §03 step 3 + §15 "Step-up bypass is not a fallback".

    Five branches:

    * non-step-up + no code → normal hit branch.
    * non-step-up + code → code IGNORED (never burnt) + normal hit.
    * step-up + no code → ``stepup_missing`` audit, no mail, no nonce.
    * step-up + wrong code → ``stepup_invalid`` audit, no mail, no nonce.
    * step-up + valid code → mail + code burnt + audit ``stepup=True``.
    """

    def test_worker_with_no_code_takes_happy_path(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        user = bootstrap_user(session, email="w@example.com", display_name="W")
        ws_id = _seed_workspace(session, slug="w-ws")
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="worker"
        )
        request_recovery(
            session,
            email="w@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # Normal hit branch: nonce + mail + recovery.requested audit
        # WITHOUT a stepup flag.
        assert len(session.scalars(select(MagicLinkNonce)).all()) == 1
        assert len(mailer.sent) == 1
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "recovery.requested")
        ).one()
        assert isinstance(audit.diff, dict)
        assert audit.diff["hit"] is True
        # ``stepup`` is only emitted on the step-up redeem branch.
        assert "stepup" not in audit.diff

    def test_worker_with_code_does_not_burn(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """A worker who submits a code → code IGNORED (never burnt).

        §03 step 4: "For the non-step-up population (workers, clients,
        guests with no manager or owners membership anywhere), the
        code field is ignored and never burnt".
        """
        from app.adapters.db.identity.models import BreakGlassCode

        user = bootstrap_user(session, email="wc@example.com", display_name="WC")
        ws_id = _seed_workspace(session, slug="wc-ws")
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="worker"
        )
        # Even if the worker happens to have a code in the DB (e.g.
        # legacy from when they were a manager), the redemption walk
        # is never invoked for non-step-up users, so the code stays
        # unused.
        code_id = _seed_break_glass_code_for_user(
            session,
            user_id=user.id,
            workspace_id=ws_id,
            plaintext="legacy-code",
        )
        request_recovery(
            session,
            email="wc@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            break_glass_code="legacy-code",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # Hit path executed (mail + nonce).
        assert len(mailer.sent) == 1
        # Code NOT burnt — non-step-up flow ignores it entirely.
        from app.tenancy import tenant_agnostic

        with tenant_agnostic():
            row = session.get(BreakGlassCode, code_id)
        assert row is not None
        assert row.used_at is None

    def test_manager_no_code_writes_stepup_missing_audit(
        self,
        engine: Engine,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        redirect_default_engine: None,
    ) -> None:
        """Step-up user without a code → ``recovery.stepup_missing`` on
        a fresh UoW, no mail, no nonce.
        """
        user = bootstrap_user(session, email="mn@example.com", display_name="MN")
        ws_id = _seed_workspace(session, slug="mn-ws")
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="manager"
        )
        session.commit()

        request_recovery(
            session,
            email="mn@example.com",
            ip="198.51.100.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # No mail, no nonce, no recovery.requested.
        assert mailer.sent == []
        assert session.scalars(select(MagicLinkNonce)).all() == []
        assert (
            session.scalars(
                select(AuditLog).where(AuditLog.action == "recovery.requested")
            ).all()
            == []
        )
        # Audit row landed on the fresh UoW.
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as reader:
            row = reader.scalars(
                select(AuditLog).where(AuditLog.action == "recovery.stepup_missing")
            ).one()
        assert row.entity_kind == "user"
        assert row.entity_id == user.id
        assert isinstance(row.diff, dict)
        assert row.diff["reason"] == "stepup_missing"
        assert len(row.diff["email_hash"]) == 64
        assert len(row.diff["ip_hash"]) == 64
        # No PII in the audit payload.
        assert "mn@example.com" not in str(row.diff)
        assert "198.51.100.1" not in str(row.diff)

    def test_manager_empty_code_is_treated_as_missing(
        self,
        engine: Engine,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        redirect_default_engine: None,
    ) -> None:
        """A whitespace-only / empty code is the same as no code at all.

        Treats it as ``stepup_missing`` rather than counting against
        the redemption rate-limit (which is gated on *failed verify
        attempts*, not "submitted nothing").
        """
        user = bootstrap_user(session, email="me@example.com", display_name="ME")
        ws_id = _seed_workspace(session, slug="me-ws")
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="manager"
        )
        session.commit()

        request_recovery(
            session,
            email="me@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            break_glass_code="   ",  # whitespace-only
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        assert mailer.sent == []
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as reader:
            assert reader.scalars(
                select(AuditLog).where(AuditLog.action == "recovery.stepup_missing")
            ).all()
            # NOT recorded as stepup_invalid (no fail-counter increment).
            assert (
                reader.scalars(
                    select(AuditLog).where(AuditLog.action == "recovery.stepup_invalid")
                ).all()
                == []
            )

    def test_manager_wrong_code_writes_stepup_invalid_audit(
        self,
        engine: Engine,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        redirect_default_engine: None,
    ) -> None:
        user = bootstrap_user(session, email="mi@example.com", display_name="MI")
        ws_id = _seed_workspace(session, slug="mi-ws")
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="manager"
        )
        # User has codes — but the submitted code doesn't match any.
        _seed_break_glass_code_for_user(
            session,
            user_id=user.id,
            workspace_id=ws_id,
            plaintext="real-code",
        )
        session.commit()

        request_recovery(
            session,
            email="mi@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            break_glass_code="WRONG",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # No mail, no nonce.
        assert mailer.sent == []
        assert session.scalars(select(MagicLinkNonce)).all() == []
        # Audit row on the fresh UoW.
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as reader:
            row = reader.scalars(
                select(AuditLog).where(AuditLog.action == "recovery.stepup_invalid")
            ).one()
        assert row.entity_id == user.id
        assert isinstance(row.diff, dict)
        assert row.diff["reason"] == "stepup_invalid"

    def test_manager_valid_code_burns_and_proceeds(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """Step-up user with a valid code → burn code, send mail, audit
        ``stepup=True`` on ``recovery.requested``, ``consumed_magic_link_id``
        stamped with the magic-link nonce's jti.
        """
        from app.adapters.db.identity.models import BreakGlassCode

        user = bootstrap_user(session, email="mv@example.com", display_name="MV")
        ws_id = _seed_workspace(session, slug="mv-ws")
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="manager"
        )
        code_id = _seed_break_glass_code_for_user(
            session,
            user_id=user.id,
            workspace_id=ws_id,
            plaintext="valid-code",
        )
        request_recovery(
            session,
            email="mv@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            break_glass_code="valid-code",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # Mail + nonce landed.
        assert len(mailer.sent) == 1
        nonce = session.scalars(select(MagicLinkNonce)).one()
        # Code burnt + ``consumed_magic_link_id`` stamped with the
        # nonce's jti.
        from app.tenancy import tenant_agnostic

        with tenant_agnostic():
            burnt = session.get(BreakGlassCode, code_id)
        assert burnt is not None
        assert burnt.used_at is not None
        assert burnt.consumed_magic_link_id == nonce.jti
        # Audit row carries ``stepup=True`` on the success branch.
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "recovery.requested")
        ).one()
        assert isinstance(audit.diff, dict)
        assert audit.diff["hit"] is True
        assert audit.diff["stepup"] is True

    def test_owners_group_member_requires_code(
        self,
        engine: Engine,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        redirect_default_engine: None,
    ) -> None:
        """An ``owners``-group member without a manager grant is still
        in the step-up population (§03 step 3).
        """
        user = bootstrap_user(session, email="og@example.com", display_name="OG")
        ws_id = _seed_workspace(session, slug="og-ws")
        # Owners-group membership only — no role_grant at all.
        _seed_owners_group_for_user(session, workspace_id=ws_id, user_id=user.id)
        session.commit()

        request_recovery(
            session,
            email="og@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            # No code submitted.
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # No mail (step-up gate refused).
        assert mailer.sent == []
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as reader:
            assert reader.scalars(
                select(AuditLog).where(AuditLog.action == "recovery.stepup_missing")
            ).all()

    def test_lockout_after_three_failures_blocks_subsequent_requests(
        self,
        engine: Engine,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        redirect_default_engine: None,
    ) -> None:
        """3 failed code attempts → 24h lockout. Subsequent requests
        write ``stepup_locked_out`` on the fresh UoW, no mail, no
        nonce.

        Spec §03 "Break-glass codes" → "Redemption rate limit" + §15
        "Step-up bypass is not a fallback".
        """
        # The recover-start throttle would trip on the 4th request
        # (3/email/hour), so we use 3 wrong attempts to land in
        # lockout, then a 4th request that the recover-start cap
        # would refuse anyway. To exercise the LOCKOUT branch
        # specifically, we use a fresh ``Throttle`` instance and pin
        # the requests at distinct ``now`` values so the recover-start
        # cap doesn't preempt.
        user = bootstrap_user(session, email="lo@example.com", display_name="LO")
        ws_id = _seed_workspace(session, slug="lo-ws")
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="manager"
        )
        _seed_break_glass_code_for_user(
            session,
            user_id=user.id,
            workspace_id=ws_id,
            plaintext="real",
        )
        session.commit()

        # 3 wrong-code attempts — each writes ``stepup_invalid`` and
        # increments the redemption-failure counter.
        for i in range(3):
            request_recovery(
                session,
                email="lo@example.com",
                ip="127.0.0.1",
                mailer=mailer,
                base_url="https://crew.day",
                break_glass_code=f"wrong-{i}",
                now=_PINNED + timedelta(seconds=i),
                throttle=throttle,
                settings=settings,
            )

        # 4th attempt (now within lockout) — even with a *correct*
        # code, the lockout check refuses BEFORE the redeem walk.
        # The recover-start throttle would have tripped here; we
        # construct a NEW throttle so we exercise the lockout, not
        # the recover-start cap.
        fresh_throttle = Throttle()
        request_recovery(
            session,
            email="lo@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            break_glass_code="real",
            now=_PINNED + timedelta(seconds=4),
            throttle=fresh_throttle,
            settings=settings,
        )

        # No mail at all — every attempt was refused.
        assert mailer.sent == []
        # Locked-out audit row landed on the fresh UoW.
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as reader:
            locked = reader.scalars(
                select(AuditLog).where(AuditLog.action == "recovery.stepup_locked_out")
            ).all()
            invalid = reader.scalars(
                select(AuditLog).where(AuditLog.action == "recovery.stepup_invalid")
            ).all()
        assert len(locked) >= 1
        assert len(invalid) == 3

    def test_unknown_email_with_code_takes_miss_branch(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """Unknown email + a submitted code → normal miss branch.

        The step-up gate runs AFTER user lookup; an unknown email
        never reaches the gate (no user → no step-up classifier
        call) and sends no email.
        """
        request_recovery(
            session,
            email="ghost@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            break_glass_code="anything",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # Normal miss: no mail, no nonce, audit hit=False with NO
        # stepup flag.
        assert mailer.sent == []
        assert session.scalars(select(MagicLinkNonce)).all() == []
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "recovery.requested")
        ).one()
        assert isinstance(audit.diff, dict)
        assert audit.diff["hit"] is False
        assert "stepup" not in audit.diff


# ---------------------------------------------------------------------------
# Step-up timing-leak hardening (§15 line 957: "neither a user's
# existence nor their step-up status can be probed").
# ---------------------------------------------------------------------------


class TestStepUpTimingBurn:
    """The step-up refusal branches must pay matching CPU.

    §15 forbids the response shape from leaking step-up status. Without
    a CPU burn on the missing / locked-out branches, a network adversary
    submitting ``email + dummy-code`` could distinguish "step-up user
    with no code-set" (instant return) from "step-up user with a wrong
    code" (~50ms argon2id verify + HMAC sign). The burn collapses
    those two onto roughly the same baseline.
    """

    def test_missing_code_burns_argon2_and_hmac(
        self,
        engine: Engine,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        redirect_default_engine: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Step-up + missing code → ``_burn_cpu_for_stepup_refusal`` fires.

        We monkeypatch the burn helper so the test runs in millisecond
        time and we can assert it was invoked exactly once on the
        refusal path.
        """
        from app.auth import recovery as recovery_module

        burn_calls: list[bytes] = []

        def _spy(pepper: bytes) -> None:
            burn_calls.append(pepper)

        monkeypatch.setattr(recovery_module, "_burn_cpu_for_stepup_refusal", _spy)

        user = bootstrap_user(session, email="bm@example.com", display_name="BM")
        ws_id = _seed_workspace(session, slug="bm-ws")
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="manager"
        )
        session.commit()

        request_recovery(
            session,
            email="bm@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        assert len(burn_calls) == 1

    def test_locked_out_burns_argon2_and_hmac(
        self,
        engine: Engine,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        redirect_default_engine: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Step-up + locked-out → ``_burn_cpu_for_stepup_refusal`` fires.

        Trip the lockout via three failed redeem attempts, then assert
        the next request (which short-circuits on ``check_redeem_allowed``)
        also pays the burn.
        """
        from app.auth import break_glass as bg_module
        from app.auth import recovery as recovery_module

        # Land the user in lockout state directly.
        for _ in range(3):
            bg_module.record_redeem_failure(user_id="will-be-set", now=_PINNED)

        burn_calls: list[bytes] = []

        def _spy(pepper: bytes) -> None:
            burn_calls.append(pepper)

        monkeypatch.setattr(recovery_module, "_burn_cpu_for_stepup_refusal", _spy)

        user = bootstrap_user(session, email="lk@example.com", display_name="LK")
        ws_id = _seed_workspace(session, slug="lk-ws")
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="manager"
        )
        session.commit()

        # Land THIS user in lockout — the canned counters above used
        # a placeholder id; the helper is keyed on user_id.
        for _ in range(3):
            bg_module.record_redeem_failure(user_id=user.id, now=_PINNED)

        request_recovery(
            session,
            email="lk@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            break_glass_code="anything",
            now=_PINNED + timedelta(seconds=1),
            throttle=throttle,
            settings=settings,
        )
        assert len(burn_calls) == 1
        assert mailer.sent == []

    def test_non_step_up_user_does_not_pay_burn(
        self,
        session: Session,
        mailer: _RecordingMailer,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Workers / clients / guests bypass the step-up gate entirely.

        The CPU burn is a step-up hardening — non-step-up users pay
        the normal hit branch (mail + nonce + magic-link HMAC), not
        the burn. Otherwise we'd be paying twice the cost on the
        common case.
        """
        from app.auth import recovery as recovery_module

        burn_calls: list[bytes] = []

        def _spy(pepper: bytes) -> None:
            burn_calls.append(pepper)

        monkeypatch.setattr(recovery_module, "_burn_cpu_for_stepup_refusal", _spy)

        user = bootstrap_user(session, email="ww@example.com", display_name="WW")
        ws_id = _seed_workspace(session, slug="ww-ws")
        _seed_role_grant(
            session, workspace_id=ws_id, user_id=user.id, grant_role="worker"
        )
        request_recovery(
            session,
            email="ww@example.com",
            ip="127.0.0.1",
            mailer=mailer,
            base_url="https://crew.day",
            now=_PINNED,
            throttle=throttle,
            settings=settings,
        )
        # Worker → normal hit branch, no step-up burn.
        assert burn_calls == []
        assert len(mailer.sent) == 1
