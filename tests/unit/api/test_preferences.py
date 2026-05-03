"""HTTP tests for :mod:`app.api.preferences`.

Five bare-host cookie-setter endpoints that hydrate the SPA's UI
preferences (role, theme, workspace pointer, agent rail collapse, nav
rail collapse). Verified end-to-end through a minimal FastAPI app
mounting only the preferences router — no DB, no workspace context, no
auth.

The CSRF + tenancy bypass is verified separately by the wider
:mod:`tests.unit.test_tenancy_middleware` suite (the prefix entries in
``SKIP_PATHS``); here we focus on the per-endpoint contract:

* validation rejects unknown role / theme / state values with 400;
* successful calls return 204 and stamp the matching cookie on the
  response;
* the cookie attributes follow the §15 shape (no ``HttpOnly`` so the
  SPA can read it back, ``SameSite=Lax``, ``Path=/``).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.preferences import build_preferences_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(build_preferences_router())
    return TestClient(app)


def _cookie_attrs(set_cookie_header: str) -> dict[str, str]:
    """Return the cookie attributes (lower-cased keys, raw values).

    The first entry is ``<name>=<value>``; subsequent entries are
    attribute=value or bare flags (``HttpOnly``, ``Secure``).
    """
    parts = [chunk.strip() for chunk in set_cookie_header.split(";")]
    attrs: dict[str, str] = {}
    for i, chunk in enumerate(parts):
        if i == 0:
            name, _, value = chunk.partition("=")
            attrs["__name"] = name
            attrs["__value"] = value
            continue
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            attrs[key.lower()] = value
        else:
            attrs[chunk.lower()] = ""
    return attrs


# ---------------------------------------------------------------------------
# /switch/{role} — crewday_role
# ---------------------------------------------------------------------------


class TestSwitchRole:
    def test_post_sets_role_cookie(self) -> None:
        response = _client().post("/switch/manager")
        assert response.status_code == 204
        cookie = response.headers.get("set-cookie")
        assert cookie is not None
        attrs = _cookie_attrs(cookie)
        assert attrs["__name"] == "crewday_role"
        assert attrs["__value"] == "manager"
        assert attrs["path"] == "/"
        assert attrs["samesite"].lower() == "lax"
        # SPA reads via document.cookie — must NOT be HttpOnly.
        assert "httponly" not in attrs

    def test_get_also_sets_role_cookie(self) -> None:
        # Mocks accepts both verbs for legacy ``<a href>`` writers.
        response = _client().get("/switch/client")
        assert response.status_code == 204
        attrs = _cookie_attrs(response.headers["set-cookie"])
        assert attrs["__value"] == "client"

    def test_unknown_role_returns_400(self) -> None:
        response = _client().post("/switch/wat")
        assert response.status_code == 400
        # No cookie on the rejection.
        assert "set-cookie" not in response.headers

    def test_admin_is_accepted(self) -> None:
        response = _client().post("/switch/admin")
        assert response.status_code == 204
        attrs = _cookie_attrs(response.headers["set-cookie"])
        assert attrs["__value"] == "admin"


# ---------------------------------------------------------------------------
# /theme/set/{value} — crewday_theme
# ---------------------------------------------------------------------------


class TestThemeSet:
    def test_post_sets_theme_cookie(self) -> None:
        response = _client().post("/theme/set/dark")
        assert response.status_code == 204
        attrs = _cookie_attrs(response.headers["set-cookie"])
        assert attrs["__name"] == "crewday_theme"
        assert attrs["__value"] == "dark"

    def test_get_also_sets_theme_cookie(self) -> None:
        response = _client().get("/theme/set/light")
        assert response.status_code == 204
        attrs = _cookie_attrs(response.headers["set-cookie"])
        assert attrs["__value"] == "light"

    def test_unknown_theme_returns_400(self) -> None:
        response = _client().post("/theme/set/neon")
        assert response.status_code == 400
        assert "set-cookie" not in response.headers


# ---------------------------------------------------------------------------
# /workspaces/switch/{wsid} — crewday_workspace
# ---------------------------------------------------------------------------


class TestWorkspaceSwitch:
    def test_post_sets_workspace_cookie(self) -> None:
        response = _client().post("/workspaces/switch/01HW7XYZ")
        assert response.status_code == 204
        attrs = _cookie_attrs(response.headers["set-cookie"])
        assert attrs["__name"] == "crewday_workspace"
        assert attrs["__value"] == "01HW7XYZ"

    def test_get_is_not_supported(self) -> None:
        # Workspace switch is POST only — mocks accepts GET as a fallback,
        # but the production app omits the GET pivot since the SPA
        # never falls back to ``<a href>`` for the workspace switcher.
        response = _client().get("/workspaces/switch/01HW7XYZ")
        assert response.status_code == 405


# ---------------------------------------------------------------------------
# /agent/sidebar/{state} — crewday_agent_collapsed
# ---------------------------------------------------------------------------


class TestAgentSidebar:
    def test_collapsed_writes_one(self) -> None:
        response = _client().post("/agent/sidebar/collapsed")
        assert response.status_code == 204
        attrs = _cookie_attrs(response.headers["set-cookie"])
        assert attrs["__name"] == "crewday_agent_collapsed"
        assert attrs["__value"] == "1"

    def test_open_writes_zero(self) -> None:
        response = _client().post("/agent/sidebar/open")
        assert response.status_code == 204
        attrs = _cookie_attrs(response.headers["set-cookie"])
        assert attrs["__value"] == "0"

    def test_unknown_state_returns_400(self) -> None:
        response = _client().post("/agent/sidebar/wat")
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# /nav/sidebar/{state} — crewday_nav_collapsed
# ---------------------------------------------------------------------------


class TestNavSidebar:
    def test_collapsed_writes_one(self) -> None:
        response = _client().post("/nav/sidebar/collapsed")
        assert response.status_code == 204
        attrs = _cookie_attrs(response.headers["set-cookie"])
        assert attrs["__name"] == "crewday_nav_collapsed"
        assert attrs["__value"] == "1"

    def test_open_writes_zero(self) -> None:
        response = _client().post("/nav/sidebar/open")
        assert response.status_code == 204
        attrs = _cookie_attrs(response.headers["set-cookie"])
        assert attrs["__value"] == "0"

    def test_unknown_state_returns_400(self) -> None:
        response = _client().post("/nav/sidebar/wat")
        assert response.status_code == 400
