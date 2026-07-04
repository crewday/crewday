"""Shared bare-host session-user resolver for the identity routers.

The ``/me`` token, push-token, avatar, email-change, and export routers
all start the same way: read the ``__Host-crewday_session`` (or dev
``crewday_session``) cookie, validate it, and turn it into the acting
user. This module is the single seam for that step so the five routers
don't each carry their own copy.

Two axes of variation, both parameterised:

* ``request`` — when supplied, the caller's ``User-Agent`` +
  ``Accept-Language`` are forwarded so the §15 fingerprint gate fires,
  and a :class:`~app.auth.session.UserArchived` result surfaces the
  typed :data:`~app.auth.session.USER_ARCHIVED_WIRE_CODE`. When omitted
  (token / push-token / export routers), no fingerprint headers are
  forwarded and archived collapses to the generic ``session_invalid``
  via the :class:`~app.auth.session.SessionInvalid` superclass —
  preserving those routers' original behaviour exactly.
* ``hydrate`` — when true (avatar router), the :class:`User` row is
  loaded and returned instead of the bare id, saving a second
  ``session.get`` bounce in a router that mutates the row directly.
"""

from __future__ import annotations

from typing import Literal, overload

from fastapi import Request
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.api.v1.auth.errors import auth_unauthorized
from app.auth import session as auth_session
from app.tenancy import tenant_agnostic

__all__ = ["resolve_bare_host_session_user"]


@overload
def resolve_bare_host_session_user(
    session: Session,
    *,
    cookie_primary: str | None,
    cookie_dev: str | None,
    request: Request | None = ...,
    hydrate: Literal[False] = ...,
) -> str: ...


@overload
def resolve_bare_host_session_user(
    session: Session,
    *,
    cookie_primary: str | None,
    cookie_dev: str | None,
    request: Request | None = ...,
    hydrate: Literal[True],
) -> User: ...


def resolve_bare_host_session_user(
    session: Session,
    *,
    cookie_primary: str | None,
    cookie_dev: str | None,
    request: Request | None = None,
    hydrate: bool = False,
) -> str | User:
    """Return the session user's id (or hydrated row) or raise HTTP 401.

    Both the prod ``__Host-crewday_session`` and the dev fallback
    ``crewday_session`` cookies are accepted. See the module docstring
    for the ``request`` / ``hydrate`` semantics.
    """
    cookie_value = cookie_primary or cookie_dev
    if not cookie_value:
        raise auth_unauthorized("session_required")
    if request is not None:
        ua = request.headers.get("user-agent", "")
        accept_language = request.headers.get("accept-language", "")
    else:
        ua = ""
        accept_language = ""
    try:
        user_id = auth_session.validate(
            session,
            cookie_value=cookie_value,
            ua=ua,
            accept_language=accept_language,
        )
    except auth_session.UserArchived as exc:
        # UserArchived is a subclass of SessionInvalid. Header-forwarding
        # callers surface the typed archived wire code; header-less
        # callers historically collapsed it to the generic
        # ``session_invalid`` via the superclass branch — keep that.
        wire = (
            auth_session.USER_ARCHIVED_WIRE_CODE
            if request is not None
            else "session_invalid"
        )
        raise auth_unauthorized(wire) from exc
    except (auth_session.SessionInvalid, auth_session.SessionExpired) as exc:
        raise auth_unauthorized("session_invalid") from exc

    if not hydrate:
        return user_id

    # ``user`` is identity-scoped — no workspace filter to apply; keyed
    # by the session's own user_id.
    with tenant_agnostic():
        user = session.get(User, user_id)
    if user is None:
        # Row referenced by the session was hard-deleted between validate
        # and lookup. Collapse to 401 — same shape the SPA already
        # handles on a stale session.
        raise auth_unauthorized("session_invalid")
    return user
