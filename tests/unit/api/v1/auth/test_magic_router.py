"""Router-level tests for :mod:`app.api.v1.auth.magic`.

Two focuses:

* ``MagicConsumeBody`` token-length bounds (cd-jt0v) — the
  ``min_length=1, max_length=4096`` defence-in-depth pins so a multi-MB
  nuisance body never reaches :func:`app.auth.magic_link.consume_link`.
  itsdangerous fails fast on garbage (~1 ms for 1 MB), so the bounds
  are belt-and-braces. They match the convention every other
  itsdangerous-signed token body in the app advertises
  (``EmailVerifyBody``, ``EmailRevertBody``, ``RegisterMobilePushBody``).

* ``build_magic_router`` HTTP wiring (cd-glaz) — drives both routes
  through :class:`fastapi.testclient.TestClient` against fake use
  cases monkeypatched onto the router's module namespace. These tests
  prove the contract the router's own docstring advertises:

  - ``POST /auth/magic/request`` returns 202 on enrolment regardless
    of whether the email matched a user (enumeration guard, §03 / §15).
  - ``POST /auth/magic/request`` returns 429 ``rate_limited`` when the
    throttle trips (the request docstring is explicit that the
    enumeration guard sits *under* the throttle gate).
  - ``POST /auth/magic/consume`` happy path returns 200 with the
    :class:`MagicLinkOutcome` fields.
  - ``POST /auth/magic/consume`` maps ``InvalidToken`` to 400,
    ``PurposeMismatch`` to 400, ``TokenExpired`` to 410, and
    ``AlreadyConsumed`` to 409.
  - ``POST /auth/magic/consume`` ``ConsumeLockout`` returns 429 AND
    does **not** advance the per-IP fail counter
    (:meth:`Throttle.record_consume_failure` is not called) — the
    pre-flight lockout is the cheap path that never touches the
    nonce row, so it would be wrong to charge it back into the
    counter that flipped the lockout in the first place.
  - ``POST /auth/magic/consume`` non-lockout failures DO advance the
    fail counter exactly once.
  - ``POST /auth/magic/consume`` happy path resets the fail counter
    via :meth:`Throttle.record_consume_success`.

The end-to-end consume flow against a real DB lives in
:mod:`tests.unit.auth.test_magic_link` (domain) and
:mod:`tests.integration.auth.test_magic_link_mailpit` (full stack).
This file owns the *router* layer in isolation — no real domain
service runs, no SMTP transport — so the assertions are about the
HTTP contract, not the underlying nonce flip.

See ``docs/specs/03-auth-and-tokens.md`` §"Magic link format" and
``docs/specs/15-security-privacy.md`` §"Rate limiting and abuse
controls".
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db import audit, identity  # noqa: F401  # register tables
from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.api.deps import db_session as db_session_dep
from app.api.v1.auth import magic as magic_module
from app.api.v1.auth.magic import MagicConsumeBody, build_magic_router
from app.auth.magic_link import (
    AlreadyConsumed,
    ConsumeLockout,
    InvalidToken,
    MagicLinkOutcome,
    MagicLinkPurpose,
    PendingMagicLink,
    PurposeMismatch,
    RateLimited,
    Throttle,
    TokenExpired,
)
from app.config import Settings
from tests._fakes.mailer import InMemoryMailer

_BASE_URL: str = "https://crew.day"


# ---------------------------------------------------------------------------
# Schema-level tests (cd-jt0v)
# ---------------------------------------------------------------------------


class TestMagicConsumeBodyTokenLength:
    """``token`` rejects empty and oversize bodies before the service runs."""

    def test_typical_token_accepted(self) -> None:
        """A real magic-link token round-trips at ~150 chars; the bound is roomy."""
        body = MagicConsumeBody(token="x" * 200, purpose="signup_verify")
        assert body.token == "x" * 200

    def test_max_length_4096_accepts_boundary(self) -> None:
        """Exactly 4096 characters is still valid (inclusive upper bound)."""
        body = MagicConsumeBody(token="x" * 4096, purpose="signup_verify")
        assert len(body.token) == 4096

    def test_token_above_4096_raises_validation_error(self) -> None:
        """4097 chars trips Pydantic before the magic-link service is called.

        cd-jt0v: defence-in-depth so a multi-MB nuisance body never
        reaches :func:`app.auth.magic_link.consume_link`. Pydantic's
        ``string_too_long`` is what FastAPI renders as ``422`` upstream.
        """
        with pytest.raises(ValidationError) as excinfo:
            MagicConsumeBody(token="x" * 4097, purpose="signup_verify")
        # Pin the offending field + the error category so a future
        # rename of the constraint surfaces here, not at runtime.
        errors = excinfo.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("token",)
        assert errors[0]["type"] == "string_too_long"

    def test_empty_token_raises_validation_error(self) -> None:
        """An empty string trips Pydantic — itsdangerous would always reject it
        at the domain layer; catching it here costs nothing.
        """
        with pytest.raises(ValidationError) as excinfo:
            MagicConsumeBody(token="", purpose="signup_verify")
        errors = excinfo.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("token",)
        assert errors[0]["type"] == "string_too_short"


# ---------------------------------------------------------------------------
# Router-level tests (cd-glaz)
# ---------------------------------------------------------------------------


class _RecordingThrottle(Throttle):
    """:class:`Throttle` subclass that records bookkeeping calls.

    The real ``Throttle`` state still ticks (so ``check_consume_allowed``
    behaves exactly as in production), but every call to
    :meth:`record_consume_failure` and :meth:`record_consume_success` is
    appended to a list so the cd-glaz lockout-vs-fail-counter assertion
    can pin the call shape without poking private dict internals.
    """

    __slots__ = ("failures", "successes")

    def __init__(self) -> None:
        super().__init__()
        self.failures: list[str] = []
        self.successes: list[str] = []

    def record_consume_failure(self, *, ip: str, now: datetime) -> None:
        self.failures.append(ip)
        super().record_consume_failure(ip=ip, now=now)

    def record_consume_success(self, *, ip: str) -> None:
        self.successes.append(ip)
        super().record_consume_success(ip=ip)


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-magic-router-root-key-0123456789"),
        public_url=_BASE_URL,
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine, schema created from ``Base.metadata``.

    Only needed because the ``/request`` route opens its own
    ``with make_uow() as session`` block and asserts the yielded
    object is a real :class:`sqlalchemy.orm.Session` — even though
    the fake :func:`request_link` we wire below never reads from it.
    """
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture(autouse=False)
def _redirect_default_uow_to_test_engine(
    engine: Engine,
    session_factory: sessionmaker[Session],
) -> Iterator[None]:
    """Bind ``make_uow`` to the per-test engine.

    The ``/request`` handler opens ``with make_uow() as session`` so the
    SMTP send fires post-commit; without this redirect the UoW would
    bind to whatever the default sessionmaker last pointed at. Mirrors
    the redirect in :mod:`tests.unit.api.v1.auth.test_email_change`.
    """
    import app.adapters.db.session as _session_mod
    from app.adapters.db.session import FilteredSession

    # ``_default_sessionmaker_`` is privately typed
    # ``sessionmaker[FilteredSession] | None``; our plain
    # :class:`Session` factory is structurally compatible at runtime
    # because the router never relies on the tenant filter in these
    # router-only paths (the use cases are mocked). The :func:`cast`
    # is a runtime no-op that documents the intentional widening
    # without an opaque ``# type: ignore``.
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = cast(
        "sessionmaker[FilteredSession]", session_factory
    )
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


