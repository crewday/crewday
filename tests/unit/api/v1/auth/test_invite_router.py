"""Router-level tests for :mod:`app.api.v1.auth.invite`.

Narrow scope on purpose: cd-rpxd acceptance criterion #5 — invite
accept rejects a token whose ``purpose`` is not ``accept``. The
domain service
(:func:`app.domain.identity.membership.consume_invite_token`) raises
the seam-level :class:`MagicLinkPurposeMismatch` (re-exported as
:class:`membership.PurposeMismatch`) on that branch; the HTTP
router's :func:`_http_for_token` mapping collapses that to a
``purpose_mismatch`` error envelope. This test pins the mapping so a
future refactor of the error taxonomy cannot silently drop the guard.

The broader invite / accept flow (new-user branch, existing-user
branch, work_engagement seeding, session-cookie re-entrance) is
covered end-to-end by
:mod:`tests.integration.identity.test_invite_accept` and
:mod:`tests.integration.identity.test_membership`. This file owns
only the purpose-guard surface so the cd-rpxd acceptance criterion
maps to a dedicated, fast-running unit.

See ``docs/specs/03-auth-and-tokens.md`` §"Additional users
(invite → click-to-accept)" and ``docs/specs/12-rest-api.md`` §"Auth".
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from app.api.errors import CONTENT_TYPE_PROBLEM_JSON, add_exception_handlers
from app.api.v1.auth.invite import (
    _http_for_invite,
    _http_for_invite_passkey,
    _http_for_invites_invite,
    _http_for_invites_token,
    _http_for_token,
)
from app.auth._throttle import ConsumeLockout, RateLimited
from app.domain.errors import CANONICAL_TYPE_BASE, DomainError
from app.domain.identity.membership import (
    AlreadyConsumed,
    InvalidToken,
    InviteAlreadyAccepted,
    InviteExpired,
    InviteNotFound,
    InvitePasskeyAlreadyRegistered,
    InviteStateInvalid,
    PasskeySessionRequired,
    PurposeMismatch,
    TokenExpired,
)

_TYPE_BY_STATUS = {
    status.HTTP_400_BAD_REQUEST: "validation",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_410_GONE: "gone",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "validation",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
}
_TITLE_BY_STATUS = {
    status.HTTP_400_BAD_REQUEST: "Bad request",
    status.HTTP_401_UNAUTHORIZED: "Unauthorized",
    status.HTTP_404_NOT_FOUND: "Not found",
    status.HTTP_409_CONFLICT: "Conflict",
    status.HTTP_410_GONE: "Gone",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "Validation error",
    status.HTTP_429_TOO_MANY_REQUESTS: "Rate limited",
}


def _response_for(exc: DomainError) -> TestClient:
    app = FastAPI()

    @app.get("/boom")
    def boom() -> None:
        raise exc

    add_exception_handlers(app)
    return TestClient(app, raise_server_exceptions=False)


def _assert_problem(exc: DomainError, status_code: int, symbol: str) -> None:
    response = _response_for(exc).get("/boom")
    assert response.status_code == status_code
    assert response.headers["content-type"].startswith(CONTENT_TYPE_PROBLEM_JSON)
    assert "retry-after" not in response.headers
    body = response.json()
    assert body["type"] == f"{CANONICAL_TYPE_BASE}{_TYPE_BY_STATUS[status_code]}"
    assert body["title"] == _TITLE_BY_STATUS[status_code]
    assert body["status"] == status_code
    assert body["instance"] == "/boom"
    assert body["error"] == symbol
    assert "detail" not in body
    assert "errors" not in body


class TestInvitePurposeMismatchMapping:
    """``_http_for_token`` collapses :class:`PurposeMismatch` to 400."""

    def test_purpose_mismatch_maps_to_400_symbol(self) -> None:
        """Spec §12 "Auth" error vocabulary pins this symbol.

        cd-rpxd acceptance criterion #5: a token whose ``purpose``
        is not ``accept`` refuses the invite. The symbol is
        ``purpose_mismatch`` so the SPA's form-level messaging keys
        off a stable code, and the status is ``400 Bad Request``
        (the token is syntactically valid but semantically wrong on
        this endpoint — matches the spec §03 "Magic link format"
        vocabulary where ``purpose_mismatch`` is a client-side
        correctable error).
        """
        error = _http_for_token(PurposeMismatch("expected accept, got n_confirm"))
        _assert_problem(error, status.HTTP_400_BAD_REQUEST, "purpose_mismatch")

    def test_token_expired_maps_to_410(self) -> None:
        error = _http_for_token(TokenExpired("24h TTL elapsed"))
        _assert_problem(error, status.HTTP_410_GONE, "expired")

    def test_already_consumed_maps_to_409(self) -> None:
        error = _http_for_token(AlreadyConsumed("jti burnt"))
        _assert_problem(error, status.HTTP_409_CONFLICT, "already_consumed")

    def test_consume_lockout_maps_to_429(self) -> None:
        error = _http_for_token(ConsumeLockout("too many attempts"))
        _assert_problem(error, status.HTTP_429_TOO_MANY_REQUESTS, "consume_locked_out")

    def test_rate_limited_maps_to_429(self) -> None:
        error = _http_for_token(RateLimited("per-ip bucket full"))
        _assert_problem(error, status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited")

    def test_invalid_token_fallback_maps_to_400(self) -> None:
        """Catch-all bucket: any other :class:`InvalidToken` → 400."""
        error = _http_for_token(InvalidToken("bad signature"))
        _assert_problem(error, status.HTTP_400_BAD_REQUEST, "invalid_token")


class TestInvitePasskeyErrorMapping:
    """`_http_for_invite` + `_http_for_invite_passkey` cd-9q6bb branches.

    The bare-host invite passkey routes share `_http_for_invite` for
    invite-state errors and `_http_for_invite_passkey` for passkey
    domain errors. These tests pin the new mappings so a refactor of
    the error taxonomy cannot silently drop a guard the SPA + e2e
    suite (cd-db0g) rely on.

    The router now raises :class:`DomainError` subclasses directly; the
    shared problem-json seam preserves the legacy ``error`` extension.
    """

    @pytest.mark.parametrize(
        ("exc", "status_code", "symbol"),
        [
            (InviteNotFound("missing"), status.HTTP_404_NOT_FOUND, "invite_not_found"),
            (InviteExpired("ttl elapsed"), status.HTTP_410_GONE, "expired"),
            (
                InviteAlreadyAccepted("done"),
                status.HTTP_409_CONFLICT,
                "already_accepted",
            ),
            (
                InvitePasskeyAlreadyRegistered("user already enrolled"),
                status.HTTP_409_CONFLICT,
                "passkey_already_registered",
            ),
            (
                InviteStateInvalid("bad state"),
                status.HTTP_409_CONFLICT,
                "invalid_state",
            ),
            (
                PasskeySessionRequired("sign in first"),
                status.HTTP_401_UNAUTHORIZED,
                "passkey_session_required",
            ),
        ],
    )
    def test_invite_state_errors_preserve_wire_contract(
        self,
        exc: Exception,
        status_code: int,
        symbol: str,
    ) -> None:
        _assert_problem(_http_for_invite(exc), status_code, symbol)

    def test_invalid_registration_maps_to_400(self) -> None:
        from app.auth.passkey import InvalidRegistration

        error = _http_for_invite_passkey(InvalidRegistration("bad attestation"))
        _assert_problem(error, status.HTTP_400_BAD_REQUEST, "invalid_registration")

    def test_challenge_expired_maps_to_400(self) -> None:
        from app.auth.passkey import ChallengeExpired

        error = _http_for_invite_passkey(ChallengeExpired("ttl elapsed"))
        _assert_problem(error, status.HTTP_400_BAD_REQUEST, "challenge_expired")

    def test_challenge_consumed_or_unknown_maps_to_409(self) -> None:
        """Replay safety: a burnt or never-issued challenge is 409.

        ``ChallengeNotFound`` is the canonical surface for both shapes
        — the row is deleted atomically with the credential insert so
        a replayed finish is indistinguishable from a never-existed id.
        The error envelope collapses onto one shape so the SPA's
        retry-or-restart heuristic doesn't differentiate them; re-running
        ``start`` is the right next step in either case.
        """
        from app.auth.passkey import ChallengeNotFound

        error = _http_for_invite_passkey(ChallengeNotFound("gone"))
        _assert_problem(
            error,
            status.HTTP_409_CONFLICT,
            "challenge_consumed_or_unknown",
        )

    def test_too_many_passkeys_maps_to_422(self) -> None:
        """Concurrent enrolment race: a 6th-passkey insert hits the cap."""
        from app.auth.passkey import TooManyPasskeys

        error = _http_for_invite_passkey(TooManyPasskeys("cap"))
        _assert_problem(
            error, status.HTTP_422_UNPROCESSABLE_CONTENT, "too_many_passkeys"
        )


class TestPluralInviteErrorMapping:
    """Plural ``/invites`` helpers preserve the no-existence-leak surface."""

    @pytest.mark.parametrize(
        "exc",
        [
            InvalidToken("bad signature"),
            PurposeMismatch("wrong purpose"),
            TokenExpired("ttl elapsed"),
            AlreadyConsumed("jti burnt"),
        ],
    )
    def test_token_validity_errors_flatten_to_404(self, exc: Exception) -> None:
        _assert_problem(
            _http_for_invites_token(exc),
            status.HTTP_404_NOT_FOUND,
            "invite_not_found",
        )

    @pytest.mark.parametrize(
        ("exc", "symbol"),
        [
            (ConsumeLockout("too many attempts"), "consume_locked_out"),
            (RateLimited("per-ip bucket full"), "rate_limited"),
        ],
    )
    def test_token_throttle_errors_stay_429(self, exc: Exception, symbol: str) -> None:
        _assert_problem(
            _http_for_invites_token(exc),
            status.HTTP_429_TOO_MANY_REQUESTS,
            symbol,
        )

    @pytest.mark.parametrize(
        "exc",
        [
            InviteNotFound("missing"),
            InviteExpired("ttl elapsed"),
            InviteAlreadyAccepted("done"),
            InviteStateInvalid("bad state"),
            InvitePasskeyAlreadyRegistered("user already enrolled"),
        ],
    )
    def test_invite_row_errors_flatten_to_404(self, exc: Exception) -> None:
        _assert_problem(
            _http_for_invites_invite(exc),
            status.HTTP_404_NOT_FOUND,
            "invite_not_found",
        )

    def test_passkey_session_required_stays_401(self) -> None:
        _assert_problem(
            _http_for_invites_invite(PasskeySessionRequired("sign in first")),
            status.HTTP_401_UNAUTHORIZED,
            "passkey_session_required",
        )
