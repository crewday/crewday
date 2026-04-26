"""Pilot e2e: first-boot owner enrollment lands on the role home (cd-ndmv).

Spec: ``docs/specs/17-testing-quality.md`` §"End-to-end" — journey 1
of the GA five.

The full journey is *signup → magic-link → passkey-register → today*
via the WebAuthn virtual authenticator (see
``tests/e2e/_helpers/auth.py::enroll_owner``). Today the dev compose
stack ships with ``CREWDAY_WEBAUTHN_RP_ID=dev.crew.day`` while the
loopback origin Playwright drives is ``http://127.0.0.1:8100`` —
the browser refuses ``navigator.credentials.create()`` against a
mismatched RP, so the full ceremony cannot complete at the loopback.

The pilot therefore takes the **dev_login fast path**: mint a real
session cookie via ``scripts/dev_login.py`` (which provisions the
same User / Workspace / RoleGrant / owners-membership rows the
self-serve signup would create) and assert the SPA renders the
freshly-provisioned owner's role-home page.

Once the RP-ID alignment lands (follow-up Beads), the pilot promotes
to the full ceremony by swapping ``login_with_dev_session`` for
``enroll_owner`` and adding an ``install_virtual_authenticator``
fixture. The assertion shape is identical so the diff is a one-liner.

Cross-browser parity per the spec: parametrised over Chromium and
WebKit. Both browsers can consume the cookie path; only the WebAuthn
ceremony is Chromium-only via CDP.
"""

from __future__ import annotations

from typing import Final

import pytest

from tests.e2e._helpers.auth import login_with_dev_session

# Email + slug for the pilot. ``e2e-pilot`` keeps the row out of any
# manual smoke testing the developer might do (a focused "this is the
# Playwright row" prefix), and the slug doubles as the workspace
# display name in dev_login since the script defaults
# ``Workspace.name`` to the slug when none is given.
PILOT_EMAIL: Final[str] = "e2e-pilot-first-boot@dev.local"
PILOT_SLUG: Final[str] = "e2e-pilot-first-boot"


# Run on both engines per §17 "End-to-end" cross-browser parity.
# pytest-playwright's ``--browser`` flag drives the matrix at the CLI;
# this in-test parametrisation is a safety net so the suite still
# covers both browsers when the operator runs ``pytest tests/e2e``
# without the flag.
@pytest.mark.parametrize("browser_name_under_test", ["chromium", "webkit"])
def test_first_boot_owner_lands_on_role_home(
    browser_name_under_test: str,
    base_url: str,
    browser_type_launch_args: dict[str, object],
    playwright: object,
) -> None:
    """Provision an owner via dev_login, assert they land on /dashboard.

    Manager grant lands at ``/dashboard`` per ``LoginPage.tsx``'s
    ``ROLE_LANDING`` map; an owner role-grant from dev_login resolves
    to manager (the v1 enum collapses owner→manager — see
    :mod:`scripts.dev_login`), so the role-home redirect is
    ``/dashboard``.

    Why a per-test parametrise instead of the ``--browser`` flag: the
    spec is explicit that both engines must pass; gating that on a CLI
    flag means a developer running ``pytest tests/e2e`` without the
    flag silently skips half the contract. The parametrise pulls the
    engine into the test signature so the matrix is documented in
    code.
    """
    browser_type = getattr(playwright, browser_name_under_test)
    try:
        browser = browser_type.launch(**browser_type_launch_args)
    except Exception as exc:
        # WebKit on Ubuntu 25.10+ (questing) wants ``libicu74``,
        # which the distro ships as ``libicu76``; the missing-deps
        # diagnostic comes back as a generic ``Error`` envelope from
        # CDP. We skip with the original message so the developer
        # sees the focused remediation hint without a redundant
        # traceback. Chromium has no equivalent gap on this distro,
        # so a Chromium failure here is a real regression.
        if "missing dependencies" in str(exc) or "libicu" in str(exc):
            pytest.skip(
                f"{browser_name_under_test}: host missing browser deps — "
                f"{exc!r}; install via `sudo playwright install-deps`"
            )
        raise
    try:
        context = browser.new_context(base_url=base_url)
        try:
            login = login_with_dev_session(
                context,
                base_url=base_url,
                email=PILOT_EMAIL,
                workspace_slug=PILOT_SLUG,
                role="owner",
            )
            page = context.new_page()
            # Round-trip the auth gate via the JSON API first — the
            # SPA's RoleHome redirect happens client-side after the
            # auth store loads, so a direct goto('/') would race the
            # token-vs-render. Hitting /api/v1/auth/me up front
            # confirms the cookie reaches the server with a 200 and
            # surfaces auth failures (401 on a bad cookie) at a
            # focused assertion point rather than as a "still on
            # /login" red herring later.
            me_response = page.request.get(f"{base_url.rstrip('/')}/api/v1/auth/me")
            assert me_response.status == 200, (
                f"/api/v1/auth/me returned {me_response.status}; "
                "dev_login cookie did not authenticate the request — "
                f"body={me_response.text()[:200]!r}"
            )
            payload = me_response.json()
            assert payload["email"] == login.email, (
                f"/api/v1/auth/me email mismatch: server={payload['email']!r}, "
                f"expected={login.email!r}"
            )
            assert payload["available_workspaces"], (
                "no workspaces on the freshly-provisioned owner"
            )
            assert payload["available_workspaces"][0]["workspace"]["id"] == login.slug

            # Now drive the SPA. RoleHome at "/" Navigate-redirects
            # to /dashboard for manager-grant users (owner → manager
            # per the v1 enum collapse — see scripts.dev_login).
            #
            # Wait on `networkidle` rather than `wait_for_url(lambda)`:
            # the lambda variant registers a navigation listener
            # *after* the goto resolves, so a fast SPA-side redirect
            # finishes before the listener arms and the wait times
            # out on a "no further navigation" idle state. networkidle
            # waits until the SPA's bootstrap probe + RoleHome
            # Navigate have all settled, then we read page.url
            # directly.
            page.goto(f"{base_url.rstrip('/')}/")
            page.wait_for_load_state("networkidle", timeout=15_000)
            assert "/dashboard" in page.url or "/today" in page.url, (
                f"expected role-home redirect but landed at {page.url}"
            )
        finally:
            context.close()
    finally:
        browser.close()
