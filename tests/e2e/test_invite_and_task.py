"""GA journey 2: invite + passkey enrolment + first task completion (cd-db0g).

Spec: ``docs/specs/17-testing-quality.md`` §"End-to-end" — journey 2
of the GA five.

The journey wires together every identity surface a fresh worker
hits before they can do real work:

1. **Owner provisions.** Dev-login mints an owner session; the test
   then drives ``POST /properties``, ``POST /properties/{id}/areas``,
   ``POST /work_roles``, ``POST /users/invite`` against the
   workspace-scoped API. The mocks SPA buttons for these flows are
   not wired (cd-bbjt and friends), so the test reaches the same
   endpoints the SPA's data layer would call once the create UIs
   land. The journey is still "via the SPA" in the sense that every
   provisioning call rides the same authenticated session a manager
   would carry.
2. **Mailpit interception.** The invite email lands in the in-stack
   Mailpit container; the test extracts the magic-link token via
   :func:`tests.e2e._helpers.auth.wait_for_magic_link` —
   :mod:`tests.integration.mail` already pins the JSON shape so the
   helper is a thin wrapper.
3. **Invitee passkey enrolment.** The browser parks on the SPA's
   ``/login`` origin so navigator.credentials runs against the same
   host the WebAuthn RP-ID binds to, then drives the bare-host
   invite passkey trio (``/api/v1/invite/accept`` →
   ``/api/v1/invite/passkey/start`` →
   ``navigator.credentials.create()`` against the Chromium WebAuthn
   virtual authenticator → ``/api/v1/invite/passkey/finish``).
   cd-9q6bb shipped the bridging ``/invite/passkey/{start,finish}``
   routes; cd-kd26 then folded the former ``/invite/complete``
   second leg into the finish callback so the SPA gets the
   ``workspace_id`` + redirect target back in one round trip
   without a second public endpoint. Spec §03 "Additional users
   (invite → click-to-accept)".
4. **Worker login.** A real passkey assertion via
   ``/api/v1/auth/passkey/login/{start,finish}`` mints the worker's
   session cookie; :func:`mirror_dev_cookie_alias` projects it onto
   the dev-only ``crewday_session`` alias so plain-HTTP loopback can
   ship it.
5. **Authenticated SPA navigation + task completion.** The worker
   navigates to ``/today``, the role home for an owner-less worker,
   and the WorkspaceGate auto-adopts their single workspace. With
   the protected tree mounted, the test posts to
   ``/tasks/<id>/complete`` through Playwright's ``page.request``
   client — same browser context as the SPA, same UA + Accept-
   Language fingerprint the passkey login stamped onto the session
   row, same ``crewday_csrf`` cookie the CSRF middleware enforces.
   The state goes ``pending → completed`` and the assertion reads
   back via ``GET /tasks/<id>``.

Chromium-only because the WebAuthn virtual authenticator is
exposed through Chrome DevTools Protocol; WebKit auto-skips with
the same focused message :func:`install_virtual_authenticator`
emits. Per-browser parametrisation lives at the pytest-playwright
level (``pytest --browser chromium --browser webkit``) — the test
function itself only needs to detect WebKit and skip.
"""

from __future__ import annotations

import secrets
from typing import Any, Final

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from app.auth.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.auth.session_cookie import DEV_SESSION_COOKIE_NAME
from tests.e2e._helpers.auth import (
    create_passkey_attestation,
    extract_magic_link_token,
    get_passkey_assertion,
    install_virtual_authenticator,
    login_with_dev_session,
    mirror_dev_cookie_alias,
    wait_for_magic_link,
)

JOURNEY_SLUG_PREFIX: Final[str] = "e2e-invite-and-task"


