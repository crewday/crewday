"""GA journey 3: iCal feed → turnover task → photo evidence → guest welcome (cd-zxvk).

Spec: ``docs/specs/17-testing-quality.md`` §"End-to-end" — journey 3
of the GA five.

The journey wires the full reservation lifecycle:

1. **Manager (dev_login fast path) imports an iCal feed.** The test
   spins up an HTTPS server **inside the app-api container** (via
   ``docker exec``) so the validator reaches it on loopback,
   generates a self-signed cert with ``openssl``, and serves a
   synthetic VCALENDAR body containing two consecutive reservations.
   The workspace setting ``ical.allow_self_signed`` is flipped on
   for the new workspace so the validator's TLS posture accepts the
   self-signed cert. The compose-level
   ``CREWDAY_ICAL_ALLOW_PRIVATE_ADDRESSES`` knob (cd-xr652) opens
   the SSRF private-address gate so the loopback resolution passes —
   both gates default closed in production.
2. **Stays import yields turnover Tasks.** The manager hits
   ``POST /ical-feeds`` to register, then ``POST /ical-feeds/{id}/poll-once``
   (cd-jk6is) to drive a single ingest tick. The route runs the
   live ``ReservationUpserted`` subscribers in the request UoW so
   the ``StayBundle`` + ``Occurrence`` (turnover task) rows land
   transactionally with the upsert. The test asserts the rows
   landed via the workspace-scoped reservation / stay-bundle list
   endpoints, then navigates to the manager Stays page to confirm
   the SPA renders against the ingested data (UI assertion preferred
   per spec §End-to-end).
3. **Worker completes the turnover with photo evidence.** A
   separate worker dev_login session opens the assigned task,
   uploads a tiny PNG fixture via ``page.set_input_files()`` against
   ``POST /tasks/{id}/evidence`` (multipart), then drives
   ``/tasks/{id}/complete`` with the resulting evidence id.
4. **Guest opens the welcome link.** The owner mints a guest link
   for the just-checked-in stay; an unauthenticated browser context
   loads the SPA's ``/w/<slug>/guest/<token>`` route and asserts the
   non-empty welcome page renders (eyebrow + property heading).

The test logic itself is browser-agnostic — no passkey ceremony is
involved, so the WebKit virtual-authenticator carve-out (§17
"End-to-end") doesn't apply. Whether the WebKit leg actually runs
depends on the host: pytest-playwright's ``browser`` fixture errors
at launch time on hosts missing libicu74 (AGENTS.md §"End-to-end
Playwright suite"), independent of this test.
"""

from __future__ import annotations

import datetime as _dt
import secrets
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Final

import httpx
from playwright.sync_api import Browser, expect

from app.auth.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.auth.session_cookie import DEV_SESSION_COOKIE_NAME
from tests.e2e._helpers.auth import login_with_dev_session

JOURNEY_SLUG_PREFIX: Final[str] = "e2e-turnover-welcome"

# The ICS feed server runs **inside the app-api container** so the
# validator can reach it on loopback (``https://127.0.0.1:<port>``).
# Two reasons we don't bind on the host:
#
# 1. Production-host ufw policies routinely DROP incoming traffic
#    from the docker bridge interface to the host; binding on the
#    bridge gateway IP loses traffic on those hosts even though the
#    interface is administratively up.
# 2. The compose stack already grants ``CREWDAY_ICAL_ALLOW_PRIVATE_ADDRESSES=1``
#    in the e2e override (cd-xr652). Loopback is a private address
#    so the SSRF gate clears, and the validator's
#    ``ical.allow_self_signed=true`` workspace setting (cd-t2qtg)
#    accepts the freshly-minted self-signed cert. Together those two
#    knobs are the documented dev / e2e carve-out — production keeps
#    both closed.
#
# ``crewday-app-api`` is the compose container name pinned in
# ``mocks/docker-compose.yml`` — stable across compose project-name
# overrides because the file declares ``container_name`` explicitly.
_APP_API_CONTAINER: Final[str] = "crewday-app-api"

# Path to the tiny PNG fixture (1x1, < 200 bytes) used as photo
# evidence. The bytes are below every per-kind cap so the
# multipart parser + filetype-sniff pipeline both clear immediately.
_PHOTO_FIXTURE_PATH: Final[Path] = (
    Path(__file__).parent / "_fixtures" / "evidence_thumb.png"
)


