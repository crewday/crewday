"""SPA e2e for self-serve signup through passkey enrollment."""

from __future__ import annotations

import json
import re
import uuid

import pytest
from playwright.sync_api import Browser, Route
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.auth.session_cookie import DEV_SESSION_COOKIE_NAME, SESSION_COOKIE_NAME
from tests.e2e._helpers.auth import (
    extract_magic_link_token,
    install_virtual_authenticator,
    wait_for_magic_link,
)


def test_signup_spa_enrolls_owner_and_lands_on_dashboard(
    base_url: str,
    browser: Browser,
) -> None:
    """Drive /signup -> /signup/verify -> /signup/enroll in the SPA."""
    if browser.browser_type.name != "chromium":
        pytest.skip("signup SPA passkey ceremony uses Chromium CDP WebAuthn")

    suffix = uuid.uuid4().hex[:8]
    email = f"e2e-spa-signup-{suffix}@dev.local"
    workspace_slug = f"e2e-spa-signup-{suffix}"

    context = browser.new_context(base_url=base_url)
    try:
        page = context.new_page()
        install_virtual_authenticator(context)
        page.route("**/api/v1/signup/start", _inject_test_captcha)

        page.goto(f"{base_url.rstrip('/')}/signup")
        page.get_by_test_id("signup-email").fill(email)
        page.get_by_test_id("signup-slug").fill(workspace_slug)
        page.get_by_test_id("signup-submit").click()
        page.get_by_test_id("signup-sent").wait_for(timeout=15_000)

        msg = wait_for_magic_link(
            recipient=email,
            subject_substring="verify your email",
        )
        token = extract_magic_link_token(msg)
        page.goto(f"{base_url.rstrip('/')}/signup/verify?token={token}")
        page.wait_for_url("**/signup/enroll", timeout=15_000)
        page.get_by_test_id("signup-enroll-name").fill("E2E SPA Owner")
        page.get_by_test_id("signup-enroll-submit").click()
        try:
            # Self-serve owners are manager-role users; employees land on /today.
            page.wait_for_url(re.compile(r".*/dashboard$"), timeout=20_000)
        except PlaywrightTimeoutError as exc:
            error = page.get_by_test_id("signup-enroll-error")
            error_text = error.text_content(timeout=1_000) if error.count() else None
            raise AssertionError(
                "signup SPA did not land on /dashboard after passkey login; "
                f"url={page.url!r} error={error_text!r}"
            ) from exc

        cookies = context.cookies(base_url)
        cookie_names = {cookie["name"] for cookie in cookies}
        assert (
            SESSION_COOKIE_NAME in cookie_names
            or DEV_SESSION_COOKIE_NAME in cookie_names
        ), f"passkey login did not set a session cookie; cookies={cookie_names!r}"

        me_response = page.request.get(f"{base_url.rstrip('/')}/api/v1/auth/me")
        assert me_response.status == 200, (
            f"/api/v1/auth/me returned {me_response.status}; "
            f"body={me_response.text()[:300]!r}"
        )
        me = me_response.json()
        assert isinstance(me, dict)
        assert me["email"] == email
        assert me["available_workspaces"][0]["workspace"]["id"] == workspace_slug
        assert page.url.endswith("/dashboard")
    finally:
        context.close()


def _inject_test_captcha(route: Route) -> None:
    """Add the e2e-only captcha token while keeping the SPA form real."""
    body = json.loads(route.request.post_data or "{}")
    if not isinstance(body, dict):
        body = {}
    body.setdefault("captcha_token", "test-pass")
    route.continue_(
        post_data=json.dumps(body),
        headers={
            **route.request.headers,
            "content-type": "application/json",
        },
    )