def test_owner_invites_worker_and_worker_completes_first_task(
    base_url: str,
    browser: Browser,
) -> None:
    """End-to-end GA journey 2 (cd-db0g) on the loopback e2e stack.

    Skips on non-Chromium browsers because the bare-host invite
    passkey ceremony goes through the CDP WebAuthn virtual
    authenticator. Spec §17 explicitly carves this out: "passkey
    ceremony coverage is Chromium-only until a WebKit virtual-
    authenticator driver exists in the suite".
    """
    if browser.browser_type.name != "chromium":
        pytest.skip("invite + passkey ceremony uses Chromium CDP WebAuthn")

    run_id = secrets.token_hex(3)
    workspace_slug = f"{JOURNEY_SLUG_PREFIX}-{run_id}"
    owner_email = f"{JOURNEY_SLUG_PREFIX}-owner-{run_id}@dev.local"
    invitee_email = f"{JOURNEY_SLUG_PREFIX}-worker-{run_id}@dev.local"
    invitee_display = "Maria E2E"

    # Each phase runs in its own browser context so the owner and
    # invitee never share a cookie jar — the worker's passkey login
    # must succeed against an empty session, not coast on the owner's.
    owner_ctx = browser.new_context(base_url=base_url)
    invitee_ctx = browser.new_context(base_url=base_url)
    try:
        # ----- Owner phase: provision and invite ---------------------
        owner_login = login_with_dev_session(
            owner_ctx,
            base_url=base_url,
            email=owner_email,
            workspace_slug=workspace_slug,
            role="owner",
        )
        owner_api = _SessionApi(
            base_url=base_url,
            workspace_slug=workspace_slug,
            session_cookie=owner_login.cookie_value,
        )
        me_envelope = owner_api.get("/api/v1/me")
        # Owner workspace id is what /users/invite expects in the
        # grant scope_id — the slug only resolves the tenancy context
        # at the URL prefix, the body still carries the ULID. ``/me``
        # carries ``current_workspace_id`` as a ULID; ``/auth/me``
        # surfaces the same data via ``available_workspaces[].workspace.id``
        # but with the URL slug substituted, which is the wrong shape
        # for the invite scope_id check.
        workspace_id = _expect_str(me_envelope, "current_workspace_id")

        property_id = _create_property(owner_api)
        area_id = _create_area(owner_api, property_id=property_id)
        work_role_id = _create_work_role(owner_api)
        invite = _invite_worker(
            owner_api,
            workspace_id=workspace_id,
            email=invitee_email,
            display_name=invitee_display,
        )

        # ----- Invitee phase: redeem invite + enrol passkey ----------
        invitee_page = invitee_ctx.new_page()
        # Install the virtual authenticator before any navigator.credentials
        # call — the CDP domain attaches per-page, and a deferred install
        # would race with the in-page navigator.credentials.create().
        install_virtual_authenticator(invitee_ctx)

        token = _read_invite_token(invitee_email)
        # Land on /login so navigator.credentials runs from the SPA
        # origin (the WebAuthn RP-ID matches the page host). The
        # invite-accept SPA path is /auth/magic/<token>, which the
        # SPA forwards to the invite ceremony — but the test drives
        # the underlying JSON endpoints directly, so any
        # authenticated-origin landing page works.
        invitee_page.goto(f"{base_url.rstrip('/')}/login")

        accept_body = _post_json(
            invitee_page,
            f"{base_url.rstrip('/')}/api/v1/invite/accept",
            {"token": token},
            expected_status=200,
        )
        assert accept_body["kind"] == "new_user", (
            f"expected new_user accept envelope, got {accept_body!r}"
        )
        invite_id = _expect_str(accept_body, "invite_id")
        assert invite_id == invite["invite_id"]
        invitee_user_id = _expect_str(accept_body, "user_id")
        assert invite["user_id"] == invitee_user_id

        passkey_start = _post_json(
            invitee_page,
            f"{base_url.rstrip('/')}/api/v1/invite/passkey/start",
            {"invite_id": invite_id},
            expected_status=200,
        )
        challenge_id = _expect_str(passkey_start, "challenge_id")
        attestation = create_passkey_attestation(invitee_page, passkey_start["options"])
        finish_body = _post_json(
            invitee_page,
            f"{base_url.rstrip('/')}/api/v1/invite/passkey/finish",
            {
                "invite_id": invite_id,
                "challenge_id": challenge_id,
                "credential": attestation,
            },
            expected_status=200,
        )
        # cd-kd26: ``/invite/passkey/finish`` now atomically lands the
        # credential AND activates the pending grants, so the response
        # body carries the redirect target directly — no second
        # ``/invite/complete`` call required.
        assert finish_body["user_id"] == invitee_user_id
        assert finish_body["workspace_id"] == workspace_id
        assert finish_body["redirect"].endswith("/today")

        # ----- Worker phase: passkey login then task completion ------
        login_start = _post_json(
            invitee_page,
            f"{base_url.rstrip('/')}/api/v1/auth/passkey/login/start",
            {},
            expected_status=200,
        )
        assertion = get_passkey_assertion(invitee_page, login_start["options"])
        login_resp = invitee_page.request.post(
            f"{base_url.rstrip('/')}/api/v1/auth/passkey/login/finish",
            data={
                "challenge_id": _expect_str(login_start, "challenge_id"),
                "credential": assertion,
            },
        )
        assert login_resp.status == 200, (
            f"passkey login finish failed: {login_resp.status} "
            f"body={login_resp.text()[:300]!r}"
        )
        login_body = login_resp.json()
        assert isinstance(login_body, dict)
        assert login_body["user_id"] == invitee_user_id
        # Project the __Host- session onto the dev-only alias so the
        # SPA can ship it on plain HTTP — same dance enroll_owner uses.
        mirror_dev_cookie_alias(
            invitee_page,
            base_url=base_url,
            set_cookie=login_resp.headers.get("set-cookie"),
        )

        # Owner now creates the worker's first task. We assign
        # explicitly via /assign rather than relying on auto-pool
        # walking + work_role linkage — the override path keeps the
        # test's surface narrow on the assignment-algorithm side.
        scheduled_for = _scheduled_iso_local()
        # The tasks router is mounted under the ``tasks`` context so
        # the create endpoint sits at ``/w/<slug>/api/v1/tasks/tasks``
        # (the inner ``/tasks`` is the route, the outer is the
        # context-name prefix from CONTEXT_ROUTERS).
        task = owner_api.post(
            f"/w/{workspace_slug}/api/v1/tasks/tasks",
            {
                "title": f"GA journey 2 first task {run_id}",
                "property_id": property_id,
                "area_id": area_id,
                "expected_role_id": work_role_id,
                "scheduled_for_local": scheduled_for,
                "duration_minutes": 30,
                "photo_evidence": "disabled",
                "is_personal": False,
            },
        )
        task_id = _expect_str(task, "id")
        assigned = owner_api.post(
            f"/w/{workspace_slug}/api/v1/tasks/tasks/{task_id}/assign",
            {"assignee_user_id": invitee_user_id},
        )
        assert assigned["assigned_user_id"] == invitee_user_id

        # The worker now navigates the SPA and lands on a workspace-
        # scoped page. ``/today`` is the worker's role home (App.tsx
        # ``RoleHome`` component) and exercises the full chain we
        # care about: passkey-issued cookie reaches FastAPI →
        # /auth/me probe succeeds → WorkspaceGate auto-adopts the
        # worker's only workspace → the protected tree mounts. The
        # production TaskDetailPage at ``/task/:tid`` consumes a
        # double-prefix path (``/api/v1/tasks/<id>/detail`` → SPA
        # rewrites to ``/w/<slug>/api/v1/tasks/<id>/detail`` while
        # the backend serves ``/w/<slug>/api/v1/tasks/tasks/<id>/detail``);
        # tracking that frontend route gap is out of scope here — see
        # follow-up Beads. We assert the worker's authenticated
        # arrival and complete the task through the same JSON API
        # the SPA's "Mark done" button targets.
        invitee_page.goto(
            f"{base_url.rstrip('/')}/today",
            wait_until="domcontentloaded",
        )
        # Wait until the workspace-gated tree commits — the heading
        # only renders after WorkspaceGate auto-adopts the worker's
        # single workspace. Without this gate the next API call
        # could race against an unprovisioned cookie jar.
        expect(invitee_page).to_have_url(
            f"{base_url.rstrip('/')}/today", timeout=15_000
        )
        # The SPA mounts after auth bootstraps; the "Today" heading
        # is the most stable readiness anchor that doesn't depend
        # on task data (which is gated on the route-mismatch above).
        expect(invitee_page.get_by_role("heading", name="Today")).to_be_visible(
            timeout=15_000
        )

        # Drive completion through the worker's freshly-minted
        # session — same opaque session value the SPA's
        # ``credentials: "same-origin"`` fetch would carry. We use
        # Playwright's request fixture rather than httpx because
        # the passkey login stamped a fingerprint on the session row
        # (UA + Accept-Language hash, see app/auth/session.py): an
        # httpx call with its own UA would mismatch the stored
        # fingerprint and 401 with ``session_invalid``. Routing
        # through the same browser context preserves both headers.
        worker_me_resp = invitee_page.request.get(f"{base_url.rstrip('/')}/api/v1/me")
        assert worker_me_resp.status == 200, (
            f"worker /me probe failed: {worker_me_resp.status} "
            f"body={worker_me_resp.text()[:300]!r}"
        )
        worker_me = worker_me_resp.json()
        assert worker_me["user_id"] == invitee_user_id, (
            f"worker /me user_id mismatch: got {worker_me!r}"
        )
        assert worker_me["current_workspace_id"] == workspace_id, (
            f"worker /me workspace mismatch: got {worker_me!r}, "
            f"expected workspace_id={workspace_id!r}"
        )
        # The CSRF middleware (app/auth/csrf.py) refuses non-GET
        # requests that lack a matching ``X-CSRF`` header. The SPA
        # reads ``crewday_csrf`` from its cookie jar and echoes it
        # on every mutating fetch; we mirror that exact dance here.
        csrf_token = _read_csrf_cookie(invitee_page)
        complete_resp_obj = invitee_page.request.post(
            f"{base_url.rstrip('/')}/w/{workspace_slug}"
            f"/api/v1/tasks/tasks/{task_id}/complete",
            data={"note_md": "Done via GA journey 2 e2e"},
            headers={CSRF_HEADER_NAME: csrf_token},
        )
        assert complete_resp_obj.status == 200, (
            f"complete failed: {complete_resp_obj.status} "
            f"body={complete_resp_obj.text()[:300]!r}"
        )
        complete_resp = complete_resp_obj.json()
        assert complete_resp["task_id"] == task_id
        assert complete_resp["state"] == "completed"

        # Verify the row reads back at the steady-state — same
        # endpoint the SPA's task-detail GET would hit if the
        # frontend route gap were fixed (see comment above on
        # ``/today`` navigation).
        completed_row = _poll_task_state_via_page(
            invitee_page,
            base_url=base_url,
            workspace_slug=workspace_slug,
            task_id=task_id,
            target_state="completed",
        )
        assert completed_row["state"] == "completed"
        # The TaskPayload projection does not surface
        # ``completed_by_user_id``; the assignee is the closest
        # signal that the worker drove the transition.
        assert completed_row["assigned_user_id"] == invitee_user_id
    finally:
        owner_ctx.close()
        invitee_ctx.close()