def test_ical_to_turnover_to_welcome(
    base_url: str,
    browser: Browser,
) -> None:
    """End-to-end GA journey 3 (cd-zxvk).

    See module docstring for the four-phase walkthrough. The test
    runs on both Chromium and WebKit (use
    ``--browser chromium --browser webkit`` for both legs) because no
    passkey ceremony is exercised — the dev_login fast path mints
    both manager and worker sessions.
    """
    # AGENTS.md §"End-to-end Playwright suite" notes that the WebKit
    # driver requires libicu74 — pytest-playwright's ``browser``
    # fixture errors at launch time on hosts that only ship libicu70,
    # before this body runs. There is no in-test mitigation; CI and
    # local invocations targeting WebKit must install the dep first.
    # The Chromium leg always runs the full journey.
    run_id = secrets.token_hex(3)
    workspace_slug = f"{JOURNEY_SLUG_PREFIX}-{run_id}"
    owner_email = f"{JOURNEY_SLUG_PREFIX}-owner-{run_id}@dev.local"
    worker_email = f"{JOURNEY_SLUG_PREFIX}-worker-{run_id}@dev.local"

    owner_ctx = browser.new_context(base_url=base_url)
    worker_ctx = browser.new_context(base_url=base_url)
    guest_ctx = browser.new_context(base_url=base_url)
    try:
        # ---- Owner phase: provision + ingest ----------------------------
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
        owner_me = owner_api.get("/api/v1/me")
        owner_user_id = _expect_str(owner_me, "user_id")
        del owner_user_id  # consumed for assertion symmetry; not reused

        # Flip the per-workspace ``ical.allow_self_signed`` switch
        # so the test ICS server's self-signed cert clears the
        # validator's TLS posture (cd-t2qtg). Production never opts
        # in by default; the e2e knob is the only legitimate flip.
        owner_api.patch(
            f"/w/{workspace_slug}/api/v1/settings",
            {"ical.allow_self_signed": True},
        )

        # Provision a property + work_role; the manager-issued
        # invite (skipped here for speed) is replaced with a
        # second dev_login that drops the worker into the same
        # workspace. ``dev_login`` is idempotent on
        # ``(workspace_slug, email)`` so the worker call below
        # joins the existing workspace rather than creating a
        # second.
        property_id = _create_property(owner_api, run_id=run_id)
        work_role_id = _create_work_role(owner_api, run_id=run_id)
        del work_role_id  # available for follow-up but not consumed

        # ---- Worker phase: dev_login (workspace already exists) ---------
        worker_login = login_with_dev_session(
            worker_ctx,
            base_url=base_url,
            email=worker_email,
            workspace_slug=workspace_slug,
            role="worker",
        )
        worker_api = _SessionApi(
            base_url=base_url,
            workspace_slug=workspace_slug,
            session_cookie=worker_login.cookie_value,
        )
        worker_me = worker_api.get("/api/v1/me")
        worker_user_id = _expect_str(worker_me, "user_id")

        # ---- ICS server bring-up + feed registration --------------------
        # Two consecutive reservations: the first stay's checkout
        # gives the bundle generator a "next stay" so the
        # ``after_checkout`` rule produces a turnover Occurrence.
        # Anchor in the future so the SPA's "today" filter doesn't
        # surface stale data, but inside the worker's ad-hoc viewing
        # window so the assignment lands on a current row.
        anchor = _dt.datetime.now(_dt.UTC).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        first_check_in = anchor + _dt.timedelta(days=1)
        first_check_out = anchor + _dt.timedelta(days=4)
        next_check_in = anchor + _dt.timedelta(days=5)
        next_check_out = anchor + _dt.timedelta(days=8)
        ics_body = _build_two_stay_ics(
            first_check_in=first_check_in,
            first_check_out=first_check_out,
            second_check_in=next_check_in,
            second_check_out=next_check_out,
            uid_prefix=f"e2e-{run_id}",
        )

        with _serve_ics_https(ics_body) as feed_url:
            feed = owner_api.post(
                f"/w/{workspace_slug}/api/v1/stays/ical-feeds",
                {"property_id": property_id, "url": feed_url},
            )
            feed_id = _expect_str(feed, "id")

            # Drive a single manual ingest tick. The route blocks
            # synchronously while the subscribers materialise the
            # StayBundle + Occurrence rows in the request UoW —
            # the response is read after they are durable.
            poll = owner_api.post(
                f"/w/{workspace_slug}/api/v1/stays/ical-feeds/{feed_id}/poll-once",
                {},
            )

        if poll.get("status") != "polled" or poll.get("reservations_created", 0) < 2:
            raise AssertionError(
                f"poll-once did not ingest both reservations; payload={poll!r}"
            )

        # Verify reservations + bundle landed via the workspace API
        # surface (no DB peeking — the SPA reads the same path).
        reservations = owner_api.get(f"/w/{workspace_slug}/api/v1/stays/reservations")[
            "data"
        ]
        if len(reservations) < 2:
            raise AssertionError(
                f"expected 2 reservations after ingest, got {len(reservations)}: "
                f"{reservations!r}"
            )
        first_reservation = _find_reservation(reservations, check_in=first_check_in)
        first_stay_id = _expect_str(first_reservation, "id")

        bundles = owner_api.get(
            f"/w/{workspace_slug}/api/v1/stays/stay-bundles"
            f"?reservation_id={first_stay_id}"
        )["data"]
        if not bundles:
            raise AssertionError(
                f"expected at least one stay bundle for reservation "
                f"{first_stay_id!r}; got {bundles!r}"
            )
        # Pull the first turnover task occurrence id from the bundle.
        bundle = bundles[0]
        bundle_tasks = bundle.get("tasks") or []
        if not isinstance(bundle_tasks, list) or not bundle_tasks:
            raise AssertionError(f"stay bundle has no task entries: bundle={bundle!r}")
        first_entry = bundle_tasks[0]
        if not isinstance(first_entry, dict):
            raise AssertionError(f"unexpected bundle task entry shape: {first_entry!r}")
        turnover_task_id = first_entry.get("occurrence_id")
        if not isinstance(turnover_task_id, str) or not turnover_task_id:
            raise AssertionError(
                f"bundle task entry missing occurrence_id: {first_entry!r}"
            )

        # Manager UI assertion — load the Stays page and confirm
        # the SPA renders the workspace shell. The Stays surface is
        # the operator's calendar of imported reservations + turnover
        # swatches (``app/web/src/pages/manager/StaysPage.tsx``).
        # The route is ``/stays`` (workspace-bare) — the SPA's
        # WorkspaceGate auto-adopts the owner's workspace before
        # the protected route mounts. Landing on it proves the
        # cookie alias + workspace gate clear after the ingest
        # round-trip.
        owner_page = owner_ctx.new_page()
        owner_page.goto(
            f"{base_url.rstrip('/')}/stays",
            wait_until="domcontentloaded",
        )
        expect(owner_page.get_by_role("heading", name="Stays").first).to_be_visible(
            timeout=15_000
        )
        owner_page.close()

        # The auto-generated turnover Occurrence carries
        # ``photo_evidence='disabled'`` (cd-1ai default — the
        # ``occurrence`` row inherits the worker-friendly setting
        # from the lifecycle rule rather than the §06 template
        # column, which the slice doesn't yet read). Flip it to
        # ``optional`` via the manager-side PATCH so the worker's
        # completion can carry a photo evidence id without
        # tripping the ``forbid`` policy gate
        # (:func:`app.domain.tasks.evidence._default_policy`).
        owner_api.patch(
            f"/w/{workspace_slug}/api/v1/tasks/tasks/{turnover_task_id}",
            {"photo_evidence": "optional"},
        )

        # Assign the turnover task to the worker so they have
        # permission to complete it (workers can only complete
        # tasks they're assigned to — see
        # :func:`app.domain.tasks.completion._is_assignee`).
        assigned = owner_api.post(
            f"/w/{workspace_slug}/api/v1/tasks/tasks/{turnover_task_id}/assign",
            {"assignee_user_id": worker_user_id},
        )
        if assigned.get("assigned_user_id") != worker_user_id:
            raise AssertionError(f"task assign did not pin worker: {assigned!r}")

        # ---- Worker phase: photo evidence + complete --------------------
        worker_page = worker_ctx.new_page()
        worker_page.goto(
            f"{base_url.rstrip('/')}/today",
            wait_until="domcontentloaded",
        )
        expect(worker_page).to_have_url(f"{base_url.rstrip('/')}/today", timeout=15_000)
        # The "Today" heading is the worker shell's stable readiness
        # anchor — present after WorkspaceGate auto-adopts the
        # worker's single workspace and the protected tree mounts.
        expect(worker_page.get_by_role("heading", name="Today").first).to_be_visible(
            timeout=15_000
        )

        # Upload photo evidence via the multipart endpoint. Routing
        # through Playwright's APIRequestContext (instead of httpx)
        # keeps the worker's session-fingerprint pin (UA +
        # Accept-Language) intact — see the rationale in
        # ``test_invite_and_task.py`` for why fingerprints matter.
        # The CSRF cookie is re-read **before each** mutating call
        # because :class:`app.auth.csrf.CSRFMiddleware` re-mints the
        # cookie on every response — the previous value is stale by
        # the time the next request lines up.
        evidence_resp = worker_page.request.post(
            f"{base_url.rstrip('/')}/w/{workspace_slug}"
            f"/api/v1/tasks/tasks/{turnover_task_id}/evidence",
            multipart={
                "kind": "photo",
                "file": {
                    "name": _PHOTO_FIXTURE_PATH.name,
                    "mimeType": "image/png",
                    "buffer": _PHOTO_FIXTURE_PATH.read_bytes(),
                },
            },
            headers={CSRF_HEADER_NAME: _read_csrf_cookie(worker_page)},
        )
        if evidence_resp.status != 201:
            raise AssertionError(
                f"evidence upload failed: {evidence_resp.status} "
                f"body={evidence_resp.text()[:300]!r}"
            )
        evidence_body = evidence_resp.json()
        if not isinstance(evidence_body, dict):
            raise AssertionError(
                f"evidence upload returned non-object: {evidence_body!r}"
            )
        evidence_id = _expect_str(evidence_body, "id")

        # Complete the task carrying the freshly-uploaded evidence
        # id. The completion service re-validates the policy
        # ("required" / "optional" / "disabled") against the row;
        # an "optional" row accepts the photo and clears.
        complete_resp = worker_page.request.post(
            f"{base_url.rstrip('/')}/w/{workspace_slug}"
            f"/api/v1/tasks/tasks/{turnover_task_id}/complete",
            data={
                "note_md": "GA journey 3 turnover complete",
                "photo_evidence_ids": [evidence_id],
            },
            headers={CSRF_HEADER_NAME: _read_csrf_cookie(worker_page)},
        )
        if complete_resp.status != 200:
            raise AssertionError(
                f"task complete failed: {complete_resp.status} "
                f"body={complete_resp.text()[:300]!r}"
            )
        complete_body = complete_resp.json()
        if not isinstance(complete_body, dict):
            raise AssertionError(
                f"task complete returned non-object: {complete_body!r}"
            )
        if complete_body.get("state") != "completed":
            raise AssertionError(
                f"task did not reach completed state: {complete_body!r}"
            )
        worker_page.close()

        # ---- Guest phase: welcome link ----------------------------------
        link = owner_api.post(
            f"/w/{workspace_slug}/api/v1/stays/stays/{first_stay_id}/welcome-link",
            {},
        )
        token = _expect_str(link, "token")

        # Sanity-probe the public welcome JSON before navigating: a
        # 410 here means the link was minted but the resolver rejects
        # it (clock skew, signing-key mismatch, …) and the SPA would
        # only render the "no longer valid" copy. Surface the cause
        # immediately so the test failure points at the API, not the
        # browser.
        with httpx.Client(timeout=10.0) as probe:
            welcome_probe = probe.get(
                f"{base_url.rstrip('/')}/api/v1/stays/welcome/{token}"
            )
        if welcome_probe.status_code != 200:
            raise AssertionError(
                f"public welcome probe failed: status={welcome_probe.status_code} "
                f"body={welcome_probe.text[:300]!r}"
            )

        guest_page = guest_ctx.new_page()
        # Use the token-bearing path the SPA renders for guests —
        # the "/w/<slug>/guest/<token>" surface is anonymous,
        # backed by the public ``/api/v1/stays/welcome/<token>``
        # JSON read.
        guest_page.goto(
            f"{base_url.rstrip('/')}/w/{workspace_slug}/guest/{token}",
            wait_until="domcontentloaded",
        )
        # The welcome page renders a `.guest__hero` header with
        # an eyebrow ("Welcome to") and the property name as
        # ``h1.guest__name``. Both must be present and non-empty
        # for the page to be considered "rendered". Wait on the
        # eyebrow text directly since it's stable copy in the
        # template, independent of property naming.
        try:
            expect(guest_page.get_by_text("Welcome to").first).to_be_visible(
                timeout=15_000
            )
        except AssertionError:
            content = guest_page.content()
            raise AssertionError(
                f"guest welcome did not render at "
                f"{guest_page.url!r}; html[:500]={content[:500]!r}"
            ) from None
        heading = guest_page.locator(".guest__name").first
        expect(heading).to_be_visible(timeout=5_000)
        if (heading.inner_text() or "").strip() == "":
            raise AssertionError("welcome heading rendered empty")
        guest_page.close()
    finally:
        owner_ctx.close()
        worker_ctx.close()
        guest_ctx.close()


