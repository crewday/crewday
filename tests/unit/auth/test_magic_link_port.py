"""Unit tests for the cd-24im :class:`MagicLinkAdapter` exception bridge.

The adapter wraps :mod:`app.auth.magic_link` and translates the auth-
layer exception types into the seam-level equivalents declared on
:mod:`app.domain.identity.email_change_ports`. The
:mod:`app.domain.identity.email_change` service catches the seam-level
names — without lossless translation here, an
``app.auth.magic_link.AlreadyConsumed`` would slip past the
``except MagicLinkAlreadyConsumed`` and surface as a 500.

Coverage:

* ``peek_link`` and ``consume_link`` translate every magic-link domain
  exception (``InvalidToken`` / ``PurposeMismatch`` / ``TokenExpired`` /
  ``AlreadyConsumed``) into its seam-level equivalent, preserving the
  message string (lossless) and chaining the original via ``__cause__``.
* Throttle-layer exceptions (:class:`RateLimited` / :class:`ConsumeLockout`)
  propagate **verbatim** — the seam contract is explicit that those
  shared throttle types are router-mapped uniformly across all auth
  flows and must not be re-typed.
* ``inspect_token_jti`` translates :class:`InvalidToken` to the seam-
  level :class:`MagicLinkInvalidToken`.
* ``request_link`` propagates :class:`RateLimited` verbatim.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service email change"
and ``app/auth/magic_link_port.py`` for the contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.auth import magic_link
from app.auth._throttle import ConsumeLockout, RateLimited, Throttle
from app.auth.magic_link_port import MagicLinkAdapter
from app.config import Settings
from app.domain.identity.email_change_ports import (
    MagicLinkAlreadyConsumed,
    MagicLinkInvalidToken,
    MagicLinkPurposeMismatch,
    MagicLinkTokenExpired,
)
from tests._fakes.mailer import InMemoryMailer

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_BASE_URL = "https://crew.day"


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-magic-link-port-root-key"),
        public_url=_BASE_URL,
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
def adapter(session: Session) -> MagicLinkAdapter:
    return MagicLinkAdapter(session)


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


# ---------------------------------------------------------------------------
# peek_link / consume_link translate every magic-link error
# ---------------------------------------------------------------------------


class TestPeekLinkExceptionTranslation:
    """Every :mod:`app.auth.magic_link` typed error becomes its seam peer."""

    @pytest.mark.parametrize(
        ("auth_exc", "seam_exc"),
        [
            (magic_link.InvalidToken("forged sig"), MagicLinkInvalidToken),
            (
                magic_link.PurposeMismatch("token purpose != expected"),
                MagicLinkPurposeMismatch,
            ),
            (magic_link.TokenExpired("ttl lapsed"), MagicLinkTokenExpired),
            (magic_link.AlreadyConsumed("nonce burnt"), MagicLinkAlreadyConsumed),
        ],
    )
    def test_translates_auth_layer_error(
        self,
        adapter: MagicLinkAdapter,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        auth_exc: Exception,
        seam_exc: type[Exception],
    ) -> None:
        def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise auth_exc

        monkeypatch.setattr(magic_link, "peek_link", _raise)
        with pytest.raises(seam_exc) as excinfo:
            adapter.peek_link(
                token="token-x",
                expected_purpose="email_change_confirm",
                ip="127.0.0.1",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )
        # Lossless message — the seam-level exception carries the
        # original auth-layer exception's args[0] verbatim.
        assert str(excinfo.value) == str(auth_exc)
        # Chained via ``raise … from`` so the original is preserved
        # for tracebacks.
        assert excinfo.value.__cause__ is auth_exc

    @pytest.mark.parametrize(
        "throttle_exc",
        [
            RateLimited("too many requests"),
            ConsumeLockout("ip locked out"),
        ],
    )
    def test_throttle_errors_propagate_verbatim(
        self,
        adapter: MagicLinkAdapter,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        throttle_exc: Exception,
    ) -> None:
        """Throttle-layer types are shared infrastructure; the router
        maps them uniformly across every auth flow, so the seam must
        NOT re-type them.
        """

        def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise throttle_exc

        monkeypatch.setattr(magic_link, "peek_link", _raise)
        with pytest.raises(type(throttle_exc)) as excinfo:
            adapter.peek_link(
                token="token-x",
                expected_purpose="email_change_confirm",
                ip="127.0.0.1",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )
        # Verbatim — the same instance the auth layer raised.
        assert excinfo.value is throttle_exc


class TestConsumeLinkExceptionTranslation:
    """Same translation contract on the consume path."""

    @pytest.mark.parametrize(
        ("auth_exc", "seam_exc"),
        [
            (magic_link.InvalidToken("forged sig"), MagicLinkInvalidToken),
            (
                magic_link.PurposeMismatch("token purpose != expected"),
                MagicLinkPurposeMismatch,
            ),
            (magic_link.TokenExpired("ttl lapsed"), MagicLinkTokenExpired),
            (magic_link.AlreadyConsumed("nonce burnt"), MagicLinkAlreadyConsumed),
        ],
    )
    def test_translates_auth_layer_error(
        self,
        adapter: MagicLinkAdapter,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        auth_exc: Exception,
        seam_exc: type[Exception],
    ) -> None:
        def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise auth_exc

        monkeypatch.setattr(magic_link, "consume_link", _raise)
        with pytest.raises(seam_exc) as excinfo:
            adapter.consume_link(
                token="token-x",
                expected_purpose="email_change_confirm",
                ip="127.0.0.1",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )
        assert str(excinfo.value) == str(auth_exc)
        assert excinfo.value.__cause__ is auth_exc

    def test_throttle_lockout_propagates_verbatim(
        self,
        adapter: MagicLinkAdapter,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lockout = ConsumeLockout("ip locked out")

        def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise lockout

        monkeypatch.setattr(magic_link, "consume_link", _raise)
        with pytest.raises(ConsumeLockout) as excinfo:
            adapter.consume_link(
                token="token-x",
                expected_purpose="email_change_confirm",
                ip="127.0.0.1",
                now=_PINNED,
                throttle=throttle,
                settings=settings,
            )
        assert excinfo.value is lockout


# ---------------------------------------------------------------------------
# inspect_token_jti — narrow translation surface
# ---------------------------------------------------------------------------


class TestInspectTokenJtiExceptionTranslation:
    def test_translates_invalid_token(
        self,
        adapter: MagicLinkAdapter,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original = magic_link.InvalidToken("bad sig")

        def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise original

        monkeypatch.setattr(magic_link, "inspect_token_jti", _raise)
        with pytest.raises(MagicLinkInvalidToken) as excinfo:
            adapter.inspect_token_jti("forged-token", settings=settings)
        assert str(excinfo.value) == str(original)
        assert excinfo.value.__cause__ is original


# ---------------------------------------------------------------------------
# request_link — throttle errors propagate (no translation)
# ---------------------------------------------------------------------------


class TestRequestLinkExceptionPassthrough:
    def test_rate_limited_propagates_verbatim(
        self,
        adapter: MagicLinkAdapter,
        throttle: Throttle,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rate_limited = RateLimited("too many requests")

        def _raise(*_args: Any, **_kwargs: Any) -> Any:
            raise rate_limited

        monkeypatch.setattr(magic_link, "request_link", _raise)
        mailer = InMemoryMailer()
        with pytest.raises(RateLimited) as excinfo:
            adapter.request_link(
                email="alice@example.com",
                purpose="email_change_confirm",
                ip="127.0.0.1",
                mailer=mailer,
                base_url=_BASE_URL,
                now=_PINNED,
                throttle=throttle,
                settings=settings,
                subject_id="01HWA00000000000000000USR1",
            )
        assert excinfo.value is rate_limited

    def test_round_trip_returns_handle(
        self,
        adapter: MagicLinkAdapter,
        throttle: Throttle,
        settings: Settings,
    ) -> None:
        """Sanity: an end-to-end ``request_link`` over a real session
        returns a handle whose URL embeds the magic-link prefix.

        Validates that the adapter's pass-through of mailer / throttle /
        clock keywords reaches the underlying function and the
        :class:`PendingMagicLink` returned is structurally a
        :class:`MagicLinkHandle` (has ``url`` + ``deliver``).
        """
        mailer = InMemoryMailer()
        handle = adapter.request_link(
            email="alice@example.com",
            purpose="email_change_confirm",
            ip="127.0.0.1",
            mailer=mailer,
            base_url=_BASE_URL,
            now=_PINNED,
            throttle=throttle,
            settings=settings,
            subject_id="01HWA00000000000000000USR1",
        )
        assert handle is not None
        assert "/auth/magic/" in handle.url
        # The deferred send is callable but must not have fired yet.
        assert mailer.sent == []
        handle.deliver()
        assert len(mailer.sent) == 1
