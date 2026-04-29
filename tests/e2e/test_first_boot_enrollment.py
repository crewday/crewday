"""Pilot e2e: first-boot owner enrollment lands on the role home (cd-ndmv).

Spec: ``docs/specs/17-testing-quality.md`` §"End-to-end" — journey 1
of the GA five.

The journey is *signup → magic-link → passkey-register → passkey-login
assertion → role home* via the Chromium CDP WebAuthn virtual
authenticator (see ``tests/e2e._helpers.auth.enroll_owner``). The
dev stack must be started with ``mocks/docker-compose.e2e.yml`` so
the advertised WebAuthn RP ID matches the loopback origin.
"""

from __future__ import annotations

import re
import uuid

import pytest
from playwright.sync_api import Browser

from tests.e2e._helpers.auth import enroll_owner, install_virtual_authenticator


def test_first_boot_owner_lands_on_role_home(
    base_url: str,
    browser: Browser,
) -> None:
    """Enroll an owner with a real passkey ceremony and assert role home.

    Manager grant lands at ``/dashboard`` per ``LoginPage.tsx``'s
    ``ROLE_LANDING`` map; self-serve signup creates an owners-group
    member, and the v1 auth/me envelope surfaces owners as manager.
    """
    suffix = uuid.uuid4().hex[:8]
    email = f"e2e-pilot-first-boot-{suffix}@dev.local"
    workspace_slug = f"e2e-pilot-first-boot-{suffix}"

    if browser.browser_type.name != "chromium":
        pytest.skip("first-boot passkey ceremony uses Chromium CDP WebAuthn")

    context = browser.new_context(base_url=base_url)
    try:
        page = context.new_page()
        install_virtual_authenticator(context)
        enrollment = enroll_owner(
            page,
            base_url=base_url,
            email=email,
            workspace_slug=workspace_slug,
        )
        # Round-trip the auth gate via the JSON API first — the SPA's
        # RoleHome redirect happens client-side after the auth store
        # loads, so a direct goto('/') would blur cookie transport
        # failures into a generic "still on /login" symptom.
        me_response = page.request.get(f"{base_url.rstrip('/')}/api/v1/auth/me")
        assert me_response.status == 200, (
            f"/api/v1/auth/me returned {me_response.status}; "
            "passkey login cookie did not authenticate the request — "
            f"body={me_response.text()[:200]!r}"
        )
        payload = me_response.json()
        assert payload["user_id"] == enrollment.user_id
        assert payload["email"] == enrollment.email, (
            f"/api/v1/auth/me email mismatch: server={payload['email']!r}, "
            f"expected={enrollment.email!r}"
        )
        assert payload["available_workspaces"], (
            "no workspaces on the freshly-provisioned owner"
        )
        assert payload["available_workspaces"][0]["workspace"]["id"] == enrollment.slug

        # Now drive the SPA. RoleHome at "/" Navigate-redirects to
        # /dashboard for manager-surface users. The SPA keeps an SSE
        # connection open, so networkidle is the wrong readiness
        # signal here; wait for the client-side route.
        page.goto(f"{base_url.rstrip('/')}/")
        page.wait_for_url(re.compile(r".*/(dashboard|today)$"), timeout=15_000)
        assert "/dashboard" in page.url or "/today" in page.url, (
            f"expected role-home redirect but landed at {page.url}"
        )
    finally:
        context.close()
