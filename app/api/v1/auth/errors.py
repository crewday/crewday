"""Shared problem+json errors for auth routers."""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.errors import (
    BadRequest,
    Conflict,
    Forbidden,
    Gone,
    NotFound,
    RateLimited,
    Unauthorized,
)

__all__ = [
    "AuthBadRequest",
    "AuthConflict",
    "AuthForbidden",
    "AuthGone",
    "AuthNotFound",
    "AuthRateLimited",
    "AuthUnauthorized",
    "auth_bad_request",
    "auth_conflict",
    "auth_forbidden",
    "auth_gone",
    "auth_not_found",
    "auth_rate_limited",
    "auth_unauthorized",
]


class AuthBadRequest(BadRequest):
    """Auth-router 400 carrying the legacy ``error`` extension."""


class AuthUnauthorized(Unauthorized):
    """Auth-router 401 carrying the legacy ``error`` extension."""


class AuthForbidden(Forbidden):
    """Auth-router 403 carrying the legacy ``error`` extension."""


class AuthGone(Gone):
    """Auth-router 410 carrying the legacy ``error`` extension."""


class AuthNotFound(NotFound):
    """Auth-router 404 carrying the legacy ``error`` extension."""


class AuthConflict(Conflict):
    """Auth-router 409 carrying the legacy ``error`` extension."""


class AuthRateLimited(RateLimited):
    """Auth-router 429 carrying the legacy ``error`` extension."""


def _with_error(
    symbol: str,
    extra: Mapping[str, object] | None,
) -> dict[str, object]:
    merged = dict(extra) if extra is not None else {}
    merged["error"] = symbol
    return merged


def auth_bad_request(
    symbol: str,
    detail: str | None = None,
    *,
    extra: Mapping[str, object] | None = None,
) -> AuthBadRequest:
    return AuthBadRequest(detail, extra=_with_error(symbol, extra))


def auth_unauthorized(
    symbol: str,
    detail: str | None = None,
    *,
    extra: Mapping[str, object] | None = None,
) -> AuthUnauthorized:
    return AuthUnauthorized(detail, extra=_with_error(symbol, extra))


def auth_forbidden(
    symbol: str,
    detail: str | None = None,
    *,
    extra: Mapping[str, object] | None = None,
) -> AuthForbidden:
    return AuthForbidden(detail, extra=_with_error(symbol, extra))


def auth_gone(
    symbol: str,
    detail: str | None = None,
    *,
    extra: Mapping[str, object] | None = None,
) -> AuthGone:
    return AuthGone(detail, extra=_with_error(symbol, extra))


def auth_not_found(
    symbol: str,
    detail: str | None = None,
    *,
    extra: Mapping[str, object] | None = None,
) -> AuthNotFound:
    return AuthNotFound(detail, extra=_with_error(symbol, extra))


def auth_conflict(
    symbol: str,
    detail: str | None = None,
    *,
    extra: Mapping[str, object] | None = None,
) -> AuthConflict:
    return AuthConflict(detail, extra=_with_error(symbol, extra))


def auth_rate_limited(
    symbol: str = "rate_limited",
    detail: str | None = None,
    *,
    retry_after_seconds: int | None = None,
    extra: Mapping[str, object] | None = None,
) -> AuthRateLimited:
    merged = _with_error(symbol, extra)
    if retry_after_seconds is not None:
        merged["retry_after_seconds"] = retry_after_seconds
    return AuthRateLimited(detail, extra=merged)