# ---------------------------------------------------------------------------
# HTTP helpers (workspace-scoped owner / worker JSON client)
# ---------------------------------------------------------------------------


class _SessionApi:
    """Thin httpx wrapper that pins the session + CSRF cookies.

    Mirrors the same-named class in
    :mod:`tests.e2e.test_agent_task_lifecycle` — both tests run a
    handful of mutating calls against the workspace-scoped surface.
    Keeping the class shape identical means a future refactor that
    promotes the helper into ``_helpers/`` lands as a single rename.
    """

    def __init__(
        self, *, base_url: str, workspace_slug: str, session_cookie: str
    ) -> None:
        csrf = secrets.token_urlsafe(24)
        self._client = httpx.Client(
            base_url=base_url,
            cookies={
                DEV_SESSION_COOKIE_NAME: session_cookie,
                CSRF_COOKIE_NAME: csrf,
            },
            headers={CSRF_HEADER_NAME: csrf},
            timeout=15.0,
            follow_redirects=False,
        )
        self.workspace_slug = workspace_slug

    def get(self, path: str) -> dict[str, Any]:
        resp = self._client.get(path)
        _raise_api_error(resp, method="GET", path=path)
        body = resp.json()
        if not isinstance(body, dict):
            raise AssertionError(f"GET {path} returned non-object JSON: {body!r}")
        return body

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post(path, json=payload)
        _raise_api_error(resp, method="POST", path=path)
        body = resp.json()
        if not isinstance(body, dict):
            raise AssertionError(f"POST {path} returned non-object JSON: {body!r}")
        return body


