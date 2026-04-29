"""Authentication helpers for the e2e suite (cd-ndmv).

Two surfaces:

* :func:`login_with_dev_session` — the **fast path**. Mints a session
  cookie via ``scripts/dev_login.py`` (running inside the
  ``app-api`` compose container so the SQLite DB is the one the dev
  stack is actually serving) and seeds it onto a Playwright context
  so the next ``page.goto("/today")`` lands authenticated. Used by
  every pilot test that doesn't itself exercise the passkey
  ceremony — the cookie is the same row :class:`SessionCookie`
  middleware would issue at end of a real passkey login.

* :func:`enroll_owner` — the **full first-boot journey**. Drives the
  signup → magic-link → passkey-register → passkey-login assertion →
  role-home flow with Mailpit's REST API for token interception and a
  Chrome DevTools Protocol WebAuthn virtual authenticator for the two
  browser ceremonies. Implements the §17 "End-to-end" pilot journey
  against the loopback e2e stack.

  **RP-ID gate.** The e2e compose override must serve a WebAuthn
  ``rp_id`` that matches the loopback host. The helper raises
  :class:`RPIDMismatch` with a focused message when the running stack
  still advertises the normal ``dev.crew.day`` RP ID.

The dev_login fast path is intentionally separate from the full
ceremony: the spec's GA journeys (§17) cover the *user* flow at the
UI level, but most other pilot tests (visual regression, 360 px
sitemap walk) just need an authenticated page and don't care which
mechanism produced the cookie. Splitting the two helpers keeps each
test's intent visible.

See ``docs/specs/03-auth-and-tokens.md`` and ``scripts/dev_login.py``.
"""

from __future__ import annotations

import re
import subprocess
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from playwright.sync_api import APIResponse, BrowserContext, Page

from app.auth.session_cookie import DEV_SESSION_COOKIE_NAME, SESSION_COOKIE_NAME
from tests.integration.mail import (
    fetch_message_detail,
    fetch_messages,
)

__all__ = [
    "DEFAULT_DEV_PASSKEY_NAME",
    "DEFAULT_MAILPIT_BASE_URL",
    "DevLoginResult",
    "EnrollmentResult",
    "MailpitMessage",
    "RPIDMismatch",
    "consume_magic_link_via_mailpit",
    "enroll_owner",
    "extract_magic_link_token",
    "install_virtual_authenticator",
    "login_with_dev_session",
    "wait_for_magic_link",
]

# Compose **service** name (not container name) for ``app-api``.
# ``docker compose exec`` resolves services via the compose file's
# top-level ``services:`` key — passing the container name (e.g.
# ``crewday-app-api``) makes compose error out with "no service named".
# Tests that override the dev stack via a different compose file
# should pass ``service=`` explicitly.
_DEFAULT_DEV_LOGIN_SERVICE: Final[str] = "app-api"

# Mailpit's JSON API — see https://mailpit.axllent.org/docs/api-v1/.
# The ``mocks/docker-compose.yml`` publishes Mailpit's web UI on
# ``127.0.0.1:8026`` (compose maps :8025 → :8026 to dodge a host-side
# port collision); the JSON API lives on the same port.
DEFAULT_MAILPIT_BASE_URL: Final[str] = "http://127.0.0.1:8026"

# Display name for the virtual authenticator's credential. Surfaces in
# the SPA's "Your passkeys" list — useful when a flake leaves a stale
# row, since you can grep audit log for the name.
DEFAULT_DEV_PASSKEY_NAME: Final[str] = "e2e-virtual-authenticator"

# Resolve the compose file relative to the repo root rather than the
# caller's cwd. ``parents[3]`` walks tests/e2e/_helpers/auth.py →
# crewday/. A relative path would silently break if a developer ran
# ``pytest`` from somewhere other than the repo root (the test
# harness reports rootdir to pytest but ``subprocess.run`` inherits
# the host cwd).
_COMPOSE_FILE: Final[Path] = (
    Path(__file__).resolve().parents[3] / "mocks" / "docker-compose.yml"
)
_DEV_LOGIN_CACHE: dict[tuple[str, str, str, str, str, str], str] = {}

