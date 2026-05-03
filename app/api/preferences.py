"""Cookie-setting preference endpoints (bare-host, public).

Five fire-and-forget endpoints the SPA hits to persist UI preferences
the browser then reads back synchronously from ``document.cookie`` on
the next page load. Mirrors the mock implementation in
``mocks/app/main.py``:

* ``/switch/{role}``          — ``crewday_role`` (employee/manager/client/admin)
* ``/theme/set/{value}``      — ``crewday_theme`` (light/dark/system)
* ``/workspaces/switch/{wsid}`` — ``crewday_workspace`` (opaque ULID)
* ``/agent/sidebar/{state}``  — ``crewday_agent_collapsed`` (1/0)
* ``/nav/sidebar/{state}``    — ``crewday_nav_collapsed`` (1/0)

Why these are bare-host and unauthenticated:

* They are pure UI hints. The cookies are read by the SPA shell only
  to hydrate initial state (avoid theme flicker, restore the rail
  collapsed/open). Auth and authorisation still happen at the data
  layer — these cookies confer **no** authority, so the worst case of
  spoofing is a stale UI hint that the next ``/api/v1/auth/me`` call
  immediately corrects.
* They mount on the bare host because the SPA writes them before a
  workspace context exists (workspace switcher) and because the
  ``crewday_workspace`` cookie *is* the workspace pointer the
  tenancy middleware would otherwise need to resolve.

Cookie attributes mirror :mod:`mocks.app.main`:

* ``Path=/``, ``SameSite=Lax`` — readable on the SPA's pages, sent on
  top-level navigations so a bookmark restores the right shell state.
* **Not** ``HttpOnly`` — the SPA reads them via ``document.cookie``.
* ``Secure`` only when the request arrived over HTTPS. The dev
  loopback (``http://127.0.0.1:8100``) gets a non-Secure cookie so
  the same code path works without TLS termination.
* ``Max-Age`` chosen per cookie: 30 days for role/workspace, 365
  days for theme + sidebar collapse — visual prefs are stickier than
  identity hints.

CSRF: the cookie-setter sub-paths (``/switch``, ``/theme/set``,
``/workspaces/switch``, ``/agent/sidebar``, ``/nav/sidebar``) sit on
:data:`app.tenancy.middleware.SKIP_PATHS`, so
:class:`app.auth.csrf.CSRFMiddleware` does **not** enforce the
double-submit pair on them. The skip-prefix entries are deliberately
pinned to those exact sub-paths (not the wider ``/workspaces``,
``/agent``, or ``/nav`` namespaces) so a future sibling route under
the same namespace doesn't silently inherit the CSRF bypass. Two
reasons make the bypass safe for the cookie setters specifically:

* The cookies confer no authority. The SPA can already write any
  ``crewday_*`` cookie via ``document.cookie`` from its own JS, so a
  cross-site form post that flips the user's theme or rail collapse
  achieves nothing more than a stale UI hint that the next
  ``/api/v1/auth/me`` call corrects.
* The sidebar writers use ``navigator.sendBeacon`` which cannot
  attach a custom ``X-CSRF`` header. Forcing CSRF here would either
  break the writer or push us back to ``fetch``-only without any
  real security gain.

The ``/switch/{role}`` and ``/theme/set/{value}`` endpoints
additionally accept ``GET`` for legacy ``href=`` writers in the mocks,
which matches ``mocks/app/main.py``.

Validation: unknown values for role / theme / state return ``400``.
The workspace id passes through unchanged — the cookie is just a
pointer; the tenancy middleware re-validates on every request that
actually queries the workspace.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Request, Response

__all__ = ["build_preferences_router"]


# ---------------------------------------------------------------------------
# Cookie names — aligned with the SPA reader in
# ``app/web/src/lib/preferences.ts`` and the existing reads in
# ``app/api/v1/auth/me.py`` (``crewday_theme``, ``crewday_agent_collapsed``).
# ---------------------------------------------------------------------------

_ROLE_COOKIE: Final[str] = "crewday_role"
_THEME_COOKIE: Final[str] = "crewday_theme"
_WORKSPACE_COOKIE: Final[str] = "crewday_workspace"
_AGENT_COLLAPSED_COOKIE: Final[str] = "crewday_agent_collapsed"
_NAV_COLLAPSED_COOKIE: Final[str] = "crewday_nav_collapsed"


# Validation sets — match :mod:`mocks.app.main` so the mocks and the
# production app agree on what counts as a valid preference value.
_VALID_ROLES: Final[frozenset[str]] = frozenset(
    {"employee", "manager", "client", "admin"}
)
_VALID_THEMES: Final[frozenset[str]] = frozenset({"light", "dark", "system"})
_VALID_SIDEBAR_STATES: Final[frozenset[str]] = frozenset({"open", "collapsed"})


# Cookie lifetimes (seconds). Identity-shaped hints (role, workspace
# pointer) age out quicker so a stale browser doesn't keep nudging the
# user back into a workspace they've left; visual prefs (theme, rail
# collapse) are sticky for a year because re-picking a dark theme on
# every login is friction without value.
_ROLE_MAX_AGE: Final[int] = 60 * 60 * 24 * 30  # 30 days
_WORKSPACE_MAX_AGE: Final[int] = 60 * 60 * 24 * 30  # 30 days
_THEME_MAX_AGE: Final[int] = 60 * 60 * 24 * 365  # 365 days
_SIDEBAR_MAX_AGE: Final[int] = 60 * 60 * 24 * 365  # 365 days


def _is_secure_request(request: Request) -> bool:
    """Return ``True`` when the request arrived over HTTPS.

    Dev loopback (``http://127.0.0.1:8100``) returns ``False`` so the
    cookie is emitted without ``Secure`` and the browser keeps it; the
    public ``https://dev.crew.day`` deployment returns ``True``.
    """
    return request.url.scheme == "https"


def _set_pref_cookie(
    response: Response,
    *,
    name: str,
    value: str,
    max_age: int,
    secure: bool,
) -> None:
    """Stamp a preference cookie on ``response`` with the shared shape.

    ``HttpOnly`` is deliberately ``False`` — the SPA reads these via
    ``document.cookie`` for synchronous hydration. The auth session
    cookie is the only ``HttpOnly`` cookie this app emits.
    """
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        path="/",
        samesite="lax",
        httponly=False,
        secure=secure,
    )


def build_preferences_router() -> APIRouter:
    """Return the bare-host router for the five UI-preference cookies."""
    router = APIRouter(tags=["preferences"], include_in_schema=False)

    @router.api_route("/switch/{role}", methods=["GET", "POST"])
    def switch_role(role: str, request: Request) -> Response:
        """Set ``crewday_role`` to ``role`` after validating against the
        spec-pinned set. Returns 204 on success, 400 on unknown role.
        """
        if role not in _VALID_ROLES:
            return Response(status_code=400)
        response = Response(status_code=204)
        _set_pref_cookie(
            response,
            name=_ROLE_COOKIE,
            value=role,
            max_age=_ROLE_MAX_AGE,
            secure=_is_secure_request(request),
        )
        return response

    @router.api_route("/theme/set/{value}", methods=["GET", "POST"])
    def theme_set(value: str, request: Request) -> Response:
        """Set ``crewday_theme``. Mocks accepts both verbs; we mirror it
        so a legacy ``<a href>`` write still lands.
        """
        if value not in _VALID_THEMES:
            return Response(status_code=400)
        response = Response(status_code=204)
        _set_pref_cookie(
            response,
            name=_THEME_COOKIE,
            value=value,
            max_age=_THEME_MAX_AGE,
            secure=_is_secure_request(request),
        )
        return response

    @router.post("/workspaces/switch/{wsid}")
    def switch_workspace(wsid: str, request: Request) -> Response:
        """Set ``crewday_workspace`` to ``wsid``. The cookie is just a
        pointer the SPA reads to bias workspace selection; the tenancy
        middleware re-validates membership on every request that
        actually queries the workspace, so we do not check it here.
        """
        response = Response(status_code=204)
        _set_pref_cookie(
            response,
            name=_WORKSPACE_COOKIE,
            value=wsid,
            max_age=_WORKSPACE_MAX_AGE,
            secure=_is_secure_request(request),
        )
        return response

    @router.post("/agent/sidebar/{state}")
    def agent_sidebar_set(state: str, request: Request) -> Response:
        """Set ``crewday_agent_collapsed`` to ``"1"`` (collapsed) or
        ``"0"`` (open). Tri-state on the reader side: missing means
        "no preference, use viewport default".
        """
        if state not in _VALID_SIDEBAR_STATES:
            return Response(status_code=400)
        response = Response(status_code=204)
        _set_pref_cookie(
            response,
            name=_AGENT_COLLAPSED_COOKIE,
            value="1" if state == "collapsed" else "0",
            max_age=_SIDEBAR_MAX_AGE,
            secure=_is_secure_request(request),
        )
        return response

    @router.post("/nav/sidebar/{state}")
    def nav_sidebar_set(state: str, request: Request) -> Response:
        """Set ``crewday_nav_collapsed``. Same tri-state shape as the
        agent rail above.
        """
        if state not in _VALID_SIDEBAR_STATES:
            return Response(status_code=400)
        response = Response(status_code=204)
        _set_pref_cookie(
            response,
            name=_NAV_COLLAPSED_COOKIE,
            value="1" if state == "collapsed" else "0",
            max_age=_SIDEBAR_MAX_AGE,
            secure=_is_secure_request(request),
        )
        return response

    return router