def _create_property(api: _SessionApi) -> str:
    """Create a residence property and return its id."""
    created = api.post(
        f"/w/{api.workspace_slug}/api/v1/properties",
        {
            "name": f"GA Journey Villa {secrets.token_hex(3)}",
            "kind": "residence",
            "address": "1 Loopback Lane",
            "country": "US",
            "locale": "en-US",
            "default_currency": "USD",
            "timezone": "UTC",
            "tags_json": [],
            "welcome_defaults_json": {},
            "property_notes_md": "",
        },
    )
    return _expect_str(created, "id")


def _create_area(api: _SessionApi, *, property_id: str) -> str:
    """Create one indoor-room area on the property."""
    created = api.post(
        f"/w/{api.workspace_slug}/api/v1/properties/{property_id}/areas",
        {
            "name": "Kitchen",
            "kind": "indoor_room",
        },
    )
    return _expect_str(created, "id")


def _create_work_role(api: _SessionApi) -> str:
    """Create the cleaner work role."""
    created = api.post(
        f"/w/{api.workspace_slug}/api/v1/work_roles",
        {
            "key": f"ga-journey-{secrets.token_hex(3)}",
            "name": "GA Journey Cleaner",
            "description_md": "",
            "default_settings_json": {},
            "icon_name": "clipboard-check",
        },
    )
    return _expect_str(created, "id")


