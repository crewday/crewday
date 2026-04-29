"""Quote-send Mailpit round-trip against the dev stack.

This test targets the quote email leg only: seed a dev-session owner,
create the minimum billing org/property/quote over the real dev-stack
HTTP API, send the quote through SMTP, poll Mailpit, and redeem the
public token from the delivered email.
"""

from __future__ import annotations

import http.cookies
import json
import os
import re
import secrets
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest

from app.auth.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.auth.session_cookie import SESSION_COOKIE_NAME
from tests.integration.mail import (
    fetch_message_detail,
    is_reachable,
    purge_inbox,
    wait_for_message,
)

pytestmark = pytest.mark.integration

_DEFAULT_APP_URL: Final[str] = "http://127.0.0.1:8100"
_DEFAULT_MAILPIT_URL: Final[str] = "http://127.0.0.1:8026"
_QUOTE_LINK_RE: Final[re.Pattern[str]] = re.compile(
    r"https?://[^\s\"<]+/q/[A-Z0-9]+\?token=[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
)


def _app_url() -> str:
    return os.environ.get("CREWDAY_TEST_APP_URL", _DEFAULT_APP_URL)


def _mailpit_url() -> str:
    return os.environ.get("CREWDAY_TEST_MAILPIT_URL", _DEFAULT_MAILPIT_URL)


def _app_reachable(app_url: str, *, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{app_url}/healthz", timeout=timeout) as resp:
            status = int(resp.status)
            resp.read()
    except urllib.error.URLError, ConnectionError, OSError:
        return False
    return 200 <= status < 300


def _readyz_failures(app_url: str, *, timeout: float = 2.0) -> list[str] | None:
    try:
        with urllib.request.urlopen(f"{app_url}/readyz", timeout=timeout) as resp:
            status = int(resp.status)
            payload_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            status = exc.code
            payload_bytes = exc.read()
        finally:
            exc.close()
    except urllib.error.URLError, ConnectionError, OSError:
        return ["unreachable"]

    if 200 <= status < 300:
        return None
    try:
        payload = json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}
    except ValueError:
        return [f"http_{status}"]
    if not isinstance(payload, dict):
        return [f"http_{status}"]
    checks = payload.get("checks", [])
    if not isinstance(checks, list):
        return [f"http_{status}"]
    failures = [
        check.get("check", "unknown")
        for check in checks
        if isinstance(check, dict) and check.get("ok") is False
    ]
    return failures or [f"http_{status}"]


@pytest.fixture(scope="module")
def stack_endpoints() -> Iterator[tuple[str, str]]:
    app_url = _app_url()
    mailpit_url = _mailpit_url()
    if not _app_reachable(app_url):
        pytest.skip(
            f"app-api not reachable at {app_url}; start the dev stack with "
            "`docker compose -f mocks/docker-compose.yml up -d`"
        )
    failures = _readyz_failures(app_url)
    if failures is not None:
        pytest.skip(
            f"app-api at {app_url} is not ready (failing: {failures}); "
            "run `docker compose -f mocks/docker-compose.yml exec app-api "
            "alembic upgrade head` or restart the dev stack"
        )
    if not is_reachable(mailpit_url):
        pytest.skip(
            f"Mailpit not reachable at {mailpit_url}; start the dev stack with "
            "`docker compose -f mocks/docker-compose.yml up -d`"
        )
    yield app_url, mailpit_url


@pytest.fixture
def clean_inbox(stack_endpoints: tuple[str, str]) -> Iterator[tuple[str, str]]:
    _, mailpit_url = stack_endpoints
    purge_inbox(mailpit_url)
    yield stack_endpoints


def _run_dev_login_or_skip(*, email: str, workspace: str) -> str:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not on PATH; cannot seed dev-stack identity")
    cmd = [
        "docker",
        "compose",
        "-f",
        "mocks/docker-compose.yml",
        "exec",
        "-T",
        "app-api",
        "python",
        "-m",
        "scripts.dev_login",
        "--email",
        email,
        "--workspace",
        workspace,
        "--role",
        "owner",
        "--output",
        "cookie",
    ]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            cwd=os.getcwd(),
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"dev-login seed command could not run: {exc!r}")
    if result.returncode != 0:
        pytest.skip(
            "dev-login seed command failed; is the dev stack running? "
            f"stderr={result.stderr.strip()!r}"
        )
    cookie_name, sep, cookie_value = result.stdout.strip().partition("=")
    if sep != "=" or cookie_name != SESSION_COOKIE_NAME or not cookie_value:
        pytest.skip(f"dev-login returned unexpected cookie output: {result.stdout!r}")
    return cookie_value


def _post_json(
    url: str,
    body: dict[str, Any],
    *,
    session_cookie: str | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    csrf = secrets.token_urlsafe(18)
    cookies = http.cookies.SimpleCookie()
    cookies[CSRF_COOKIE_NAME] = csrf
    if session_cookie is not None:
        cookies[SESSION_COOKIE_NAME] = session_cookie
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            CSRF_HEADER_NAME: csrf,
            "Cookie": "; ".join(m.OutputString() for m in cookies.values()),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload_bytes = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        try:
            payload_bytes = exc.read()
            status = exc.code
        finally:
            exc.close()
    payload = json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}
    if not isinstance(payload, dict):
        raise AssertionError(f"POST {url} returned non-object JSON: {payload!r}")
    return status, payload


