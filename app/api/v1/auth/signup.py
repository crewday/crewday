"""Self-serve signup HTTP router (``/signup/*``).

Bare-host routes, tenant-agnostic. Every route is gated on
``capabilities.settings.signup_enabled`` — a disabled deployment
returns ``404`` so the surface is invisible rather than "present
but forbidden" (§03 "Self-serve signup").

Routes (all ``/api/v1/signup/*`` — the app factory mounts the router
at ``/api/v1``; the router itself carries the ``/signup`` prefix):

* ``POST /signup/start`` ``{email, desired_slug}`` — 202 on success,
  422 on bad slug, 409 on slug collisions (``slug_taken`` /
  ``slug_reserved`` / ``slug_homoglyph_collision`` /
  ``slug_in_grace_period``).
* ``POST /signup/verify`` ``{token}`` — JSON body with the
  signup-session id. §14 is SPA-first, so we return JSON rather than
  a 302 redirect; the SPA takes the ``signup_session_id`` and
  forwards to the passkey step.
* ``POST /signup/passkey/start`` ``{signup_session_id}`` — delegates
  to :func:`app.auth.passkey.register_start_signup`.
* ``POST /signup/passkey/finish`` — delegates to
  :func:`app.auth.signup.complete_signup`. On success returns
  ``{workspace_slug, redirect}`` so the SPA has everything it needs
  to navigate.

Handlers are intentionally thin: unpack the body, call the domain
service, map typed errors onto HTTP symbols. The spec's error
vocabulary lives here so swapping to RFC 7807 later (cd-waq3) is a
single diff.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-serve signup" and
``docs/specs/12-rest-api.md`` §"Auth / signup".
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import SignupAttempt
from app.adapters.mail.ports import Mailer
from app.api.deps import db_session
from app.auth import passkey, signup
from app.auth._throttle import Throttle
from app.auth.magic_link import (
    AlreadyConsumed,
    ConsumeLockout,
    InvalidToken,
    PurposeMismatch,
    RateLimited,
    TokenExpired,
)
from app.capabilities import Capabilities
from app.config import Settings, get_settings
from app.tenancy import InvalidSlug, tenant_agnostic

__all__ = [
    "PasskeyFinishBody",
    "PasskeyFinishResponse",
    "PasskeyStartBody",
    "PasskeyStartResponse",
    "SignupStartBody",
    "SignupStartResponse",
    "VerifyBody",
    "VerifyResponse",
    "build_signup_router",
]


_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class SignupStartBody(BaseModel):
    """Request body for ``POST /signup/start``."""

    email: str = Field(..., min_length=3, max_length=320)
    desired_slug: str = Field(..., min_length=3, max_length=40)


class SignupStartResponse(BaseModel):
    """202 body — the SPA only needs confirmation the request landed.

    A status-only reply leaks nothing about whether the email already
    existed; slug-related errors still surface via 409 body, per spec.
    """

    status: str = "accepted"


class VerifyBody(BaseModel):
    """Request body for ``POST /signup/verify``."""

    token: str


class VerifyResponse(BaseModel):
    """Response body carrying the signup-session id + desired slug."""

    signup_session_id: str
    desired_slug: str


class PasskeyStartBody(BaseModel):
    """Request body for ``POST /signup/passkey/start``."""

    signup_session_id: str
    # ``display_name`` + ``timezone`` are collected at passkey start so
    # the WebAuthn user entity has real values, even though the
    # :class:`User` row doesn't land until ``/signup/passkey/finish``.
    display_name: str = Field(..., min_length=1, max_length=160)


class PasskeyStartResponse(BaseModel):
    """Parsed PublicKeyCredentialCreationOptions + its challenge handle."""

    challenge_id: str
    options: dict[str, Any]


class PasskeyFinishBody(BaseModel):
    """Request body for ``POST /signup/passkey/finish``."""

    signup_session_id: str
    challenge_id: str
    display_name: str = Field(..., min_length=1, max_length=160)
    timezone: str = Field(..., min_length=1, max_length=80)
    credential: dict[str, Any]


class PasskeyFinishResponse(BaseModel):
    """Final redirect hint for the SPA."""

    workspace_slug: str
    redirect: str


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


_StartDomainError = (
    signup.SignupDisabled,
    InvalidSlug,
    signup.SlugReserved,
    signup.SlugTaken,
    signup.SlugHomoglyphError,
    signup.SlugInGracePeriod,
    RateLimited,
)


_VerifyDomainError = (
    signup.SignupDisabled,
    signup.SignupAttemptMissing,
    signup.SignupAttemptExpired,
    InvalidToken,
    PurposeMismatch,
    TokenExpired,
    AlreadyConsumed,
    ConsumeLockout,
    RateLimited,
)


_CompleteDomainError = (
    signup.SignupDisabled,
    signup.SignupAttemptMissing,
    signup.SignupAttemptExpired,
    passkey.ChallengeNotFound,
    passkey.ChallengeAlreadyConsumed,
    passkey.ChallengeExpired,
    passkey.ChallengeSubjectMismatch,
    passkey.InvalidRegistration,
    passkey.TooManyPasskeys,
)


def _http_for_start(exc: Exception) -> HTTPException:
    """Map a :func:`start_signup` domain error to an HTTP response."""
    if isinstance(exc, signup.SignupDisabled):
        # §03 spec says disabled deployments 404 the entire surface.
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found"},
        )
    if isinstance(exc, InvalidSlug):
        # Starlette renamed the constant from *_ENTITY to *_CONTENT in
        # a recent release; use the literal 422 so the router works
        # across minor versions without a conditional import.
        return HTTPException(
            status_code=422,
            detail={"error": "invalid_slug", "message": str(exc)},
        )
    if isinstance(exc, signup.SlugReserved):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "slug_reserved"},
        )
    if isinstance(exc, signup.SlugTaken):
        # Spec §03 step 1: ``409 slug_taken`` carries a
        # ``suggested_alternative`` the signup UI offers in one click.
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "slug_taken",
                "suggested_alternative": exc.suggested_alternative,
            },
        )
    if isinstance(exc, signup.SlugHomoglyphError):
        # Spec §03 requires the colliding slug in the body so the UI
        # can surface "you typed rnicasa but micasa is taken".
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "slug_homoglyph_collision",
                "colliding_slug": exc.colliding_slug,
            },
        )
    if isinstance(exc, signup.SlugInGracePeriod):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "slug_in_grace_period"},
        )
    # RateLimited — last branch; the mapper is exhaustive via
    # ``_StartDomainError``.
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"error": "rate_limited"},
    )


def _http_for_verify(exc: Exception) -> HTTPException:
    if isinstance(exc, signup.SignupDisabled):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found"},
        )
    if isinstance(exc, signup.SignupAttemptMissing):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "signup_attempt_not_found"},
        )
    if isinstance(exc, signup.SignupAttemptExpired):
        if exc.state == "expired":
            return HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={"error": "expired"},
            )
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": exc.state},
        )
    if isinstance(exc, TokenExpired):
        return HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"error": "expired"},
        )
    if isinstance(exc, AlreadyConsumed):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "already_consumed"},
        )
    if isinstance(exc, PurposeMismatch):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "purpose_mismatch"},
        )
    if isinstance(exc, ConsumeLockout):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "consume_locked_out"},
        )
    if isinstance(exc, RateLimited):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limited"},
        )
    # InvalidToken — default fallback for the verify family.
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "invalid_token"},
    )


def _http_for_complete(exc: Exception) -> HTTPException:
    if isinstance(exc, signup.SignupDisabled):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found"},
        )
    if isinstance(exc, signup.SignupAttemptMissing):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "signup_attempt_not_found"},
        )
    if isinstance(exc, signup.SignupAttemptExpired):
        if exc.state == "expired":
            return HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={"error": "expired"},
            )
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": exc.state},
        )
    if isinstance(exc, passkey.ChallengeNotFound | passkey.ChallengeAlreadyConsumed):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "challenge_consumed_or_unknown"},
        )
    if isinstance(exc, passkey.ChallengeExpired):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "challenge_expired"},
        )
    if isinstance(
        exc,
        passkey.InvalidRegistration | passkey.ChallengeSubjectMismatch,
    ):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_registration"},
        )
    # TooManyPasskeys — only reachable via a weird concurrent enrol,
    # but map it for completeness.
    return HTTPException(
        status_code=422,
        detail={"error": "too_many_passkeys"},
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    """Best-effort source IP for ``request`` — mirrors the magic router."""
    if request.client is None:
        return ""
    return request.client.host


def build_signup_router(
    *,
    mailer: Mailer,
    throttle: Throttle,
    capabilities: Capabilities,
    base_url: str | None = None,
    settings: Settings | None = None,
) -> APIRouter:
    """Return a fresh :class:`APIRouter` wired to ``mailer`` + ``throttle``.

    ``capabilities`` is the process-wide :class:`Capabilities`
    envelope; the router reads ``capabilities.settings.signup_enabled``
    on every request so a mid-flight flip (``admin settings
    signup_enabled false``) takes effect without restarting the
    worker (§01 "Capability registry").

    Mounted by the v1 app factory. Tests instantiate it directly with
    a recording mailer + per-case throttle for isolation.
    """
    router = APIRouter(prefix="/signup", tags=["auth", "signup"])
    cfg = settings if settings is not None else get_settings()
    resolved_base_url = base_url if base_url is not None else cfg.public_url

    @router.post(
        "/start",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=SignupStartResponse,
        summary="Start a self-serve signup; 404 if signup is disabled",
    )
    def post_start(
        body: SignupStartBody,
        request: Request,
        session: _Db,
    ) -> SignupStartResponse:
        """Kick off the signup flow — validate + mint link + audit."""
        if resolved_base_url is None:
            raise RuntimeError(
                "base_url / settings.public_url is not set; "
                "cannot build magic-link URLs"
            )
        try:
            signup.start_signup(
                session,
                email=body.email,
                desired_slug=body.desired_slug,
                ip=_client_ip(request),
                mailer=mailer,
                base_url=resolved_base_url,
                throttle=throttle,
                capabilities=capabilities,
                settings=cfg,
            )
        except _StartDomainError as exc:
            raise _http_for_start(exc) from exc
        return SignupStartResponse()

    @router.post(
        "/verify",
        response_model=VerifyResponse,
        summary="Consume the signup-verify magic link",
    )
    def post_verify(
        body: VerifyBody,
        request: Request,
        session: _Db,
    ) -> VerifyResponse:
        """Flip the signup_attempt to *verified* + return the session id."""
        if not capabilities.settings.signup_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found"},
            )
        try:
            ssn = signup.consume_verify(
                session,
                token=body.token,
                ip=_client_ip(request),
                throttle=throttle,
                capabilities=capabilities,
                settings=cfg,
            )
        except _VerifyDomainError as exc:
            raise _http_for_verify(exc) from exc
        return VerifyResponse(
            signup_session_id=ssn.signup_attempt_id,
            desired_slug=ssn.desired_slug,
        )

    @router.post(
        "/passkey/start",
        response_model=PasskeyStartResponse,
        summary="Mint the signup-flow passkey registration challenge",
    )
    def post_passkey_start(
        body: PasskeyStartBody,
        session: _Db,
    ) -> PasskeyStartResponse:
        """Delegate to :func:`app.auth.passkey.register_start_signup`."""
        if not capabilities.settings.signup_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found"},
            )
        # Load the signup_attempt for the email — the passkey service
        # needs the canonical email for the WebAuthn user entity's
        # ``name`` field. We keep the domain service's single source
        # of truth and don't accept an email from the body. The
        # signup_attempt row is tenant-agnostic (identity-layer);
        # wrap the read to bypass the ORM tenant filter.
        # justification: signup_attempt is identity-scoped.
        with tenant_agnostic():
            attempt = session.get(SignupAttempt, body.signup_session_id)
        if attempt is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "signup_attempt_not_found"},
            )
        try:
            opts = passkey.register_start_signup(
                session,
                signup_session_id=body.signup_session_id,
                email=attempt.email_lower,
                display_name=body.display_name,
            )
        except (
            passkey.ChallengeNotFound,
            passkey.ChallengeAlreadyConsumed,
            passkey.ChallengeExpired,
            passkey.ChallengeSubjectMismatch,
            passkey.InvalidRegistration,
            passkey.TooManyPasskeys,
        ) as exc:
            raise _http_for_complete(exc) from exc
        return PasskeyStartResponse(
            challenge_id=opts.challenge_id,
            options=opts.options,
        )

    @router.post(
        "/passkey/finish",
        response_model=PasskeyFinishResponse,
        summary="Complete signup — one-transaction workspace + user + passkey",
    )
    def post_passkey_finish(
        body: PasskeyFinishBody,
        request: Request,
        session: _Db,
    ) -> PasskeyFinishResponse:
        """Delegate to :func:`app.auth.signup.complete_signup`."""
        if not capabilities.settings.signup_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found"},
            )
        try:
            result = signup.complete_signup(
                session,
                signup_attempt_id=body.signup_session_id,
                display_name=body.display_name,
                timezone=body.timezone,
                challenge_id=body.challenge_id,
                passkey_payload=body.credential,
                ip=_client_ip(request),
                capabilities=capabilities,
                settings=cfg,
            )
        except _CompleteDomainError as exc:
            raise _http_for_complete(exc) from exc
        return PasskeyFinishResponse(
            workspace_slug=result.slug,
            redirect=f"/w/{result.slug}/today",
        )

    return router


# NOTE: the magic-link consume path audits its own ``magic_link.rejected``
# failures through a fresh UoW. The signup router above delegates to
# :func:`signup.consume_verify`, which delegates to
# :func:`magic_link.consume_link`, so that forensic trail is already
# in place — we don't re-implement it here.