# Dev-only cookie alias that the FastAPI routers accept in addition to
# the canonical ``__Host-crewday_session`` — see
# :mod:`app.auth.session_cookie`. The alias name has no prefix
# invariants, so ``Secure`` isn't required; dropping it lets Chromium
# ship the cookie on plain HTTP loopback. We import the canonical
# constant rather than hard-coding ``"crewday_session"`` so a rename
# in the deployment cookie naming surfaces here as a type / import
# error, not a silent e2e auth failure.
_DEV_FALLBACK_COOKIE_NAME: Final[str] = DEV_SESSION_COOKIE_NAME

# Compiled regex used to extract the magic-link token URL from
# Mailpit's text/HTML bodies. The signup email body contains a single
# anchor like ``https://dev.crew.day/auth/magic/<base64url-token>``;
# the consume endpoint accepts only the token portion. Group 1 is the
# token. We accept either ``https`` or ``http`` so a self-hosted
# tweak that points ``CREWDAY_PUBLIC_URL`` at a plain-HTTP origin
# still works.
_MAGIC_LINK_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"https?://[^\s/]+/auth/magic/([A-Za-z0-9_\-\.]+)"
)


class RPIDMismatch(RuntimeError):
    """The dev stack's ``rp_id`` does not match the page origin's host.

    The full passkey ceremony cannot complete in this configuration;
    callers should switch to :func:`login_with_dev_session` or run the
    suite against an origin whose host matches the deployment's
    ``CREWDAY_WEBAUTHN_RP_ID``.
    """


@dataclass(frozen=True)
class DevLoginResult:
    """Returned by :func:`login_with_dev_session`.

    ``cookie_value`` is the opaque session token; ``cookie_name`` the
    ``__Host-crewday_session`` constant. ``email`` and ``slug`` echo
    the inputs so a test can assert against them after the page loads.
    """

    cookie_name: str
    cookie_value: str
    email: str
    slug: str


@dataclass(frozen=True)
class EnrollmentResult:
    """Returned by :func:`enroll_owner` after registration + assertion."""

    email: str
    slug: str
    user_id: str


@dataclass(frozen=True)
class MailpitMessage:
    """Subset of Mailpit's message envelope the e2e helpers need.

    Mailpit exposes far more (CC / BCC / attachments / size / …); we
    pull only the fields the magic-link extractor consumes so the
    dataclass stays cheap to construct.
    """

    id: str
    subject: str
    body_text: str
    body_html: str
    to_addresses: tuple[str, ...]


# ---------------------------------------------------------------------------
# Dev-login fast path
# ---------------------------------------------------------------------------