def _get_json(url: str, *, timeout: float = 10.0) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url, method="GET", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload_bytes = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        try:
            payload_bytes = exc.read()
            status = exc.code
        finally:
            exc.close()
    payload = json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}
    if not isinstance(payload, dict):
        raise AssertionError(f"GET {url} returned non-object JSON: {payload!r}")
    return status, payload


def _create_quote_fixture(app_url: str, workspace: str, cookie: str, email: str) -> str:
    api = f"{app_url}/w/{workspace}/api/v1"
    org_status, org = _post_json(
        f"{api}/billing/organizations",
        {
            "kind": "client",
            "display_name": "Mailpit Quote Client",
            "billing_address": {},
            "default_currency": "EUR",
            "contact_email": email,
        },
        session_cookie=cookie,
    )
    assert org_status == 201, f"unexpected organization status; body={org!r}"
    org_id = org.get("id")
    assert isinstance(org_id, str) and org_id

    property_status, property_body = _post_json(
        f"{api}/properties",
        {
            "name": "Mailpit Quote Villa",
            "kind": "vacation",
            "timezone": "Europe/Paris",
            "client_org_id": org_id,
            "address_json": {
                "line1": "1 Quote Way",
                "line2": None,
                "city": "Antibes",
                "state_province": None,
                "postal_code": "06600",
                "country": "FR",
            },
        },
        session_cookie=cookie,
    )
    assert property_status == 201, f"unexpected property status; body={property_body!r}"
    property_id = property_body.get("id")
    assert isinstance(property_id, str) and property_id

    quote_status, quote = _post_json(
        f"{api}/billing/quotes",
        {
            "organization_id": org_id,
            "property_id": property_id,
            "title": "Mailpit pool repair quote",
            "body_md": "Replace seal and rebalance pump.",
            "total_cents": 12500,
            "currency": "EUR",
        },
        session_cookie=cookie,
    )
    assert quote_status == 201, f"unexpected quote status; body={quote!r}"
    quote_id = quote.get("id")
    assert isinstance(quote_id, str) and quote_id
    return quote_id


def _extract_quote_url(body_text: str) -> str:
    match = _QUOTE_LINK_RE.search(body_text)
    if match is None:
        raise AssertionError(f"no public quote URL found in body:\n{body_text!r}")
    return match.group(0)


def test_quote_send_delivers_mailpit_email_and_public_accept_works(
    clean_inbox: tuple[str, str],
) -> None:
    app_url, mailpit_url = clean_inbox
    suffix = uuid.uuid4().hex[:12]
    workspace = f"quote-mailpit-{suffix}"
    manager_email = f"quote-mailpit-manager-{suffix}@dev.local"
    client_email = f"quote-mailpit-client-{suffix}@dev.local"
    session_cookie = _run_dev_login_or_skip(email=manager_email, workspace=workspace)
    quote_id = _create_quote_fixture(app_url, workspace, session_cookie, client_email)

    send_status, sent = _post_json(
        f"{app_url}/w/{workspace}/api/v1/billing/quotes/{quote_id}/send",
        {"base_url": app_url},
        session_cookie=session_cookie,
    )
    assert send_status == 200, f"unexpected quote send status; body={sent!r}"
    assert sent["status"] == "sent"

    envelope = wait_for_message(mailpit_url, to=client_email)
    assert envelope["Subject"] == "crew.day quote: Mailpit pool repair quote"
    internal_id = envelope["ID"]
    assert isinstance(internal_id, str) and internal_id

    detail = fetch_message_detail(mailpit_url, internal_id)
    text_body = detail.get("Text")
    html_body = detail.get("HTML")
    assert isinstance(text_body, str) and text_body
    assert isinstance(html_body, str) and html_body
    assert "Mailpit pool repair quote" in text_body
    assert "EUR 125.00" in text_body
    assert f"/q/{quote_id}?token=" in text_body
    assert f"/q/{quote_id}?token=" in html_body

    quote_url = _extract_quote_url(text_body)
    parsed = urllib.parse.urlparse(quote_url)
    params = urllib.parse.parse_qs(parsed.query)
    tokens = params.get("token", [])
    assert parsed.path == f"/q/{quote_id}"
    assert len(tokens) == 1 and len(tokens[0]) >= 32

    public_query = urllib.parse.urlencode({"token": tokens[0]})
    public_url = f"{app_url}{parsed.path}?{public_query}"
    get_status, opened = _get_json(public_url)
    assert get_status == 200, f"unexpected public quote status; body={opened!r}"
    assert opened["status"] == "sent"

    accept_status, accepted = _post_json(
        f"{app_url}/q/{quote_id}/accept?{urllib.parse.urlencode({'token': tokens[0]})}",
        {},
    )
    assert accept_status == 200, f"unexpected public accept status; body={accepted!r}"
    assert accepted["status"] == "accepted"
