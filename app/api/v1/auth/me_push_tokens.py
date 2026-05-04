"""Native-app push-token HTTP router — register / list / refresh / unregister.

Mounted at bare host ``/api/v1/me/push-tokens`` (tenant-agnostic — native
push tokens live outside any workspace, §02 "user_push_token"). Every
route requires an authenticated **passkey session**; the surface is
deliberately self-only, so neither owners nor managers nor deployment
admins can enumerate another user's tokens.

Routes:

* ``POST /me/push-tokens`` ``{platform, token, device_label?,
  app_version?}`` -> ``201 {id, user_id, platform, device_label,
  app_version, created_at, last_seen_at, disabled_at}``. Idempotent on
  the ``(user_id, platform, token)`` triple — a re-registration of the
  same device returns the existing row with ``last_seen_at`` bumped and
  writes no audit row. A cross-user collision on the
  ``(platform, token)`` pair fails ``409 token_claimed`` per §02.
* ``GET /me/push-tokens`` -> list of :class:`PushTokenResponse`
  projections. Returns every push-token row owned by the session user
  (active + disabled). Always live — even on a deployment with the
  native push surface gated off, the SPA / native shell can list
  existing rows so the user can prune them.
* ``PUT /me/push-tokens/{token_id}`` ``{token?}`` -> ``200``.
  Bumps ``last_seen_at`` on the named row; when ``token`` is supplied,
  swaps the row's token to the new value (OS-rotated FCM / APNS
  handle). Targeting a row owned by another user collapses to
  ``404 push_token_not_found`` so the surface does not leak whether
  the id is enrollable elsewhere.
* ``DELETE /me/push-tokens/{token_id}`` -> ``204``. Removes the row.
  Idempotent on miss (already-deleted retry, cross-user attempt). Always
  live so a sign-out can prune a stale row regardless of whether the
  registration gate is on.

Error shapes:

* 401 ``session_required`` — no session cookie.
* 401 ``session_invalid`` — cookie unknown / expired / fingerprint gate
  fired.
* 404 ``push_token_not_found`` — refresh / delete targets an id that
  isn't a row owned by the caller.
* 409 ``token_claimed`` — register attempted with a ``(platform,
  token)`` pair already registered for another user (device hand-off
  without sign-out, §02 "user_push_token").
* 422 ``invalid_platform`` — ``platform`` outside the v1 whitelist
  (``android`` / ``ios``).
* 501 ``push_unavailable`` — register attempted on a deployment with
  :attr:`app.config.Settings.native_push_enabled` off (FCM / APNS not
  yet provisioned). ``GET`` and ``DELETE`` are intentionally
  exempted from the gate.

See ``docs/specs/02-domain-model.md`` §"user_push_token",
``docs/specs/12-rest-api.md`` §"Device push tokens", and
``docs/specs/14-web-frontend.md`` §"Native wrapper readiness".
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.adapters.db.identity.repositories import (
    SqlAlchemyUserPushTokenRepository,
)
from app.api.deps import db_session
from app.api.v1._problem_json import IDENTITY_PROBLEM_RESPONSES
from app.api.v1.auth.errors import auth_conflict, auth_not_found, auth_unauthorized
from app.auth import session as auth_session
from app.auth.session_cookie import DEV_SESSION_COOKIE_NAME
from app.config import get_settings
from app.domain.errors import NotImplementedFeature
from app.domain.errors import Validation as DomainValidation
from app.domain.identity.push_tokens import (
    InvalidPlatform,
    PushTokenNotFound,
    TokenClaimed,
    UserPushTokenView,
    list_for_user,
    refresh,
    register,
    unregister,
)

__all__ = [
    "PushTokenRefreshBody",
    "PushTokenRegisterBody",
    "PushTokenResponse",
    "build_me_push_tokens_router",
]


_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class PushTokenRegisterBody(BaseModel):
    """Request body for ``POST /api/v1/me/push-tokens``.

    ``platform`` is the §02 enum (``android`` / ``ios``); the FastAPI /
    Pydantic decoder rejects any other value at 422 before the domain
    service runs. ``token`` is the bare FCM / APNS handle — never
    echoed back over the wire (the response projection drops it).
    ``device_label`` and ``app_version`` are advisory hints the SPA
    surfaces in the device list.
    """

    model_config = ConfigDict(extra="forbid")

    platform: Literal["android", "ios"]
    token: str = Field(..., min_length=1, max_length=4096)
    # §02 ``user_push_token`` "Supplied by the client; trimmed to 64
    # chars". The DTO caps the wire length so a misbehaving client
    # gets a 422 rather than a silently-truncated row.
    device_label: str | None = Field(default=None, max_length=64)
    app_version: str | None = Field(default=None, max_length=64)


class PushTokenRefreshBody(BaseModel):
    """Request body for ``PUT /api/v1/me/push-tokens/{token_id}``.

    Both fields are optional: an empty body bumps ``last_seen_at``
    only; ``token`` (when supplied) swaps the row's token in place
    on OS-driven rotation. ``platform`` is intentionally NOT mutable
    — a different platform means a different device install.
    """

    model_config = ConfigDict(extra="forbid")

    token: str | None = Field(default=None, min_length=1, max_length=4096)


class PushTokenResponse(BaseModel):
    """Response element for the push-token routes.

    Mirrors :class:`~app.domain.identity.push_tokens.UserPushTokenView`
    column-for-column. Deliberately drops the raw ``token`` — §02
    "user_push_token" "no token payload" in any read surface.
    """

    id: str
    user_id: str
    platform: Literal["android", "ios"]
    device_label: str | None
    app_version: str | None
    created_at: datetime
    last_seen_at: datetime
    disabled_at: datetime | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_session_user(
    session: Session,
    *,
    cookie_primary: str | None,
    cookie_dev: str | None,
) -> str:
    """Return the authenticated user's id or raise HTTP 401.

    Matches :func:`app.api.v1.auth.me_tokens._resolve_session_user` —
    both the prod ``__Host-crewday_session`` and the dev fallback
    ``crewday_session`` are accepted. No fingerprint hints are
    plumbed through on v1 because the route is reachable only from an
    authenticated SPA / native shell whose session already passed the
    fingerprint gate on its last :func:`auth_session.validate`.
    """
    cookie_value = cookie_primary or cookie_dev
    if not cookie_value:
        raise auth_unauthorized("session_required")
    try:
        return auth_session.validate(session, cookie_value=cookie_value)
    except (auth_session.SessionInvalid, auth_session.SessionExpired) as exc:
        raise auth_unauthorized("session_invalid") from exc


def _view_to_response(view: UserPushTokenView) -> PushTokenResponse:
    """Translate the domain view into the wire shape."""
    # ``platform`` widened to ``str`` on the row but is constrained to
    # ``Literal["android", "ios"]`` on the wire — the CHECK constraint
    # at the DB layer guarantees the cast is sound.
    platform: Literal["android", "ios"] = (
        "android" if view.platform == "android" else "ios"
    )
    return PushTokenResponse(
        id=view.id,
        user_id=view.user_id,
        platform=platform,
        device_label=view.device_label,
        app_version=view.app_version,
        created_at=view.created_at,
        last_seen_at=view.last_seen_at,
        disabled_at=view.disabled_at,
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_me_push_tokens_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` for the native push-token surface.

    Factory shape matches every other auth router in the package so
    the app factory's wiring seam stays uniform and tests can mount
    the endpoint against an isolated FastAPI instance.
    """
    # Tags: ``identity`` clusters every identity-adjacent operation
    # under one OpenAPI section (spec §01 context map + §12 Auth);
    # ``push-tokens`` is the fine-grained client-filter tag.
    router = APIRouter(
        prefix="/me/push-tokens",
        tags=["identity", "push-tokens"],
        responses=IDENTITY_PROBLEM_RESPONSES,
    )

    @router.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=PushTokenResponse,
        operation_id="auth.me.push_tokens.register",
        summary="Register a native-app push token for the caller's device",
        openapi_extra={
            # Bare-host identity-scoped surface — distinct CLI group
            # so the runtime (cd-lato) does not collide with the
            # workspace-scoped web-push surface under
            # :mod:`app.api.v1.push_tokens` (which carries its own
            # ``push-tokens`` group).
            "x-cli": {
                "group": "me-push-tokens",
                "verb": "register",
                "summary": "Register a native-app push token",
                "mutates": True,
            },
        },
    )
    def post_me_push_token(
        body: PushTokenRegisterBody,
        session: _Db,
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias=DEV_SESSION_COOKIE_NAME),
        ] = None,
    ) -> PushTokenResponse:
        """Register (or re-register) a native push token for the session user."""
        user_id = _resolve_session_user(
            session,
            cookie_primary=session_cookie_primary,
            cookie_dev=session_cookie_dev,
        )
        # 501 short-circuit — see §02 "user_push_token" §"Surface": a
        # deployment without FCM / APNS credentials must reject
        # registration with a deterministic ``push_unavailable`` so the
        # native shell can fall back to in-app polling. ``GET`` and
        # ``DELETE`` deliberately bypass the gate so a sign-out can
        # still prune a stale row.
        if not get_settings().native_push_enabled:
            raise NotImplementedFeature(
                "native push delivery is not enabled on this deployment",
                extra={"error": "push_unavailable"},
            )
        repo = SqlAlchemyUserPushTokenRepository(session)
        try:
            view = register(
                repo,
                user_id=user_id,
                platform=body.platform,
                token=body.token,
                device_label=body.device_label,
                app_version=body.app_version,
            )
        except InvalidPlatform as exc:
            # Pydantic's literal narrowing already rejects an unknown
            # platform at 422; this branch fires only when a future
            # caller widens the DTO without keeping the domain
            # whitelist in lockstep.
            raise DomainValidation(
                str(exc),
                extra={"error": "invalid_platform"},
            ) from exc
        except TokenClaimed as exc:
            raise auth_conflict(
                "token_claimed",
                (
                    "this device handle is already registered for "
                    "another user; the previous owner must sign out first"
                ),
            ) from exc
        return _view_to_response(view)

    @router.get(
        "",
        response_model=list[PushTokenResponse],
        operation_id="auth.me.push_tokens.list",
        summary="List every native push token the caller has registered",
        openapi_extra={
            "x-cli": {
                "group": "me-push-tokens",
                "verb": "list",
                "summary": "List your native push tokens",
                "mutates": False,
            },
        },
    )
    def get_me_push_tokens(
        session: _Db,
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias=DEV_SESSION_COOKIE_NAME),
        ] = None,
    ) -> list[PushTokenResponse]:
        """Return every native push-token row for the session user."""
        user_id = _resolve_session_user(
            session,
            cookie_primary=session_cookie_primary,
            cookie_dev=session_cookie_dev,
        )
        repo = SqlAlchemyUserPushTokenRepository(session)
        views = list_for_user(repo, user_id=user_id)
        return [_view_to_response(view) for view in views]

    @router.put(
        "/{token_id}",
        response_model=PushTokenResponse,
        operation_id="auth.me.push_tokens.refresh",
        summary="Refresh last_seen_at (and optionally swap token) on a row",
        openapi_extra={
            "x-cli": {
                "group": "me-push-tokens",
                "verb": "refresh",
                "summary": "Refresh a native push token (last_seen + rotation)",
                "mutates": True,
            },
        },
    )
    def put_me_push_token(
        token_id: str,
        body: PushTokenRefreshBody,
        session: _Db,
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias=DEV_SESSION_COOKIE_NAME),
        ] = None,
    ) -> PushTokenResponse:
        """Bump ``last_seen_at`` (and optionally swap ``token``) on the row."""
        user_id = _resolve_session_user(
            session,
            cookie_primary=session_cookie_primary,
            cookie_dev=session_cookie_dev,
        )
        repo = SqlAlchemyUserPushTokenRepository(session)
        try:
            view = refresh(
                repo,
                user_id=user_id,
                token_id=token_id,
                token=body.token,
            )
        except PushTokenNotFound as exc:
            raise auth_not_found("push_token_not_found") from exc
        return _view_to_response(view)

    @router.delete(
        "/{token_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="auth.me.push_tokens.unregister",
        summary="Unregister one of the caller's native push tokens",
        openapi_extra={
            "x-cli": {
                "group": "me-push-tokens",
                "verb": "unregister",
                "summary": "Unregister a native push token",
                "mutates": True,
            },
        },
    )
    def delete_me_push_token(
        token_id: str,
        session: _Db,
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias=DEV_SESSION_COOKIE_NAME),
        ] = None,
    ) -> Response:
        """Unregister a native push token. Idempotent on miss."""
        user_id = _resolve_session_user(
            session,
            cookie_primary=session_cookie_primary,
            cookie_dev=session_cookie_dev,
        )
        repo = SqlAlchemyUserPushTokenRepository(session)
        unregister(repo, user_id=user_id, token_id=token_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
