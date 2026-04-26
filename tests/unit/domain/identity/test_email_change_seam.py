"""Fake-driven seam tests for :mod:`app.domain.identity.email_change` (cd-24im).

Drives the domain service against in-memory fakes for the
:class:`~app.domain.identity.email_change_ports.EmailChangeRepository`
and :class:`~app.domain.identity.email_change_ports.MagicLinkPort`
seams so the domain contract is exercised without an SQLAlchemy
roundtrip. Validates:

* validation paths (:class:`InvalidEmail`),
* uniqueness paths (:class:`EmailInUse` on request + verify),
* cool-off paths (:class:`RecentReenrollment` window edges),
* not-found / mismatch paths
  (:class:`PendingNotFound`, :class:`SessionUserMismatch`),
* the dispatch contract — ``add_pending`` + ``add_callback`` are
  recorded and only fire on ``deliver()``,
* the magic-link exception translation
  (:class:`MagicLinkInvalidToken` from the Port surfaces verbatim).

The router-level + SA-backed tests in
``tests/unit/domain/identity/test_email_change.py`` and
``tests/unit/api/v1/auth/test_email_change.py`` cover the production
wiring; this file owns the fake-driven seam contract.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service email change"
and ``docs/specs/15-security-privacy.md`` §"Self-service lost-device
& email-change abuse mitigations".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr

from app.adapters.mail.ports import Mailer
from app.auth._throttle import Throttle
from app.config import Settings
from app.domain.identity import email_change
from app.domain.identity.email_change_ports import (
    EmailChangeMagicLinkPurpose,
    EmailChangePendingRow,
    MagicLinkAlreadyConsumed,
    MagicLinkHandle,
    MagicLinkInvalidToken,
    MagicLinkOutcome,
    UserIdentityRow,
)
from app.util.clock import Clock
from tests._fakes.mailer import InMemoryMailer

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_BASE_URL = "https://crew.day"
_USER_ID = "01HWA00000000000000000USR1"


def _settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-root-key-email-change-seam"),
        public_url=_BASE_URL,
    )


def _user(
    *,
    user_id: str = _USER_ID,
    email: str = "alice@example.com",
    display_name: str = "Alice",
) -> UserIdentityRow:
    return UserIdentityRow(
        id=user_id,
        email=email,
        email_lower=email.lower(),
        display_name=display_name,
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeHandle:
    """In-memory :class:`MagicLinkHandle` matching the seam Protocol.

    Tracks how many times :meth:`deliver` was invoked so the dispatch
    contract can be asserted (idempotent single-fire).
    """

    url: str
    delivered: int = 0

    def deliver(self) -> None:
        self.delivered += 1


@dataclass
class _FakeDispatch:
    """In-memory :class:`MagicLinkDispatch` recording the queue.

    Mirrors :class:`app.auth.magic_link.PendingDispatch` semantics —
    callbacks fire in registration order; a queued
    :class:`MagicLinkHandle` is wrapped via its own ``deliver``.
    """

    callbacks: list[Callable[[], None]] = field(default_factory=list)
    pendings: list[MagicLinkHandle] = field(default_factory=list)

    def add_callback(self, callback: Callable[[], None]) -> None:
        self.callbacks.append(callback)

    def add_pending(self, pending: MagicLinkHandle | None) -> None:
        if pending is None:
            return
        self.pendings.append(pending)

    def fire(self) -> None:
        for handle in self.pendings:
            handle.deliver()
        for cb in self.callbacks:
            cb()


@dataclass
class _FakeRepo:
    """In-memory :class:`EmailChangeRepository`.

    Holds one ``UserIdentityRow`` per ``user_id`` plus a list of
    pending rows, mirroring the SA concretion's surface. The fake
    treats writes as idempotent within a single test (no UoW boundary).
    """

    users: dict[str, UserIdentityRow] = field(default_factory=dict)
    pendings: dict[str, EmailChangePendingRow] = field(default_factory=dict)
    latest_passkey: dict[str, datetime] = field(default_factory=dict)
    audit_session: Any = field(default=None)

    @property
    def session(self) -> Any:
        # Tests that only assert seam wiring don't exercise the audit
        # writer. The few that do swap in a real session via the
        # ``audit_session`` field.
        return self.audit_session

    def canonicalise_email(self, email: str) -> str:
        return email.strip().lower()

    def get_user(self, *, user_id: str) -> UserIdentityRow | None:
        return self.users.get(user_id)

    def update_user_email(self, *, user_id: str, new_email: str) -> UserIdentityRow:
        prev = self.users[user_id]
        updated = UserIdentityRow(
            id=prev.id,
            email=new_email,
            email_lower=self.canonicalise_email(new_email),
            display_name=prev.display_name,
        )
        self.users[user_id] = updated
        return updated

    def email_taken_by_other(
        self, *, new_email_lower: str, current_user_id: str
    ) -> bool:
        for uid, row in self.users.items():
            if row.email_lower == new_email_lower and uid != current_user_id:
                return True
        return False

    def latest_passkey_created_at(self, *, user_id: str) -> datetime | None:
        return self.latest_passkey.get(user_id)

    def insert_pending(
        self,
        *,
        pending_id: str,
        user_id: str,
        request_jti: str,
        previous_email: str,
        previous_email_lower: str,
        new_email: str,
        new_email_lower: str,
        created_at: datetime,
    ) -> EmailChangePendingRow:
        row = EmailChangePendingRow(
            id=pending_id,
            user_id=user_id,
            request_jti=request_jti,
            revert_jti=None,
            previous_email=previous_email,
            previous_email_lower=previous_email_lower,
            new_email=new_email,
            new_email_lower=new_email_lower,
            created_at=created_at,
            verified_at=None,
            revert_expires_at=None,
            reverted_at=None,
        )
        self.pendings[pending_id] = row
        return row

    def find_pending_by_request_jti(
        self, *, request_jti: str
    ) -> EmailChangePendingRow | None:
        for row in self.pendings.values():
            if row.request_jti == request_jti:
                return row
        return None

    def find_pending_by_revert_jti(
        self, *, revert_jti: str
    ) -> EmailChangePendingRow | None:
        for row in self.pendings.values():
            if row.revert_jti == revert_jti:
                return row
        return None

    def mark_verified(
        self,
        *,
        pending_id: str,
        revert_jti: str,
        revert_expires_at: datetime,
        verified_at: datetime,
    ) -> EmailChangePendingRow:
        prev = self.pendings[pending_id]
        new_row = EmailChangePendingRow(
            id=prev.id,
            user_id=prev.user_id,
            request_jti=prev.request_jti,
            revert_jti=revert_jti,
            previous_email=prev.previous_email,
            previous_email_lower=prev.previous_email_lower,
            new_email=prev.new_email,
            new_email_lower=prev.new_email_lower,
            created_at=prev.created_at,
            verified_at=verified_at,
            revert_expires_at=revert_expires_at,
            reverted_at=None,
        )
        self.pendings[pending_id] = new_row
        return new_row

    def mark_reverted(
        self, *, pending_id: str, reverted_at: datetime
    ) -> EmailChangePendingRow:
        prev = self.pendings[pending_id]
        new_row = EmailChangePendingRow(
            id=prev.id,
            user_id=prev.user_id,
            request_jti=prev.request_jti,
            revert_jti=prev.revert_jti,
            previous_email=prev.previous_email,
            previous_email_lower=prev.previous_email_lower,
            new_email=prev.new_email,
            new_email_lower=prev.new_email_lower,
            created_at=prev.created_at,
            verified_at=prev.verified_at,
            revert_expires_at=prev.revert_expires_at,
            reverted_at=reverted_at,
        )
        self.pendings[pending_id] = new_row
        return new_row


@dataclass
class _FakeMagicLinkPort:
    """In-memory :class:`MagicLinkPort` for fake-driven seam tests.

    The fake mints synthetic tokens / jtis (no signing — the test
    never calls into :mod:`app.auth.magic_link`) and lets each test
    pre-program the peek / consume responses by stuffing
    :attr:`peek_outcomes` / :attr:`consume_outcomes`. ``inspect_token_jti``
    inverts the synthetic token shape (``token-<jti>``) so the test
    can predict the jti.
    """

    request_calls: list[dict[str, Any]] = field(default_factory=list)
    peek_calls: list[dict[str, Any]] = field(default_factory=list)
    consume_calls: list[dict[str, Any]] = field(default_factory=list)
    next_handle_url: str = f"{_BASE_URL}/auth/magic/token-jti-1"
    peek_outcomes: list[MagicLinkOutcome] = field(default_factory=list)
    consume_outcomes: list[MagicLinkOutcome] = field(default_factory=list)
    request_handles: list[_FakeHandle] = field(default_factory=list)
    request_returns_none: bool = False
    inspect_raises: BaseException | None = None

    def request_link(
        self,
        *,
        email: str,
        purpose: EmailChangeMagicLinkPurpose,
        ip: str,
        mailer: Mailer | None,
        base_url: str,
        now: datetime,
        ttl: timedelta | None = None,
        throttle: Throttle,
        settings: Settings | None = None,
        clock: Clock | None = None,
        subject_id: str | None = None,
        send_email: bool = True,
    ) -> MagicLinkHandle | None:
        self.request_calls.append(
            {
                "email": email,
                "purpose": purpose,
                "ip": ip,
                "subject_id": subject_id,
                "send_email": send_email,
            }
        )
        if self.request_returns_none:
            return None
        # Mint a fresh handle whose URL ends with a synthetic jti so
        # the inspect_token_jti shim can recover it deterministically.
        jti = f"jti-{len(self.request_calls)}"
        handle = _FakeHandle(url=f"{_BASE_URL}/auth/magic/token-{jti}")
        self.request_handles.append(handle)
        return handle

    def peek_link(
        self,
        *,
        token: str,
        expected_purpose: EmailChangeMagicLinkPurpose,
        ip: str,
        now: datetime,
        throttle: Throttle,
        settings: Settings | None = None,
        clock: Clock | None = None,
    ) -> MagicLinkOutcome:
        self.peek_calls.append({"token": token, "expected_purpose": expected_purpose})
        if not self.peek_outcomes:
            raise AssertionError("peek_link called without a primed outcome")
        return self.peek_outcomes.pop(0)

    def consume_link(
        self,
        *,
        token: str,
        expected_purpose: EmailChangeMagicLinkPurpose,
        ip: str,
        now: datetime,
        throttle: Throttle,
        settings: Settings | None = None,
        clock: Clock | None = None,
    ) -> MagicLinkOutcome:
        self.consume_calls.append(
            {"token": token, "expected_purpose": expected_purpose}
        )
        if not self.consume_outcomes:
            raise AssertionError("consume_link called without a primed outcome")
        return self.consume_outcomes.pop(0)

    def inspect_token_jti(
        self,
        token: str,
        *,
        settings: Settings | None = None,
    ) -> str:
        if self.inspect_raises is not None:
            exc = self.inspect_raises
            self.inspect_raises = None
            raise exc
        # Synthetic token shape ``token-<jti>``: invert the prefix.
        if not token.startswith("token-"):
            raise MagicLinkInvalidToken(f"unexpected fake token shape: {token!r}")
        return token[len("token-") :]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo() -> _FakeRepo:
    fake = _FakeRepo()
    fake.users[_USER_ID] = _user()
    return fake


@pytest.fixture
def link_port() -> _FakeMagicLinkPort:
    return _FakeMagicLinkPort()


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def mailer() -> InMemoryMailer:
    return InMemoryMailer()


@pytest.fixture
def settings() -> Settings:
    return _settings()


# ---------------------------------------------------------------------------
# request_change validation paths
# ---------------------------------------------------------------------------


class TestRequestChangeValidation:
    """Validation gates fire before any seam work happens."""

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            " ",
            "ab",  # under length floor
            "no-at-symbol",
            "@example.com",
            "alice@",
            "has space@example.com",
            "tab\t@example.com",
        ],
    )
    def test_raises_invalid_email_for_malformed(
        self,
        repo: _FakeRepo,
        link_port: _FakeMagicLinkPort,
        throttle: Throttle,
        mailer: InMemoryMailer,
        settings: Settings,
        raw: str,
    ) -> None:
        with pytest.raises(email_change.InvalidEmail):
            email_change.request_change(
                repo=repo,
                link_port=link_port,
                user=_user(),
                new_email=raw,
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                throttle=throttle,
                now=_PINNED,
                settings=settings,
            )
        # Validation must precede any seam call.
        assert link_port.request_calls == []
        assert repo.pendings == {}

    def test_raises_invalid_email_for_overlong(
        self,
        repo: _FakeRepo,
        link_port: _FakeMagicLinkPort,
        throttle: Throttle,
        mailer: InMemoryMailer,
        settings: Settings,
    ) -> None:
        local_part = "a" * 320
        new_email = f"{local_part}@example.com"
        with pytest.raises(email_change.InvalidEmail):
            email_change.request_change(
                repo=repo,
                link_port=link_port,
                user=_user(),
                new_email=new_email,
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                throttle=throttle,
                now=_PINNED,
                settings=settings,
            )

    def test_raises_email_in_use(
        self,
        repo: _FakeRepo,
        link_port: _FakeMagicLinkPort,
        throttle: Throttle,
        mailer: InMemoryMailer,
        settings: Settings,
    ) -> None:
        # Seed a sibling user holding the target address.
        repo.users["other"] = UserIdentityRow(
            id="other",
            email="bob@example.com",
            email_lower="bob@example.com",
            display_name="Bob",
        )
        with pytest.raises(email_change.EmailInUse):
            email_change.request_change(
                repo=repo,
                link_port=link_port,
                user=_user(),
                new_email="bob@example.com",
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                throttle=throttle,
                now=_PINNED,
                settings=settings,
            )
        assert link_port.request_calls == []
        assert repo.pendings == {}

    def test_email_in_use_is_case_insensitive(
        self,
        repo: _FakeRepo,
        link_port: _FakeMagicLinkPort,
        throttle: Throttle,
        mailer: InMemoryMailer,
        settings: Settings,
    ) -> None:
        repo.users["other"] = UserIdentityRow(
            id="other",
            email="Bob@Example.com",
            email_lower="bob@example.com",
            display_name="Bob",
        )
        with pytest.raises(email_change.EmailInUse):
            email_change.request_change(
                repo=repo,
                link_port=link_port,
                user=_user(),
                new_email="BOB@example.COM",
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                throttle=throttle,
                now=_PINNED,
                settings=settings,
            )

    def test_self_address_is_not_email_in_use(
        self,
        repo: _FakeRepo,
        link_port: _FakeMagicLinkPort,
        throttle: Throttle,
        mailer: InMemoryMailer,
        settings: Settings,
    ) -> None:
        # Setting the same address as the caller's current one is a
        # no-op for the uniqueness gate (only OTHER users holding the
        # address count as a clash). The domain proceeds to mint a
        # magic link. We catch ``AttributeError`` raised by the
        # downstream audit writer because this fake repo's
        # ``audit_session`` is ``None`` — the assertion is that the
        # uniqueness gate does NOT trip first, which we verify via
        # ``link_port.request_calls``.
        with pytest.raises(AttributeError):
            email_change.request_change(
                repo=repo,
                link_port=link_port,
                user=_user(),
                new_email="alice@example.com",
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                throttle=throttle,
                now=_PINNED,
                settings=settings,
            )
        # request_link fired despite the self-address — the uniqueness
        # gate did not pre-empt it.
        assert len(link_port.request_calls) == 1


class TestRequestChangeCoolOff:
    """The §15 recent-reenrollment cool-off bounds the post-recovery hijack window."""

    def test_within_cool_off_raises(
        self,
        repo: _FakeRepo,
        link_port: _FakeMagicLinkPort,
        throttle: Throttle,
        mailer: InMemoryMailer,
        settings: Settings,
    ) -> None:
        # Passkey created 5 minutes ago — inside the 15-minute window.
        repo.latest_passkey[_USER_ID] = _PINNED - timedelta(minutes=5)
        with pytest.raises(email_change.RecentReenrollment):
            email_change.request_change(
                repo=repo,
                link_port=link_port,
                user=_user(),
                new_email="alice.new@example.com",
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                throttle=throttle,
                now=_PINNED,
                settings=settings,
            )
        assert link_port.request_calls == []

    def test_at_cool_off_boundary_passes(
        self,
        repo: _FakeRepo,
        link_port: _FakeMagicLinkPort,
        throttle: Throttle,
        mailer: InMemoryMailer,
        settings: Settings,
    ) -> None:
        # Passkey created exactly 15 minutes ago — boundary is "older
        # than the cool-off", which means equal-to-cutoff passes.
        repo.latest_passkey[_USER_ID] = _PINNED - timedelta(minutes=15)
        # Will raise on audit_session=None below; the cool-off branch
        # must NOT trip.
        with pytest.raises((RuntimeError, AttributeError)):
            email_change.request_change(
                repo=repo,
                link_port=link_port,
                user=_user(),
                new_email="alice.new@example.com",
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                throttle=throttle,
                now=_PINNED,
                settings=settings,
            )
        assert len(link_port.request_calls) == 1

    def test_no_passkey_skips_cool_off(
        self,
        repo: _FakeRepo,
        link_port: _FakeMagicLinkPort,
        throttle: Throttle,
        mailer: InMemoryMailer,
        settings: Settings,
    ) -> None:
        assert _USER_ID not in repo.latest_passkey
        with pytest.raises((RuntimeError, AttributeError)):
            email_change.request_change(
                repo=repo,
                link_port=link_port,
                user=_user(),
                new_email="alice.new@example.com",
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                throttle=throttle,
                now=_PINNED,
                settings=settings,
            )
        # request_link fired despite the empty passkey set.
        assert len(link_port.request_calls) == 1


# ---------------------------------------------------------------------------
# Dispatch contract
# ---------------------------------------------------------------------------


class TestRequestChangeDispatch:
    """The cd-9slq dispatch is fed by the seam, fired by the caller."""

    def test_dispatch_collects_pending_and_callback(
        self,
        link_port: _FakeMagicLinkPort,
        throttle: Throttle,
        mailer: InMemoryMailer,
        settings: Settings,
    ) -> None:
        # Use a real session for the audit write — :func:`write_audit`
        # still needs a Session today. We reuse the SA test
        # infrastructure but only tap the ``audit_session`` field
        # without exercising the SA-backed repo.
        from sqlalchemy.orm import sessionmaker

        from app.adapters.db.base import Base
        from app.adapters.db.session import make_engine

        engine = make_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            with factory() as audit_session:
                repo = _FakeRepo()
                repo.users[_USER_ID] = _user()
                repo.audit_session = audit_session
                dispatch = _FakeDispatch()
                outcome = email_change.request_change(
                    repo=repo,
                    link_port=link_port,
                    user=_user(),
                    new_email="alice.new@example.com",
                    ip="127.0.0.1",
                    mailer=mailer,
                    base_url=_BASE_URL,
                    throttle=throttle,
                    now=_PINNED,
                    settings=settings,
                    dispatch=dispatch,
                )
                # The seam recorded both kinds of entries.
                assert len(dispatch.pendings) == 1
                assert len(dispatch.callbacks) == 1
                # Mailer untouched until ``dispatch.fire()``.
                assert mailer.sent == []
                # The pending row is in the fake repo.
                assert outcome.pending_id in repo.pendings
                # Fire the dispatch and observe the side effects.
                dispatch.fire()
                # The seam stores via ``MagicLinkHandle``; we narrow
                # to ``_FakeHandle`` to inspect the test counter.
                handle = dispatch.pendings[0]
                assert isinstance(handle, _FakeHandle)
                assert handle.delivered == 1
                # The notice send writes one message to the
                # in-memory mailer (the magic-link send is intercepted
                # by the fake handle's ``deliver``).
                assert len(mailer.sent) == 1
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# verify_change paths
# ---------------------------------------------------------------------------


class TestVerifyChangeMismatch:
    """Verify rejects sessions that don't own the pending row."""

    def _seed_pending(
        self, repo: _FakeRepo, *, user_id: str = _USER_ID
    ) -> EmailChangePendingRow:
        pending = EmailChangePendingRow(
            id="01HWA00000000000000000PND1",
            user_id=user_id,
            request_jti="jti-fake",
            revert_jti=None,
            previous_email="alice@example.com",
            previous_email_lower="alice@example.com",
            new_email="alice.new@example.com",
            new_email_lower="alice.new@example.com",
            created_at=_PINNED,
            verified_at=None,
            revert_expires_at=None,
            reverted_at=None,
        )
        repo.pendings[pending.id] = pending
        return pending

    def test_session_user_mismatch_raises(
        self,
        repo: _FakeRepo,
        link_port: _FakeMagicLinkPort,
        throttle: Throttle,
        mailer: InMemoryMailer,
        settings: Settings,
    ) -> None:
        self._seed_pending(repo)
        link_port.peek_outcomes.append(
            MagicLinkOutcome(
                purpose="email_change_confirm",
                subject_id=_USER_ID,
                email_hash="h",
                ip_hash="i",
            )
        )
        with pytest.raises(email_change.SessionUserMismatch):
            email_change.verify_change(
                repo=repo,
                link_port=link_port,
                token="token-jti-fake",
                session_user_id="someone-else",
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                throttle=throttle,
                now=_PINNED,
                settings=settings,
            )
        # Consume must NOT have fired — the session check gates it.
        assert link_port.consume_calls == []

    def test_pending_not_found_raises(
        self,
        repo: _FakeRepo,
        link_port: _FakeMagicLinkPort,
        throttle: Throttle,
        mailer: InMemoryMailer,
        settings: Settings,
    ) -> None:
        # No pending row seeded.
        link_port.peek_outcomes.append(
            MagicLinkOutcome(
                purpose="email_change_confirm",
                subject_id=_USER_ID,
                email_hash="h",
                ip_hash="i",
            )
        )
        with pytest.raises(email_change.PendingNotFound):
            email_change.verify_change(
                repo=repo,
                link_port=link_port,
                token="token-jti-fake",
                session_user_id=_USER_ID,
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                throttle=throttle,
                now=_PINNED,
                settings=settings,
            )

    def test_pending_already_verified_collapses_to_not_found(
        self,
        repo: _FakeRepo,
        link_port: _FakeMagicLinkPort,
        throttle: Throttle,
        mailer: InMemoryMailer,
        settings: Settings,
    ) -> None:
        # Stamped verified_at — a sibling verify already won.
        pending = self._seed_pending(repo)
        repo.pendings[pending.id] = EmailChangePendingRow(
            id=pending.id,
            user_id=pending.user_id,
            request_jti=pending.request_jti,
            revert_jti="jti-revert",
            previous_email=pending.previous_email,
            previous_email_lower=pending.previous_email_lower,
            new_email=pending.new_email,
            new_email_lower=pending.new_email_lower,
            created_at=pending.created_at,
            verified_at=_PINNED - timedelta(seconds=30),
            revert_expires_at=_PINNED + timedelta(hours=72),
            reverted_at=None,
        )
        link_port.peek_outcomes.append(
            MagicLinkOutcome(
                purpose="email_change_confirm",
                subject_id=_USER_ID,
                email_hash="h",
                ip_hash="i",
            )
        )
        with pytest.raises(email_change.PendingNotFound):
            email_change.verify_change(
                repo=repo,
                link_port=link_port,
                token="token-jti-fake",
                session_user_id=_USER_ID,
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                throttle=throttle,
                now=_PINNED,
                settings=settings,
            )

    def test_email_in_use_after_verify_race(
        self,
        repo: _FakeRepo,
        link_port: _FakeMagicLinkPort,
        throttle: Throttle,
        mailer: InMemoryMailer,
        settings: Settings,
    ) -> None:
        # A sibling user grabbed the new address mid-window.
        self._seed_pending(repo)
        repo.users["sibling"] = UserIdentityRow(
            id="sibling",
            email="alice.new@example.com",
            email_lower="alice.new@example.com",
            display_name="Sib",
        )
        link_port.peek_outcomes.append(
            MagicLinkOutcome(
                purpose="email_change_confirm",
                subject_id=_USER_ID,
                email_hash="h",
                ip_hash="i",
            )
        )
        link_port.consume_outcomes.append(
            MagicLinkOutcome(
                purpose="email_change_confirm",
                subject_id=_USER_ID,
                email_hash="h",
                ip_hash="i",
            )
        )
        with pytest.raises(email_change.EmailInUse):
            email_change.verify_change(
                repo=repo,
                link_port=link_port,
                token="token-jti-fake",
                session_user_id=_USER_ID,
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                throttle=throttle,
                now=_PINNED,
                settings=settings,
            )