# ---------------------------------------------------------------------------
# HTTP / API helpers
# ---------------------------------------------------------------------------


class _SessionApi:
    """Thin httpx wrapper that pins the session + CSRF cookies.

    Mirrors ``_SessionApi`` in :mod:`tests.e2e.test_invite_and_task` —
    keeping the shape identical means a future promotion to
    ``_helpers/`` is a single rename.
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
            timeout=30.0,
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

    def patch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.patch(path, json=payload)
        _raise_api_error(resp, method="PATCH", path=path)
        body = resp.json()
        if not isinstance(body, dict):
            raise AssertionError(f"PATCH {path} returned non-object JSON: {body!r}")
        return body


def _create_property(api: _SessionApi, *, run_id: str) -> str:
    created = api.post(
        f"/w/{api.workspace_slug}/api/v1/properties",
        {
            "name": f"Journey 3 Villa {run_id}",
            "kind": "residence",
            "address": "1 Loopback Lane",
            "country": "US",
            "locale": "en-US",
            "default_currency": "USD",
            "timezone": "UTC",
            "tags_json": [],
            # Seeding a non-empty welcome bundle so the guest page
            # has something to render. The structure mirrors the
            # SPA's keys — ``wifi``, ``access`` — so the welcome
            # JSON read renders something on the guest surface
            # beyond the eyebrow / heading.
            "welcome_defaults_json": {
                "wifi": {"ssid": "JourneyNet", "password": "welcome-3"},
                "access": {"door_code": "1234"},
                "house_rules_md": "Be kind and tidy",
            },
            "property_notes_md": "",
        },
    )
    return _expect_str(created, "id")


def _create_work_role(api: _SessionApi, *, run_id: str) -> str:
    created = api.post(
        f"/w/{api.workspace_slug}/api/v1/work_roles",
        {
            "key": f"j3-cleaner-{run_id}",
            "name": "GA Journey 3 Cleaner",
            "description_md": "",
            "default_settings_json": {},
            "icon_name": "clipboard-check",
        },
    )
    return _expect_str(created, "id")


def _find_reservation(
    rows: list[dict[str, Any]], *, check_in: _dt.datetime
) -> dict[str, Any]:
    """Return the row whose ``check_in`` matches ``check_in`` (UTC).

    The list endpoint sorts by check_in ascending, so the matching
    row is normally row 0; we still match by timestamp explicitly
    so a future ordering tweak (or a leftover cross-test row that
    landed in the same workspace) can't silently shift the index.
    """
    target = check_in.astimezone(_dt.UTC).replace(microsecond=0)
    for row in rows:
        raw = row.get("check_in")
        if not isinstance(raw, str):
            continue
        try:
            row_check_in = _dt.datetime.fromisoformat(raw).astimezone(_dt.UTC)
        except ValueError:
            continue
        if row_check_in.replace(microsecond=0) == target:
            return row
    raise AssertionError(
        f"no reservation found with check_in={target.isoformat()}; rows={rows!r}"
    )


def _read_csrf_cookie(page: Any) -> str:
    """Return the ``crewday_csrf`` cookie value from the page jar.

    The CSRF middleware sets this cookie on every response; the SPA
    echoes it on every mutating request. Mirror the exact contract
    so the worker's photo upload / complete fetches clear.
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


