"""Router-level tests for :mod:`app.api.v1.auth.invite`.

Narrow scope on purpose: cd-rpxd acceptance criterion #5 — invite
accept rejects a token whose ``purpose`` is not ``accept``. The
domain service
(:func:`app.domain.identity.membership.consume_invite_token`) raises
:class:`app.auth.magic_link.PurposeMismatch` on that branch; the HTTP
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

from fastapi import status

from app.api.v1.auth.invite import _http_for_token
from app.auth.magic_link import (
    AlreadyConsumed,
    ConsumeLockout,
    InvalidToken,
    PurposeMismatch,
    RateLimited,
    TokenExpired,
)


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
        exc = PurposeMismatch("expected accept, got n_confirm")
        http = _http_for_token(exc)
        assert http.status_code == status.HTTP_400_BAD_REQUEST
        # ``detail`` on an :class:`HTTPException` is a plain dict when
        # the router constructs it that way — the RFC 7807 seam
        # (:mod:`app.api.errors`) unwraps it into the problem+json
        # envelope downstream.
        assert http.detail == {"error": "purpose_mismatch"}

    def test_token_expired_maps_to_410(self) -> None:
        http = _http_for_token(TokenExpired("24h TTL elapsed"))
        assert http.status_code == status.HTTP_410_GONE
        assert http.detail == {"error": "expired"}

    def test_already_consumed_maps_to_409(self) -> None:
        http = _http_for_token(AlreadyConsumed("jti burnt"))
        assert http.status_code == status.HTTP_409_CONFLICT
        assert http.detail == {"error": "already_consumed"}

    def test_consume_lockout_maps_to_429(self) -> None:
        http = _http_for_token(ConsumeLockout("too many attempts"))
        assert http.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert http.detail == {"error": "consume_locked_out"}

    def test_rate_limited_maps_to_429(self) -> None:
        http = _http_for_token(RateLimited("per-ip bucket full"))
        assert http.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert http.detail == {"error": "rate_limited"}

    def test_invalid_token_fallback_maps_to_400(self) -> None:
        """Catch-all bucket: any other :class:`InvalidToken` → 400."""
        http = _http_for_token(InvalidToken("bad signature"))
        assert http.status_code == status.HTTP_400_BAD_REQUEST
        assert http.detail == {"error": "invalid_token"}


class TestInvitePasskeyErrorMapping:
    """`_http_for_invite` + `_http_for_invite_passkey` cd-9q6bb branches.

    The bare-host invite passkey routes share `_http_for_invite` for
    invite-state errors and `_http_for_invite_passkey` for passkey
    domain errors. These tests pin the new mappings so a refactor of
    the error taxonomy cannot silently drop a guard the SPA + e2e
    suite (cd-db0g) rely on.

    ``_detail_dict`` re-types the Starlette-typed ``HTTPException.detail``
    (``str | None``) to the dict shape FastAPI's RFC 7807 seam actually
    emits. The widening keeps ``mypy --strict`` honest without sprinkling
    ``# type: ignore[comparison-overlap]`` on every assertion.
    """

    @staticmethod
    def _detail_dict(http: object) -> dict[str, object]:
        from fastapi import HTTPException

        assert isinstance(http, HTTPException)
        detail = http.detail
        assert isinstance(detail, dict)
        return detail

    def test_passkey_already_registered_maps_to_409(self) -> None:
        from app.api.v1.auth.invite import _http_for_invite
        from app.domain.identity.membership import (
            InvitePasskeyAlreadyRegistered,
        )

        http = _http_for_invite(InvitePasskeyAlreadyRegistered("user already enrolled"))
        assert http.status_code == status.HTTP_409_CONFLICT
        assert self._detail_dict(http) == {"error": "passkey_already_registered"}

    def test_invalid_registration_maps_to_400(self) -> None:
        from app.api.v1.auth.invite import _http_for_invite_passkey
        from app.auth.passkey import InvalidRegistration

        http = _http_for_invite_passkey(InvalidRegistration("bad attestation"))
        assert http.status_code == status.HTTP_400_BAD_REQUEST
        assert self._detail_dict(http) == {"error": "invalid_registration"}

    def test_challenge_expired_maps_to_400(self) -> None:
        from app.api.v1.auth.invite import _http_for_invite_passkey
        from app.auth.passkey import ChallengeExpired

        http = _http_for_invite_passkey(ChallengeExpired("ttl elapsed"))
        assert http.status_code == status.HTTP_400_BAD_REQUEST
        assert self._detail_dict(http) == {"error": "challenge_expired"}

    def test_challenge_consumed_or_unknown_maps_to_409(self) -> None:
        """Replay safety: a burnt or never-issued challenge is 409.

        The two cases collapse onto one error envelope so the SPA's
        retry-or-restart heuristic doesn't have to differentiate
        them — ``register_finish_signup`` sees both classes as
        "this challenge cannot be verified" and re-running ``start``
        is the right next step in either case.
        """
        from app.api.v1.auth.invite import _http_for_invite_passkey
        from app.auth.passkey import ChallengeAlreadyConsumed, ChallengeNotFound

        for exc in (ChallengeNotFound("gone"), ChallengeAlreadyConsumed("gone")):
            http = _http_for_invite_passkey(exc)
            assert http.status_code == status.HTTP_409_CONFLICT
            assert self._detail_dict(http) == {"error": "challenge_consumed_or_unknown"}

    def test_too_many_passkeys_maps_to_422(self) -> None:
        """Concurrent enrolment race: a 6th-passkey insert hits the cap."""
        from app.api.v1.auth.invite import _http_for_invite_passkey
        from app.auth.passkey import TooManyPasskeys

        http = _http_for_invite_passkey(TooManyPasskeys("cap"))
        assert http.status_code == 422
        assert self._detail_dict(http) == {"error": "too_many_passkeys"}