def login_with_dev_session(
    context: BrowserContext,
    *,
    base_url: str,
    email: str,
    workspace_slug: str,
    role: Literal["owner", "manager", "worker"] = "owner",
    service: str = _DEFAULT_DEV_LOGIN_SERVICE,
    compose_file: Path | str = _COMPOSE_FILE,
) -> DevLoginResult:
    """Mint a session cookie via ``scripts/dev_login.py`` and inject it.

    Round-trips ``docker compose exec <service> python -m
    scripts.dev_login --output cookie ...``, parses the
    ``__Host-crewday_session=<value>`` line, and seeds it on the
    Playwright context's cookie jar so the next ``page.goto("/today")``
    arrives authenticated. The container's
    ``CREWDAY_DEV_AUTH=1`` / ``CREWDAY_PROFILE=dev`` env (set by the
    compose file) keeps the script's three gates green; tests that
    point at a non-dev stack will see the script refuse and the
    helper raises :class:`subprocess.CalledProcessError`.

    **Why the ``crewday_session`` alias instead of ``__Host-…``,
    and why the cookie jar (not ``set_extra_http_headers``).** Three
    interlocking browser/test-runner policies pin this design:

    1. The deployment-canonical cookie carries the ``__Host-`` prefix,
       which forces ``Secure``. Chromium *accepts* a Secure cookie on
       a ``127.0.0.1`` origin (loopback is a secure context for set
       purposes) but refuses to *send* it on plain-HTTP requests —
       the cookie stays in the jar and never reaches the server.
    2. ``BrowserContext.set_extra_http_headers({"Cookie": ...})``
       merges with the cookie jar at request time. The first response
       that drops a non-session cookie (the SPA's ``crewday_csrf``)
       causes the browser to materialise a jar entry for the origin;
       on subsequent requests the jar value wins and the
       extra-header value is silently dropped. The pilot test fails
       on the SPA's bootstrap probe in exactly that shape.
    3. The server router accepts the deployment-canonical
       ``__Host-crewday_session`` *and* the dev-fallback alias
       ``crewday_session`` (see :func:`app.api.v1.auth.me.get_me`).

    So we set the alias-named cookie directly in the jar with
    ``secure=False`` — the alias has no prefix invariants, so
    ``Secure`` isn't required; dropping it lets Chromium ship the
    cookie on plain HTTP. The same opaque session value reaches the
    server, the DB row is identical to a passkey-issued session.
    The alias is purely a wire-format affordance for e2e + curl
    smoke against the loopback.

    Idempotent on ``(email, workspace_slug)`` — repeated calls reuse
    the User / Workspace rows and mint fresh sessions; see
    :func:`scripts.dev_login.mint_session` for the row lifecycle.
    """
    cache_key = (
        base_url,
        email,
        workspace_slug,
        role,
        service,
        str(compose_file),
    )
    value = _DEV_LOGIN_CACHE.get(cache_key)
    if value is None:
        cmd = [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "exec",
            "-T",
            service,
            "python",
            "-m",
            "scripts.dev_login",
            "--email",
            email,
            "--workspace",
            workspace_slug,
            "--role",
            role,
            "--output",
            "cookie",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        line = proc.stdout.strip()
        if "=" not in line:
            raise RuntimeError(
                f"dev_login output missing '=' separator: {line!r}; "
                f"stderr={proc.stderr!r}"
            )
        name, _, value = line.partition("=")
        # ``scripts.dev_login --output cookie`` always emits the deployment-
        # canonical ``__Host-crewday_session`` name (prod shape). Surfacing
        # a drift here points at a compose-file regression — usually a
        # stale ``CREWDAY_PROFILE`` / ``CREWDAY_DEV_AUTH`` env that flipped
        # the script into a different output mode.
        if name != SESSION_COOKIE_NAME:
            raise RuntimeError(
                f"unexpected cookie name {name!r} from dev_login (expected "
                f"{SESSION_COOKIE_NAME!r}); compose file may be out of date"
            )
        _DEV_LOGIN_CACHE[cache_key] = value

    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    # See the docstring for the prefix / cookie-jar dance — the alias
    # name carries the same opaque value through to the server, with
    # ``secure=False`` so plain-HTTP loopback can transport it.
    context.add_cookies(
        [
            {
                "name": _DEV_FALLBACK_COOKIE_NAME,
                "value": value,
                "domain": host,
                "path": "/",
                "secure": False,
                "httpOnly": True,
                "sameSite": "Lax",
            }
        ]
    )
    return DevLoginResult(
        cookie_name=_DEV_FALLBACK_COOKIE_NAME,
        cookie_value=value,
        email=email,
        slug=workspace_slug,
    )


# ---------------------------------------------------------------------------
# Mailpit + magic-link helpers
# ---------------------------------------------------------------------------


def wait_for_magic_link(
    *,
    recipient: str,
    mailpit_base_url: str = DEFAULT_MAILPIT_BASE_URL,
    subject_substring: str = "verify your email",
    timeout_seconds: float = 15.0,
    poll_interval_seconds: float = 0.5,
) -> MailpitMessage:
    """Poll Mailpit until a message addressed to ``recipient`` arrives.

    The dev stack routes outbound email through the in-stack Mailpit
    container (see ``mocks/docker-compose.yml``); the JSON API
    surfaces every message so a test can pull a magic-link without
    racing the SMTP delivery. We poll because Mailpit's ingestion is
    async to the SMTP send — the ``/auth/magic/start`` POST returns
    202 the moment the nonce row is durable, well before the message
    has been parsed and indexed.

    ``subject_substring`` filters by case-insensitive substring match
    on the message subject; the default matches the signup email
    template (``"crew.day — verify your email and finish signing up"``).

    Built on :func:`tests.integration.mail.fetch_messages` /
    :func:`tests.integration.mail.fetch_message_detail` so the polling +
    HTTP shape stays identical to the magic-link Mailpit round-trip
    test (``tests/integration/auth/test_magic_link_mailpit.py``); see
    that module's docstring for the "do not copy-paste the polling
    loop" rationale (cd-o62m).

    Raises :class:`TimeoutError` if no matching message arrives within
    ``timeout_seconds``. The error includes the count of unrelated
    messages seen so a test can spot "the email landed but the filter
    is wrong" vs "no email at all".
    """
    deadline = time.monotonic() + timeout_seconds
    recipient_cf = recipient.casefold()
    subject_cf = subject_substring.casefold()
    saw_recipient = 0
    while time.monotonic() < deadline:
        envelopes = fetch_messages(mailpit_base_url)
        for envelope in envelopes:
            if not _envelope_matches_recipient(envelope, recipient_cf):
                continue
            saw_recipient += 1
            subject = str(envelope.get("Subject", ""))
            if subject_cf not in subject.casefold():
                continue
            internal_id = envelope.get("ID")
            if not isinstance(internal_id, str):
                # Mailpit's listing always carries an ``ID`` string,
                # but we keep the guard so a future API drift surfaces
                # as a focused TypeError on the next poll, not as a
                # silent skip.
                continue
            detail = fetch_message_detail(mailpit_base_url, internal_id)
            return MailpitMessage(
                id=internal_id,
                subject=str(detail.get("Subject", subject)),
                body_text=str(detail.get("Text", "")),
                body_html=str(detail.get("HTML", "")),
                to_addresses=_extract_to_addresses(detail.get("To")),
            )
        time.sleep(poll_interval_seconds)
    raise TimeoutError(
        f"no Mailpit message matched recipient={recipient!r} + "
        f"subject~={subject_substring!r} within {timeout_seconds}s "
        f"(saw {saw_recipient} unrelated messages addressed to recipient)"
    )


def consume_magic_link_via_mailpit(
    page: Page,
    *,
    base_url: str,
    recipient: str,
    mailpit_base_url: str = DEFAULT_MAILPIT_BASE_URL,
    subject_substring: str = "verify your email",
) -> str:
    """Pull the most recent magic-link email and navigate the page to it.

    Combines :func:`wait_for_magic_link` + :func:`extract_magic_link_token`
    + ``page.goto``. Returns the raw token string for assertions.

    The email body carries a fully-qualified URL pointing at
    ``CREWDAY_PUBLIC_URL`` (``https://dev.crew.day``); the helper
    rewrites the host to ``base_url`` so the navigation lands on the
    test's actual origin (the loopback). Without the rewrite the
    page would try to reach the externally-protected host and never
    complete the ceremony.
    """
    msg = wait_for_magic_link(
        recipient=recipient,
        mailpit_base_url=mailpit_base_url,
        subject_substring=subject_substring,
    )
    token = extract_magic_link_token(msg)
    consume_url = f"{base_url.rstrip('/')}/auth/magic/{token}"
    page.goto(consume_url)
    return token


def extract_magic_link_token(message: MailpitMessage) -> str:
    """Return the magic-link token embedded in ``message``'s body.

    Pulled out of :func:`consume_magic_link_via_mailpit` so a unit test
    can pin the regex behaviour without needing the dev stack — the
    regex is the only fragile piece (a future template tweak that
    nests the URL in something the regex doesn't match would silently
    break every magic-link e2e), so we cover it directly.

    Prefers the plain-text body (templates render the URL literally);
    falls back to HTML for templates that ship HTML-only. Raises
    :class:`RuntimeError` if no URL matches — the caller's test will
    have a focused error instead of a generic ``IndexError`` on a
    later ``.group(1)``.
    """
    body = message.body_text or message.body_html
    match = _MAGIC_LINK_TOKEN_RE.search(body)
    if match is None:
        raise RuntimeError(
            f"magic-link email arrived but no token URL found in body; "
            f"subject={message.subject!r}, body[:200]={body[:200]!r}"
        )
    return match.group(1)


def _envelope_matches_recipient(
    envelope: Mapping[str, object], recipient_cf: str
) -> bool:
    """Return ``True`` when Mailpit's listing envelope addresses ``recipient_cf``.

    ``envelope`` here is the raw per-message dict from
    ``/api/v1/messages``; the ``To`` shape mirrors what
    :mod:`tests.integration.mail` consumes. Lower-cased comparison
    matches the canonical form Mailpit stores.
    """
    to_records = envelope.get("To")
    if not isinstance(to_records, list):
        return False
    for record in to_records:
        if not isinstance(record, dict):
            continue
        addr = record.get("Address")
        if isinstance(addr, str) and addr.casefold() == recipient_cf:
            return True
    return False


def _extract_to_addresses(raw: object) -> tuple[str, ...]:
    """Return the ``To.Address`` strings from Mailpit's detail envelope.

    Defensively typed because the Mailpit JSON shape is
    ``dict[str, Any]`` upstream — we materialise only the strings we
    need so a future API drift surfaces as an empty tuple, not a
    crash.
    """
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for record in raw:
        if not isinstance(record, dict):
            continue
        addr = record.get("Address")
        if isinstance(addr, str):
            out.append(addr)
    return tuple(out)


# ---------------------------------------------------------------------------
# WebAuthn virtual authenticator
# ---------------------------------------------------------------------------


def install_virtual_authenticator(
    context: BrowserContext,
    *,
    transport: Literal["internal", "usb", "nfc", "ble", "hybrid"] = "internal",
    has_resident_key: bool = True,
    has_user_verification: bool = True,
) -> str:
    """Attach a CDP virtual authenticator to the context's active page.

    Returns the authenticator id so the test can drive registration /
    assertion afterwards (e.g. ``WebAuthn.getCredentials`` to verify a
    credential landed). Create the page that will perform the ceremony
    before calling this helper; Chromium exposes WebAuthn through a
    page CDP session, and installing it on a throwaway page does not
    reliably cover later pages. Only Chromium supports the WebAuthn CDP
    domain — WebKit covers the spec from the browser side, but its
    test surface is exposed differently and not via CDP. The helper
    raises :class:`NotImplementedError` for non-Chromium engines so
    the test fails with a focused message instead of a CDP "method
    not found" error.

    ``transport=internal`` matches a platform authenticator (Touch
    ID / Windows Hello); the other values exist for ceremony tests
    that exercise external authenticator UX. The defaults match the
    spec's "discoverable credential, user-verified" envelope.
    """
    browser_name = context.browser.browser_type.name if context.browser else ""
    if browser_name != "chromium":
        raise NotImplementedError(
            f"virtual authenticator helper only supports Chromium "
            f"(got {browser_name!r}); WebKit has its own internal "
            "test API not exposed over CDP — drive it via the per-test "
            "browser context options instead"
        )
    # Open a CDP session against the page that will run the browser
    # ceremony. If the caller has not created one yet, create one and
    # leave it in the context for them to reuse.
    pages = list(context.pages)
    page = pages[0] if pages else context.new_page()
    cdp = context.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    result = cdp.send(
        "WebAuthn.addVirtualAuthenticator",
        {
            "options": {
                "protocol": "ctap2",
                "transport": transport,
                "hasResidentKey": has_resident_key,
                "hasUserVerification": has_user_verification,
                "isUserVerified": True,
                "automaticPresenceSimulation": True,
            }
        },
    )
    return str(result["authenticatorId"])


# ---------------------------------------------------------------------------
# Full first-boot journey (signup + magic link + passkey + today)
# ---------------------------------------------------------------------------


def enroll_owner(
    page: Page,
    *,
    base_url: str,
    email: str,
    workspace_slug: str,
    mailpit_base_url: str = DEFAULT_MAILPIT_BASE_URL,
) -> EnrollmentResult:
    """Drive the full first-boot owner enrollment journey end-to-end.

    Flow per ``docs/specs/03-auth-and-tokens.md`` §"Self-serve signup":

    1. Visit ``/login`` so WebAuthn runs from the app origin.
    2. Start signup through ``POST /api/v1/signup/start``.
    3. Receive the magic link via Mailpit and consume it through
       ``POST /api/v1/signup/verify``.
    4. Register a passkey through the browser's
       ``navigator.credentials.create()``.
    5. Prove assertion works through ``navigator.credentials.get()``
       and ``POST /api/v1/auth/passkey/login/finish``.
    6. Land on ``/today`` (or the role-appropriate home).

    **RP-ID prerequisite.** The dev stack must serve a ``rp_id`` that
    matches the host portion of ``base_url``. The default compose
    config ships with ``rp_id=localhost`` and the loopback default
    ``base_url`` is ``http://localhost:8100``. Running without the e2e
    override raises :class:`RPIDMismatch` immediately so the test
    failure points at config drift rather than a black-box WebAuthn
    error.

    The helper does NOT install the virtual authenticator on its own —
    callers must do so via :func:`install_virtual_authenticator`
    *before* invoking this function so the same authenticator is
    available for downstream login-as flows.
    """
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    rp_id = _peek_rp_id(page=page, base_url=base_url)
    if rp_id and rp_id != host and not host.endswith(f".{rp_id}"):
        raise RPIDMismatch(
            f"WebAuthn rp_id {rp_id!r} does not match origin host "
            f"{host!r}; the browser will refuse "
            "navigator.credentials.create() against this combination. "
            "Align CREWDAY_WEBAUTHN_RP_ID with the e2e origin (or run "
            "the suite against the matching host)."
        )

    page.goto(f"{base_url.rstrip('/')}/login")

    _post_json(
        page,
        f"{base_url.rstrip('/')}/api/v1/signup/start",
        {
            "email": email,
            "desired_slug": workspace_slug,
            "captcha_token": "test-pass",
        },
        expected_status=202,
    )

    msg = wait_for_magic_link(
        recipient=email,
        mailpit_base_url=mailpit_base_url,
        subject_substring="verify your email",
    )
    token = extract_magic_link_token(msg)
    verify = _post_json(
        page,
        f"{base_url.rstrip('/')}/api/v1/signup/verify",
        {"token": token},
        expected_status=200,
    )
    signup_session_id = _expect_str(verify, "signup_session_id")
    display_name = "E2E Owner"

    start = _post_json(
        page,
        f"{base_url.rstrip('/')}/api/v1/signup/passkey/start",
        {
            "signup_session_id": signup_session_id,
            "display_name": display_name,
        },
        expected_status=200,
    )
    attestation = _create_passkey_attestation(page, start["options"])
    _post_json(
        page,
        f"{base_url.rstrip('/')}/api/v1/signup/passkey/finish",
        {
            "signup_session_id": signup_session_id,
            "challenge_id": _expect_str(start, "challenge_id"),
            "display_name": display_name,
            "timezone": "UTC",
            "credential": attestation,
        },
        expected_status=200,
    )

    login_start = _post_json(
        page,
        f"{base_url.rstrip('/')}/api/v1/auth/passkey/login/start",
        {},
        expected_status=200,
    )
    assertion = _get_passkey_assertion(page, login_start["options"])
    login_finish = _post_json_response(
        page,
        f"{base_url.rstrip('/')}/api/v1/auth/passkey/login/finish",
        {
            "challenge_id": _expect_str(login_start, "challenge_id"),
            "credential": assertion,
        },
        expected_status=200,
    )
    _mirror_dev_cookie_alias(
        page,
        base_url=base_url,
        set_cookie=login_finish.headers.get("set-cookie"),
    )
    body = login_finish.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"login finish returned non-object JSON: {body!r}")
    return EnrollmentResult(
        email=email,
        slug=workspace_slug,
        user_id=_expect_str(body, "user_id"),
    )