# ---------------------------------------------------------------------------
# Magic-link exception bridging
# ---------------------------------------------------------------------------


class TestSeamExceptionTranslation:
    """The Port surfaces seam-level exceptions; the domain re-raises them.

    Validates that the ``MagicLink*`` types declared on
    :mod:`app.domain.identity.email_change_ports` (and re-exported on
    :mod:`app.domain.identity.email_change` under the legacy
    ``InvalidToken`` / ``PurposeMismatch`` / … names) are what the
    domain raises — not the auth-layer originals. The router catches
    these names verbatim, so the bridging must round-trip cleanly.
    """

    def test_inspect_token_jti_invalid_token_propagates(
        self,
        repo: _FakeRepo,
        link_port: _FakeMagicLinkPort,
        throttle: Throttle,
        mailer: InMemoryMailer,
        settings: Settings,
    ) -> None:
        link_port.inspect_raises = MagicLinkInvalidToken("forged signature")
        link_port.peek_outcomes.append(
            MagicLinkOutcome(
                purpose="email_change_confirm",
                subject_id=_USER_ID,
                email_hash="h",
                ip_hash="i",
            )
        )
        with pytest.raises(email_change.InvalidToken):
            email_change.verify_change(
                repo=repo,
                link_port=link_port,
                token="token-jti-fake",
                session_user_id=_USER_ID,
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                throttle=throttle,
                now=_PINNED,
                settings=settings,
            )

    def test_revert_already_consumed_propagates(
        self,
        repo: _FakeRepo,
        link_port: _FakeMagicLinkPort,
        throttle: Throttle,
        mailer: InMemoryMailer,
        settings: Settings,
    ) -> None:
        # Consume raises AlreadyConsumed via the Port's translation.
        def _raising_consume(
            *,
            token: str,
            expected_purpose: EmailChangeMagicLinkPurpose,
            ip: str,
            now: datetime,
            throttle: Throttle,
            settings: Settings | None = None,
            clock: Clock | None = None,
        ) -> MagicLinkOutcome:
            raise MagicLinkAlreadyConsumed("nonce already burnt")

        # Patch the port's consume to raise — the seam contract says
        # ``MagicLinkAlreadyConsumed`` is a subclass of the legacy
        # name re-exported from email_change.
        link_port.consume_link = _raising_consume  # type: ignore[method-assign]
        with pytest.raises(email_change.AlreadyConsumed):
            email_change.revert_change(
                repo=repo,
                link_port=link_port,
                token="token-jti-fake",
                ip="127.0.0.1",
                throttle=throttle,
                now=_PINNED,
                settings=settings,
            )

    def test_legacy_aliases_match_seam_classes(self) -> None:
        """The legacy names exposed on email_change ARE the seam ones.

        Locks the bridging in: a future drift where the auth-layer
        types accidentally bleed back into ``email_change.__all__``
        would break this test, surfacing the regression.
        """
        from app.domain.identity import email_change_ports

        assert email_change.InvalidToken is email_change_ports.MagicLinkInvalidToken
        assert (
            email_change.PurposeMismatch is email_change_ports.MagicLinkPurposeMismatch
        )
        assert email_change.TokenExpired is email_change_ports.MagicLinkTokenExpired
        assert (
            email_change.AlreadyConsumed is email_change_ports.MagicLinkAlreadyConsumed
        )