def _invite_worker(
    api: _SessionApi,
    *,
    workspace_id: str,
    email: str,
    display_name: str,
) -> dict[str, Any]:
    """Send the workspace invite email; return the invite envelope."""
    return api.post(
        f"/w/{api.workspace_slug}/api/v1/users/invite",
        {
            "email": email,
            "display_name": display_name,
            "grants": [
                {
                    "scope_kind": "workspace",
                    "scope_id": workspace_id,
                    "grant_role": "worker",
                }
            ],
        },
    )


# ---------------------------------------------------------------------------
# Mailpit + page helpers
# ---------------------------------------------------------------------------


def _read_invite_token(recipient: str) -> str:
    """Pull the invite magic-link token from Mailpit.

    Reuses :func:`wait_for_magic_link` + :func:`extract_magic_link_token`
    so the polling + regex stay aligned with every other magic-link
    flow. The invite template emits the standard
    ``CREWDAY_PUBLIC_URL/auth/magic/<token>`` URL: the SPA's
    ``/auth/magic`` handler recognises the ``grant_invite`` purpose
    and forwards to the invite-accept ceremony, but our test bypasses
    the SPA and posts the token straight to ``/api/v1/invite/accept``.

    We filter on the unique "invited" substring so an unrelated
    recovery / signup email cannot match the same recipient inbox.
    """
    msg = wait_for_magic_link(
        recipient=recipient,
        subject_substring="invited",
    )
    return extract_magic_link_token(msg)


def _post_json(
    page: Page,
    url: str,
    body: dict[str, Any],
    *,
    expected_status: int,
) -> dict[str, Any]:
    """Run a JSON POST through Playwright's request fixture.

    Mirrors the private helper inside
    :mod:`tests.e2e._helpers.auth` so this test reads the same shape
    as :func:`enroll_owner` without reaching into a private symbol.
    """
    response = page.request.post(url, data=body)
    if response.status != expected_status:
        raise AssertionError(
            f"{url} returned {response.status}, expected {expected_status}; "
            f"body={response.text()[:500]!r}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise AssertionError(f"{url} returned non-object JSON: {payload!r}")
    return payload


def _read_csrf_cookie(page: Page) -> str:
    """Return the ``crewday_csrf`` cookie value from the page jar.

    The CSRF middleware sets this cookie on every response; the SPA
    echoes it back as the ``X-CSRF`` header on mutating requests.
    The test mirrors the exact contract — see :mod:`app.auth.csrf`
    for the double-submit rationale.
    """
    cookies = page.context.cookies()
    for cookie in cookies:
        if cookie["name"] == CSRF_COOKIE_NAME:
            value = cookie["value"]
            if isinstance(value, str) and value:
                return value
    raise AssertionError(
        f"no {CSRF_COOKIE_NAME!r} cookie present in jar; cookies={cookies!r}"
    )


def _poll_task_state_via_page(
    page: Page,
    *,
    base_url: str,
    workspace_slug: str,
    task_id: str,
    target_state: str,
    deadline_seconds: float = 10.0,
) -> dict[str, Any]:
    """Poll ``GET /tasks/{id}`` through the worker's browser context.

    Routes the GET through Playwright's ``page.request`` so the
    session-fingerprint pin (UA + Accept-Language) holds — see the
    ``invitee_page.request.post`` rationale at the call site.
    """
    import time

    deadline = time.monotonic() + deadline_seconds
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        resp = page.request.get(
            f"{base_url.rstrip('/')}/w/{workspace_slug}/api/v1/tasks/tasks/{task_id}"
        )
        if resp.status == 200:
            payload = resp.json()
            if isinstance(payload, dict):
                last = payload
                if payload.get("state") == target_state:
                    return payload
        time.sleep(0.25)
    raise AssertionError(
        f"task {task_id!r} did not reach state {target_state!r}; last={last!r}"
    )


def _scheduled_iso_local() -> str:
    """Return a property-local ISO-8601 timestamp two hours from now.

    The property created above lives in ``UTC`` so naive local time
    equals UTC. Two hours of slack avoids the ad-hoc creator's
    "scheduled in the past" guard while staying well inside today's
    operational window.
    """
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")


def _expect_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AssertionError(f"expected non-empty string {key!r} in {payload!r}")
    return value


def _raise_api_error(resp: httpx.Response, *, method: str, path: str) -> None:
    if resp.is_success:
        return
    raise AssertionError(
        f"{method} {path} failed with {resp.status_code}\nbody:\n{resp.text}"
    )
