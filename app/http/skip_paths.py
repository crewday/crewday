"""Bare-host HTTP paths that bypass workspace-scoped middleware.

Derived from ``docs/specs/01-architecture.md`` §"Workspace addressing".
A request is "skipped" iff its path equals one of these strings OR starts
with one followed by ``/`` (a child segment). Keep this list in sync with
the reverse-proxy routing table and the reserved-slug list in
:mod:`app.tenancy.slug`.
"""

from __future__ import annotations

SKIP_PATHS: frozenset[str] = frozenset(
    {
        # Ops probes + identity surface (§01 "Workspace addressing").
        "/healthz",
        "/readyz",
        "/version",
        "/signup",
        "/login",
        "/recover",
        "/select-workspace",
        # Bare-host OpenAPI + docs (§12 "Base URL").
        "/api/openapi.json",
        "/api/v1",
        "/docs",
        "/redoc",
        # Bare-host auth surface (§03 "Self-serve signup", §12). Both
        # magic-link and passkey routers live here; keep the siblings in
        # lock-step so future routers (webauthn/*) are obvious to add.
        "/auth/magic",
        "/auth/passkey",
        # Bare-host email-change landing (§14 "Public"). Carries a
        # magic-link token, has no workspace until the swap completes.
        "/me/email/verify",
        # Bare-host admin shell + API (§14 "Admin", §12 "Admin surface").
        "/admin",
        "/webhooks",
        # Static assets + SPA chrome that the reverse proxy or FastAPI
        # may serve from the bare host (§14 "Shell chrome").
        "/static",
        "/assets",
        "/styleguide",
        "/unsupported",
        # Bare-host UI-preference cookie setters (see
        # :mod:`app.api.preferences`). Public, unauthenticated — the
        # cookies carry no authority (the SPA can spoof its own
        # ``document.cookie`` regardless), and the sidebar writers use
        # ``navigator.sendBeacon`` which cannot attach the ``X-CSRF``
        # header the CSRF middleware would otherwise require. Skipping
        # CSRF here too matches the mock implementation in
        # ``mocks/app/main.py`` and keeps the writer surface honest.
        #
        # The ``/workspaces``, ``/agent``, and ``/nav`` namespaces
        # could plausibly host future bare-host routes that DO carry
        # authority (a workspace search, an agent capability probe, a
        # nav config endpoint). Pin the skip prefixes to the exact
        # cookie-setter sub-paths so a future sibling route doesn't
        # silently inherit the CSRF bypass.
        "/switch",
        "/theme/set",
        "/workspaces/switch",
        "/agent/sidebar",
        "/nav/sidebar",
    }
)


def is_skip_path(path: str) -> bool:
    """Return ``True`` if ``path`` is a bare-host route we pass through.

    Matches either the exact skip-path value (``/healthz``) or a child
    segment rooted at it (``/static/app.css``, ``/docs/swagger.json``).
    Deliberately does NOT match a longer path that merely starts with
    the same characters (``/signup-flow`` is scoped, not a child of
    ``/signup``).
    """
    if path in SKIP_PATHS:
        return True
    # Child-segment check: longest skip-path is ``/select-workspace`` —
    # a single startswith-with-separator pass is cheap.
    return any(path.startswith(f"{prefix}/") for prefix in SKIP_PATHS)