# ---------------------------------------------------------------------------
# ICS body + HTTPS feed server
# ---------------------------------------------------------------------------


def _build_two_stay_ics(
    *,
    first_check_in: _dt.datetime,
    first_check_out: _dt.datetime,
    second_check_in: _dt.datetime,
    second_check_out: _dt.datetime,
    uid_prefix: str,
) -> bytes:
    """Return a VCALENDAR body with two consecutive reservations.

    The two events form a back-to-back booking — Avery's checkout
    (``first_check_out``) feeds Bailey's check-in
    (``second_check_in``) — so the "after_checkout" rule on the
    first stay finds the second stay as its next-stay anchor. The
    bundle generator subscriber runs synchronously per-event and
    queries the DB for "next reservation at this property"; the
    later stay must therefore be present *before* the earlier stay
    is upserted, otherwise the lookup whiffs and skips with
    ``skipped_no_next_stay``.

    We satisfy that by ordering the ``BEGIN:VEVENT`` blocks
    **later-stay first**: the parser walks events in source order,
    upserts Bailey first (no later stay → no bundle), then upserts
    Avery (Bailey is already in the DB → bundle fires). Spec §04
    "iCal feed" §"Polling behavior" doesn't pin a sort order on
    ingest, so this is a fixture choice rather than a contract bend.
    """

    def _stamp(dt: _dt.datetime) -> str:
        return dt.astimezone(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")

    return (
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"PRODID:-//crewday-e2e-journey3//EN\r\n"
        # Later stay first so its row is durable when the earlier
        # stay's bundle handler queries for "next reservation".
        b"BEGIN:VEVENT\r\n"
        + f"UID:{uid_prefix}-2\r\n".encode("ascii")
        + f"DTSTART:{_stamp(second_check_in)}\r\n".encode("ascii")
        + f"DTEND:{_stamp(second_check_out)}\r\n".encode("ascii")
        + b"SUMMARY:Reserved (Bailey)\r\n"
        b"END:VEVENT\r\n"
        b"BEGIN:VEVENT\r\n"
        + f"UID:{uid_prefix}-1\r\n".encode("ascii")
        + f"DTSTART:{_stamp(first_check_in)}\r\n".encode("ascii")
        + f"DTEND:{_stamp(first_check_out)}\r\n".encode("ascii")
        + b"SUMMARY:Reserved (Avery)\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )


_ICS_SERVER_SCRIPT: Final[str] = """\
import http.server, ssl, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/feed.ics':
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header('Content-Type','text/calendar; charset=utf-8')
        self.send_header('Content-Length', str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)
    def log_message(self, *args, **kwargs):
        return
with open('/tmp/icaltest/body.ics','rb') as f:
    BODY = f.read()
s = http.server.HTTPServer(('127.0.0.1', 0), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain('/tmp/icaltest/crt.pem', '/tmp/icaltest/key.pem')
s.socket = ctx.wrap_socket(s.socket, server_side=True)
print('PORT', s.server_address[1], flush=True)
s.serve_forever()
"""


@contextmanager
def _serve_ics_https(body: bytes) -> Iterator[str]:
    """Spin up a single-shot HTTPS server **inside the app-api container**.

    The validator inside the same container reaches the listener via
    ``https://127.0.0.1:<port>/feed.ics``. Three properties matter:

    * **Loopback path.** Production-host firewalls don't see this
      traffic — it never leaves the container's network namespace.
      A bridge-gateway bind would have to fight ufw / iptables
      DROP rules that are common on shared dev hosts.
    * **Self-signed cert.** Generated with ``openssl req -x509`` at
      runtime; the workspace setting ``ical.allow_self_signed=true``
      (cd-t2qtg) tells the validator to accept it.
    * **Single shutdown.** The server process is started by
      ``docker exec`` and tracked by PID; the context-manager exit
      sends ``kill <pid>`` so a flaky teardown can't leave a port
      binding behind.
    """
    # 1. Write cert + body into a host-side tempdir, then ``docker cp``
    #    them into the container's ``/tmp/icaltest``. The container
    #    has its own filesystem — bind-mounting just to share a few
    #    kB would mean editing the compose file, which we'd rather
    #    avoid for an e2e affordance.
    with tempfile.TemporaryDirectory(prefix="crewday-e2e-ical-") as host_dir_str:
        host_dir = Path(host_dir_str)
        body_path = host_dir / "body.ics"
        body_path.write_bytes(body)
        cert_path = host_dir / "crt.pem"
        key_path = host_dir / "key.pem"
        # Use openssl rather than the ``cryptography`` package to
        # keep the dep list narrow — the package is available as a
        # transitive but pinning it here would couple the e2e suite
        # to its release cadence. ``openssl req -x509`` ships with
        # any container we'd run against and on the host's stock
        # toolchain.
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(key_path),
                "-out",
                str(cert_path),
                "-days",
                "1",
                "-nodes",
                "-subj",
                "/CN=crewday-e2e-journey3",
            ],
            check=True,
            capture_output=True,
        )

        # 2. Drop the trio into the container under /tmp/icaltest.
        #    A leftover directory between runs would be benign — we
        #    overwrite per call — but the per-run cleanup at exit
        #    keeps ``/tmp`` tidy when many e2e runs share one stack.
        subprocess.run(
            ["docker", "exec", _APP_API_CONTAINER, "mkdir", "-p", "/tmp/icaltest"],
            check=True,
            capture_output=True,
        )
        for path in (body_path, cert_path, key_path):
            subprocess.run(
                [
                    "docker",
                    "cp",
                    str(path),
                    f"{_APP_API_CONTAINER}:/tmp/icaltest/{path.name}",
                ],
                check=True,
                capture_output=True,
            )

        # 3. Write the server script into the container, then spawn it
        #    in the background. ``docker exec`` runs in the foreground
        #    by default; ``-d`` would daemonise but throws away the
        #    PID we need to kill cleanly. Instead we ``Popen`` the
        #    exec, parse the "PORT <n>" line off stdout, and kill the
        #    server process by PID at teardown.
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                _APP_API_CONTAINER,
                "tee",
                "/tmp/icaltest/srv.py",
            ],
            input=_ICS_SERVER_SCRIPT,
            text=True,
            check=True,
            capture_output=True,
        )

        # ``setsid`` keeps the python child alive after the docker
        # exec parent disconnects — without it ``Popen.kill`` would
        # propagate SIGTERM to the docker exec wrapper but not the
        # underlying server process if the wrapper exits first.
        proc = subprocess.Popen(
            [
                "docker",
                "exec",
                _APP_API_CONTAINER,
                "sh",
                "-c",
                "echo $$ > /tmp/icaltest/server.pid; exec python /tmp/icaltest/srv.py",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def _teardown() -> None:
            """Kill the in-container server and wipe its workdir.

            Idempotent — safe to call on every error path including
            "port never arrived" and "yielded scope raised". Runs the
            kill via ``docker exec`` (the wrapper Popen may already
            be dead by then) so a stuck wrapper can't leave the
            in-container python running with the loopback port held.
            """
            subprocess.run(
                [
                    "docker",
                    "exec",
                    _APP_API_CONTAINER,
                    "sh",
                    "-c",
                    "kill $(cat /tmp/icaltest/server.pid) 2>/dev/null; "
                    "rm -rf /tmp/icaltest",
                ],
                capture_output=True,
            )
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                with suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)

        port: int | None = None
        # Read the server's "PORT <n>" line. Ten seconds is generous
        # for python to bind a loopback port — even a slow CI host
        # imports + binds in well under that — but bounded so a bad
        # cert / mis-mounted body file fails fast rather than hanging
        # the whole suite.
        deadline = time.monotonic() + 10.0
        if proc.stdout is None:
            _teardown()
            raise AssertionError("ICS server Popen produced no stdout pipe")
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    stderr = proc.stderr.read() if proc.stderr is not None else ""
                    _teardown()
                    raise AssertionError(
                        f"ICS server exited before reporting port: "
                        f"rc={proc.returncode} stderr={stderr!r}"
                    )
                continue
            line = line.strip()
            if line.startswith("PORT "):
                port = int(line.split(None, 1)[1])
                break
        if port is None:
            _teardown()
            raise AssertionError("ICS server never reported a PORT line")

        try:
            yield f"https://127.0.0.1:{port}/feed.ics"
        finally:
            _teardown()