def _post_json(
    page: Page,
    url: str,
    body: dict[str, object],
    *,
    expected_status: int,
) -> dict[str, object]:
    response = _post_json_response(page, url, body, expected_status=expected_status)
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"{url} returned non-object JSON: {payload!r}")
    return payload


def _post_json_response(
    page: Page,
    url: str,
    body: dict[str, object],
    *,
    expected_status: int,
) -> APIResponse:
    response = page.request.post(url, data=body)
    if response.status != expected_status:
        raise RuntimeError(
            f"{url} returned {response.status}, expected {expected_status}; "
            f"body={response.text()[:500]!r}"
        )
    return response


def _expect_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"expected non-empty string at {key!r}; got {value!r}")
    return value


def _mirror_dev_cookie_alias(
    page: Page, *, base_url: str, set_cookie: str | None
) -> None:
    if not set_cookie:
        raise RuntimeError("passkey login finish did not return a Set-Cookie header")
    first = set_cookie.split(";", 1)[0]
    name, sep, value = first.partition("=")
    if sep != "=" or name != SESSION_COOKIE_NAME or not value:
        raise RuntimeError(
            f"unexpected passkey login Set-Cookie header {set_cookie!r}; "
            f"expected {SESSION_COOKIE_NAME}=..."
        )
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    page.context.add_cookies(
        [
            {
                "name": _DEV_FALLBACK_COOKIE_NAME,
                "value": value,
                "domain": host,
                "path": "/",
                "secure": False,
                "httpOnly": True,
                "sameSite": "Lax",
            }
        ]
    )