@pytest.fixture
def mailer() -> InMemoryMailer:
    return InMemoryMailer()


@pytest.fixture
def throttle() -> _RecordingThrottle:
    return _RecordingThrottle()


@dataclass
class _RequestStub:
    """Configurable fake for :func:`app.auth.magic_link.request_link`.

    Each test sets ``raises`` or ``returns`` to choose the next-call
    behaviour. Calls are appended to ``calls`` so assertions can pin
    the kwargs the router forwarded. ``raises`` wins over ``returns``.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)
    raises: Exception | None = None
    returns: PendingMagicLink | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> PendingMagicLink | None:
        self.calls.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises
        return self.returns


@dataclass
class _ConsumeStub:
    """Configurable fake for :func:`app.auth.magic_link.consume_link`."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    raises: Exception | None = None
    outcome: MagicLinkOutcome = field(
        default_factory=lambda: MagicLinkOutcome(
            purpose="signup_verify",
            subject_id="01HSUBJECT0000000000000000",
            email_hash="hash-of-email",
            ip_hash="hash-of-ip",
        )
    )

    def __call__(self, *args: Any, **kwargs: Any) -> MagicLinkOutcome:
        self.calls.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises
        return self.outcome


@dataclass
class _AuditRecorder:
    """Recording fake for :func:`app.auth.magic_link.write_rejected_audit`.

    The router's own ``_write_rejected_on_fresh_uow`` swallows every
    ``Exception`` raised from this seam, so dropping the real writes
    does not change observable behaviour — the tests just need to
    pin that the seam was called with the right ``reason`` symbol.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(dict(kwargs))


@pytest.fixture
def request_stub() -> _RequestStub:
    return _RequestStub()


@pytest.fixture
def consume_stub() -> _ConsumeStub:
    return _ConsumeStub()


@pytest.fixture
def audit_recorder() -> _AuditRecorder:
    return _AuditRecorder()


@pytest.fixture
def client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    mailer: InMemoryMailer,
    throttle: _RecordingThrottle,
    request_stub: _RequestStub,
    consume_stub: _ConsumeStub,
    audit_recorder: _AuditRecorder,
    monkeypatch: pytest.MonkeyPatch,
    _redirect_default_uow_to_test_engine: None,
) -> Iterator[TestClient]:
    """Mount ``build_magic_router`` on a minimal FastAPI app.

    The two use cases (:func:`request_link`, :func:`consume_link`) are
    monkeypatched to recording stubs whose behaviour each test
    configures. The rejected-audit seam is replaced with a recorder
    so we don't depend on the audit-log table for the failure-path
    tests — the router's own helper swallows seam exceptions already
    (see module docstring), so dropping the real writes does not
    change observable behaviour.
    """
    monkeypatch.setattr(magic_module, "request_link", request_stub)
    monkeypatch.setattr(magic_module, "consume_link", consume_stub)
    monkeypatch.setattr(magic_module, "write_rejected_audit", audit_recorder)

    app = FastAPI()
    app.include_router(
        build_magic_router(
            mailer=mailer,
            throttle=throttle,
            settings=settings,
        ),
        prefix="/api/v1",
    )

    def _session() -> Iterator[Session]:
        # The consume route's ``_Db`` dep is satisfied by a real
        # session bound to the in-memory engine. The fake
        # :func:`consume_link` never reads from it, so no schema
        # work happens here.
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[db_session_dep] = _session

    with TestClient(app, base_url="https://testserver") as c:
        yield c


# ---------------------------------------------------------------------------
# /auth/magic/request — enumeration guard + throttle
# ---------------------------------------------------------------------------


_PURPOSE: MagicLinkPurpose = "signup_verify"


class TestMagicRequestRoute:
    """``POST /auth/magic/request`` HTTP contract."""

    def test_enrolment_returns_202_with_status_accepted(
        self,
        client: TestClient,
        request_stub: _RequestStub,
    ) -> None:
        """Default fake returns ``None`` (enumeration miss); body is opaque.

        Whether the email matched a user or not, the body must be the
        single literal ``{"status": "accepted"}`` and the status must
        be 202 (§03 "Self-serve signup", §15 "Rate limiting and
        abuse controls"). The router never differentiates.
        """
        response = client.post(
            "/api/v1/auth/magic/request",
            json={"email": "alice@example.com", "purpose": _PURPOSE},
        )
        assert response.status_code == 202
        assert response.json() == {"status": "accepted"}
        # The use case was called exactly once with the parsed body.
        assert len(request_stub.calls) == 1
        kwargs = request_stub.calls[0]
        assert kwargs["email"] == "alice@example.com"
        assert kwargs["purpose"] == _PURPOSE

    def test_known_and_unknown_address_share_the_same_202(
        self,
        client: TestClient,
        request_stub: _RequestStub,
    ) -> None:
        """Two requests, same response. The fake's default return (None,
        which mimics the "no user matched" branch) is identical to the
        "user did match" branch — both must produce 202.

        The router cannot itself know whether the email matched; the
        domain service does. By keeping both shapes mapped to the same
        202 we prove the router never adds a new branch that would
        leak the existence signal.
        """
        for email in ("known@example.com", "unknown@example.com"):
            response = client.post(
                "/api/v1/auth/magic/request",
                json={"email": email, "purpose": _PURPOSE},
            )
            assert response.status_code == 202
            assert response.json() == {"status": "accepted"}
        assert len(request_stub.calls) == 2

    def test_rate_limited_maps_to_429(
        self,
        client: TestClient,
        request_stub: _RequestStub,
    ) -> None:
        """The throttle gate fires *before* the enumeration guard, so a
        429 is the documented failure mode.

        The router docstring is explicit: "Returns ``429 rate_limited``
        when the per-IP or per-email budget is exhausted; the throttle
        fires **before** the DB or mailer are touched." The 202
        enumeration guard hides "user exists?" but the throttle status
        is independent of user existence — a caller staying under the
        budget is the precondition for the guard to apply at all.
        """
        request_stub.raises = RateLimited("per-IP request budget exceeded")
        response = client.post(
            "/api/v1/auth/magic/request",
            json={"email": "spammer@example.com", "purpose": _PURPOSE},
        )
        assert response.status_code == 429
        assert response.json() == {"detail": {"error": "rate_limited"}}

    def test_invalid_purpose_returns_422_at_validation(
        self,
        client: TestClient,
        request_stub: _RequestStub,
    ) -> None:
        """Pydantic enforces the :data:`MagicLinkPurpose` literal set;
        an unknown purpose never reaches the use case."""
        response = client.post(
            "/api/v1/auth/magic/request",
            json={"email": "x@example.com", "purpose": "not_a_real_purpose"},
        )
        assert response.status_code == 422
        assert request_stub.calls == []

    def test_empty_email_returns_422(
        self,
        client: TestClient,
        request_stub: _RequestStub,
    ) -> None:
        """``min_length=3`` on email is the first defence; the use case
        is never called for nonsense input."""
        response = client.post(
            "/api/v1/auth/magic/request",
            json={"email": "", "purpose": _PURPOSE},
        )
        assert response.status_code == 422
        assert request_stub.calls == []

    def test_pending_link_delivers_after_commit(
        self,
        client: TestClient,
        request_stub: _RequestStub,
    ) -> None:
        """When the use case returns a :class:`PendingMagicLink`, the
        router fires :meth:`PendingMagicLink.deliver` after the UoW
        commits. The cd-9i7z outbox boundary is what we pin here:
        no commit, no SMTP. The fake commit always succeeds, so we
        just verify the SMTP closure ran.
        """
        sent_flag: list[bool] = []
        request_stub.returns = PendingMagicLink(
            url=f"{_BASE_URL}/auth/magic/fake-token",
            _send_callback=lambda: sent_flag.append(True),
        )
        response = client.post(
            "/api/v1/auth/magic/request",
            json={"email": "alice@example.com", "purpose": _PURPOSE},
        )
        assert response.status_code == 202
        assert sent_flag == [True]


# ---------------------------------------------------------------------------
# /auth/magic/consume — happy path, error mapping, lockout-vs-counter
# ---------------------------------------------------------------------------


class TestMagicConsumeRoute:
    """``POST /auth/magic/consume`` HTTP contract."""

    def test_happy_path_returns_outcome_and_resets_counter(
        self,
        client: TestClient,
        throttle: _RecordingThrottle,
        consume_stub: _ConsumeStub,
    ) -> None:
        """200 with the :class:`MagicLinkOutcome` fields; success resets
        the per-IP fail counter (§03: "a legitimate redemption should
        wipe the slate for the next one").
        """
        response = client.post(
            "/api/v1/auth/magic/consume",
            json={"token": "x" * 50, "purpose": _PURPOSE},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["purpose"] == consume_stub.outcome.purpose
        assert body["subject_id"] == consume_stub.outcome.subject_id
        assert body["email_hash"] == consume_stub.outcome.email_hash
        assert body["ip_hash"] == consume_stub.outcome.ip_hash
        # Use case got the parsed body verbatim.
        assert len(consume_stub.calls) == 1
        kwargs = consume_stub.calls[0]
        assert kwargs["token"] == "x" * 50
        assert kwargs["expected_purpose"] == _PURPOSE
        # Success path: counter reset, no failure recorded.
        assert throttle.failures == []
        assert throttle.successes == [kwargs["ip"]]

    def test_invalid_token_maps_to_400_and_advances_counter(
        self,
        client: TestClient,
        throttle: _RecordingThrottle,
        consume_stub: _ConsumeStub,
        audit_recorder: _AuditRecorder,
    ) -> None:
        """``InvalidToken`` → 400 ``invalid_token`` and the per-IP fail
        counter advances by exactly one (§03 lockout cadence).

        Also pins the **full forensic shape** of the rejected-audit
        seam call: ``token``, ``expected_purpose``, ``ip`` and
        ``reason`` all flow through verbatim. The audit row is the
        security log §15 promises will land "even on misses" — an
        off-by-one rename of any of these kwargs in the router would
        slip past a ``reason``-only assertion.
        """
        consume_stub.raises = InvalidToken("bad signature")
        response = client.post(
            "/api/v1/auth/magic/consume",
            json={"token": "x" * 50, "purpose": _PURPOSE},
        )
        assert response.status_code == 400
        assert response.json() == {"detail": {"error": "invalid_token"}}
        assert len(throttle.failures) == 1
        # Rejected audit lands on a fresh UoW (§15 "Audit always
        # written… including misses").
        assert len(audit_recorder.calls) == 1
        audit_kwargs = audit_recorder.calls[0]
        assert audit_kwargs["reason"] == "invalid_token"
        assert audit_kwargs["token"] == "x" * 50
        assert audit_kwargs["expected_purpose"] == _PURPOSE
        # Loopback TestClient resolves a real client host; just pin
        # that the router forwarded a non-empty string rather than
        # the raw IP (which the underlying transport may surface as
        # ``"testclient"`` or ``"127.0.0.1"`` depending on the
        # Starlette release).
        assert isinstance(audit_kwargs["ip"], str)

    def test_purpose_mismatch_maps_to_400_and_advances_counter(
        self,
        client: TestClient,
        throttle: _RecordingThrottle,
        consume_stub: _ConsumeStub,
        audit_recorder: _AuditRecorder,
    ) -> None:
        consume_stub.raises = PurposeMismatch("token says recover")
        response = client.post(
            "/api/v1/auth/magic/consume",
            json={"token": "x" * 50, "purpose": _PURPOSE},
        )
        assert response.status_code == 400
        assert response.json() == {"detail": {"error": "purpose_mismatch"}}
        assert len(throttle.failures) == 1
        # §15: cross-purpose replays must surface in the audit trail —
        # the symbol is what makes this a security log instead of an
        # opaque "an audit row was written" check.
        assert len(audit_recorder.calls) == 1
        assert audit_recorder.calls[0]["reason"] == "purpose_mismatch"

    def test_token_expired_maps_to_410_and_advances_counter(
        self,
        client: TestClient,
        throttle: _RecordingThrottle,
        consume_stub: _ConsumeStub,
        audit_recorder: _AuditRecorder,
    ) -> None:
        consume_stub.raises = TokenExpired("ttl elapsed")
        response = client.post(
            "/api/v1/auth/magic/consume",
            json={"token": "x" * 50, "purpose": _PURPOSE},
        )
        assert response.status_code == 410
        assert response.json() == {"detail": {"error": "expired"}}
        assert len(throttle.failures) == 1
        assert len(audit_recorder.calls) == 1
        assert audit_recorder.calls[0]["reason"] == "expired"

    def test_already_consumed_maps_to_409_and_advances_counter(
        self,
        client: TestClient,
        throttle: _RecordingThrottle,
        consume_stub: _ConsumeStub,
        audit_recorder: _AuditRecorder,
    ) -> None:
        consume_stub.raises = AlreadyConsumed("jti burnt")
        response = client.post(
            "/api/v1/auth/magic/consume",
            json={"token": "x" * 50, "purpose": _PURPOSE},
        )
        assert response.status_code == 409
        assert response.json() == {"detail": {"error": "already_consumed"}}
        assert len(throttle.failures) == 1
        assert len(audit_recorder.calls) == 1
        assert audit_recorder.calls[0]["reason"] == "already_consumed"

    def test_consume_lockout_maps_to_429_without_advancing_counter(
        self,
        client: TestClient,
        throttle: _RecordingThrottle,
        consume_stub: _ConsumeStub,
        audit_recorder: _AuditRecorder,
    ) -> None:
        """The cd-glaz acceptance criterion.

        ``ConsumeLockout`` is the pre-flight 429 — the nonce row is
        never touched and the failure that flipped the lockout is the
        one that should already have been counted, not the rejected
        retry. Charging this back into the counter would extend the
        lockout window every time a locked-out IP retries, which is
        the spec's "earn the next lockout from scratch once this one
        expires" behaviour.

        Asserts:
        1. Status is 429 with ``consume_locked_out`` symbol.
        2. :meth:`Throttle.record_consume_failure` is NOT called.
        3. The rejected audit DOES still land (sustained abuse from a
           locked-out IP must be visible in the trail per §15).
        """
        consume_stub.raises = ConsumeLockout("ip is locked out")
        response = client.post(
            "/api/v1/auth/magic/consume",
            json={"token": "x" * 50, "purpose": _PURPOSE},
        )
        assert response.status_code == 429
        assert response.json() == {"detail": {"error": "consume_locked_out"}}
        # The crucial assertion: lockout does NOT advance the
        # fail-counter. A locked-out caller's retries must not extend
        # their own lockout window.
        assert throttle.failures == []
        # …and a successful consume never happened either.
        assert throttle.successes == []
        # Rejected audit still fires — sustained abuse from a
        # locked-out IP must remain visible in the trail.
        assert len(audit_recorder.calls) == 1
        assert audit_recorder.calls[0]["reason"] == "consume_locked_out"

    def test_rate_limited_on_consume_maps_to_429_and_advances_counter(
        self,
        client: TestClient,
        throttle: _RecordingThrottle,
        consume_stub: _ConsumeStub,
        audit_recorder: _AuditRecorder,
    ) -> None:
        """``RateLimited`` from the consume use case maps to 429 +
        ``rate_limited``, advances the fail counter, and lands a
        rejected-audit row.

        **Defensive forward-compat.** Today
        :func:`app.auth.magic_link.consume_link` does not raise
        :class:`RateLimited` — its only throttle interaction is the
        :class:`ConsumeLockout` pre-flight check. ``RateLimited`` is in
        the router's :data:`_DomainError` tuple so a future variant
        of the use case (or a shared throttle that fires inside
        ``consume_link``) can never silently bypass the 429 envelope.
        This test pins the dispatch shape: anything in the tuple that
        isn't :class:`ConsumeLockout` flows through the generic arm,
        which charges the fail counter. Only :class:`ConsumeLockout`
        gets the "do not charge" treatment.
        """
        consume_stub.raises = RateLimited("budget exceeded mid-flight")
        response = client.post(
            "/api/v1/auth/magic/consume",
            json={"token": "x" * 50, "purpose": _PURPOSE},
        )
        assert response.status_code == 429
        assert response.json() == {"detail": {"error": "rate_limited"}}
        assert len(throttle.failures) == 1
        assert len(audit_recorder.calls) == 1
        assert audit_recorder.calls[0]["reason"] == "rate_limited"


# ---------------------------------------------------------------------------
# Builder-level misconfiguration
# ---------------------------------------------------------------------------


class TestBuildMagicRouterMisconfigured:
    """Pin the router's misconfiguration failure mode.

    Per :func:`app.api.v1.auth.magic.build_magic_router` (line ~237):
    "``base_url`` defaults to ``settings.public_url`` — when both are
    ``None`` the service raises :class:`RuntimeError` on the first
    request, which is the right failure mode for a misconfigured
    deployment (better than silently emitting localhost links)." This
    test pins that contract: a request against a router built without
    a usable base URL surfaces a 500, not a stale-URL email.
    """

    def test_request_raises_runtime_error_when_no_base_url(
        self,
        mailer: InMemoryMailer,
        throttle: _RecordingThrottle,
    ) -> None:
        # Build settings with public_url=None to defeat both fallbacks.
        # ``model_construct`` skips Pydantic validation so we can mint
        # an invalid Settings without re-deriving the rest of the
        # production schema.
        broken_settings = Settings.model_construct(
            database_url="sqlite:///:memory:",
            root_key=SecretStr("unit-test-magic-router-root-key-0123456789"),
            public_url=None,
        )
        app = FastAPI()
        app.include_router(
            build_magic_router(
                mailer=mailer,
                throttle=throttle,
                settings=broken_settings,
            ),
            prefix="/api/v1",
        )
        # ``raise_server_exceptions=False`` makes TestClient surface
        # the 500 instead of re-raising; we want to assert on the
        # HTTP envelope the operator would actually see.
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/api/v1/auth/magic/request",
            json={"email": "alice@example.com", "purpose": _PURPOSE},
        )
        assert response.status_code == 500