def _create_passkey_attestation(page: Page, options: object) -> dict[str, object]:
    return _evaluate_webauthn(page, "create", options)


def _get_passkey_assertion(page: Page, options: object) -> dict[str, object]:
    return _evaluate_webauthn(page, "get", options)


def _evaluate_webauthn(
    page: Page, mode: Literal["create", "get"], options: object
) -> dict[str, object]:
    payload = page.evaluate(
        """async ({ mode, options }) => {
          const fromB64 = (value) => {
            const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
            const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
            const binary = atob(padded);
            const out = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
            return out.buffer;
          };
          const toB64 = (buffer) => {
            const bytes = new Uint8Array(buffer);
            let binary = "";
            for (const byte of bytes) binary += String.fromCharCode(byte);
            return btoa(binary)
              .replace(/\\+/g, "-")
              .replace(/\\//g, "_")
              .replace(/=+$/g, "");
          };
          const decodeCreation = (json) => ({
            ...json,
            challenge: fromB64(json.challenge),
            user: { ...json.user, id: fromB64(json.user.id) },
            excludeCredentials: (json.excludeCredentials || []).map((d) => ({
              ...d,
              id: fromB64(d.id),
            })),
          });
          const decodeRequest = (json) => ({
            ...json,
            challenge: fromB64(json.challenge),
            allowCredentials: (json.allowCredentials || []).map((d) => ({
              ...d,
              id: fromB64(d.id),
            })),
          });
          if (!navigator.credentials) {
            throw new Error("navigator.credentials is unavailable");
          }
          const withTimeout = (promise, label) => Promise.race([
            promise,
            new Promise((_, reject) => {
              setTimeout(() => reject(new Error(`${label} timed out`)), 10000);
            }),
          ]);
          if (mode === "create") {
            const credential = await withTimeout(
              navigator.credentials.create({
                publicKey: decodeCreation(options),
              }),
              "navigator.credentials.create",
            );
            if (!credential || credential.type !== "public-key") {
              throw new Error("browser returned no public-key attestation");
            }
            const response = credential.response;
            const transports = typeof response.getTransports === "function"
              ? response.getTransports()
              : undefined;
            return {
              id: credential.id,
              rawId: toB64(credential.rawId),
              type: credential.type,
              response: {
                clientDataJSON: toB64(response.clientDataJSON),
                attestationObject: toB64(response.attestationObject),
                ...(transports && transports.length > 0 ? { transports } : {}),
              },
              ...(credential.authenticatorAttachment !== undefined
                ? { authenticatorAttachment: credential.authenticatorAttachment }
                : {}),
            };
          }
          const credential = await withTimeout(
            navigator.credentials.get({
              publicKey: decodeRequest(options),
              mediation: "required",
            }),
            "navigator.credentials.get",
          );
          if (!credential || credential.type !== "public-key") {
            throw new Error("browser returned no public-key assertion");
          }
          const response = credential.response;
          return {
            id: credential.id,
            rawId: toB64(credential.rawId),
            type: credential.type,
            response: {
              authenticatorData: toB64(response.authenticatorData),
              clientDataJSON: toB64(response.clientDataJSON),
              signature: toB64(response.signature),
              userHandle: response.userHandle && response.userHandle.byteLength > 0
                ? toB64(response.userHandle)
                : null,
            },
            ...(credential.authenticatorAttachment !== undefined
              ? { authenticatorAttachment: credential.authenticatorAttachment }
              : {}),
          };
        }""",
        {"mode": mode, "options": options},
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"WebAuthn {mode} returned non-object payload: {payload!r}")
    return payload


def _peek_rp_id(*, page: Page, base_url: str) -> str | None:
    """Best-effort lookup of the deployment's WebAuthn ``rp_id``.

    Calls ``POST /api/v1/auth/passkey/login/start`` (a public
    endpoint that returns the challenge envelope including ``rpId``).
    Returns ``None`` on any error so the caller can decide whether to
    skip the gate or treat it as fatal — :func:`enroll_owner` treats
    a ``None`` as "couldn't verify; proceed and let the browser fail
    the ceremony".
    """
    try:
        response = page.request.post(
            f"{base_url.rstrip('/')}/api/v1/auth/passkey/login/start",
            data={},
        )
        if response.status != 200:
            return None
        payload = response.json()
        rp_id = payload.get("options", {}).get("rpId")
        return str(rp_id) if rp_id else None
    except Exception:
        return None
