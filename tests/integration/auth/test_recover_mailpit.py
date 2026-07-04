"""Recovery magic-link round-trip against the dev stack with Mailpit.

The test drives the deployed dev-stack surface rather than an in-process
router: seed a non-step-up worker identity with ``scripts.dev_login``,
request recovery, poll Mailpit, consume the recovery URL through the
current recovery API, and start the passkey ceremony. Finishing the
ceremony requires a real WebAuthn authenticator or an in-process verifier
stub; this Mailpit smoke stops at the recovery-session/challenge boundary,
which proves the mailed link is valid without pretending to own browser
credential creation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

import pytest

from tests.integration.mail import (
    fetch_message_detail,
    fetch_messages,
    wait_for_message,
)

pytestmark = pytest.mark.integration
# ``stack_endpoints`` + ``clean_inbox`` (dev-stack reachability skip guard
# and inbox purge) live in the shared ``tests.integration.mail`` plugin.
pytest_plugins = ["tests.integration.mail"]

_RECOVERY_REQUEST_PATH = "/api/v1/recover/passkey/request"
_RECOVERY_VERIFY_PATH = "/api/v1/recover/passkey/verify"
_RECOVERY_PASSKEY_START_PATH = "/api/v1/recover/passkey/start"


def _post_json(
    url: str, body: dict[str, Any], *, timeout: float = 10.0
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
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


def _request_recovery_or_skip(app_url: str, email: str) -> dict[str, Any]:
    status, body = _post_json(
        f"{app_url}{_RECOVERY_REQUEST_PATH}",
        {"email": email},
    )
    if status == 429:
        pytest.skip(
            "recovery per-IP or per-email rate limit tripped on the dev stack "
            f"(body={body!r}); rerun after the bucket drains"
        )
    assert status == 202, f"unexpected recovery request status; body={body!r}"
    assert body == {"status": "accepted"}
    return body


def _run_dev_login_or_skip(*, email: str, workspace: str, role: str) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not on PATH; cannot seed dev-stack identity")
    cmd = [
        "docker",
        "compose",
        "-f",
        "docker-compose.dev.yml",
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
        role,
        "--output",
        "json",
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
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        pytest.skip(f"dev-login returned non-JSON output: {result.stdout!r}; {exc!r}")
    if not isinstance(payload, dict) or payload.get("name") != "__Host-crewday_session":
        pytest.skip(f"dev-login returned unexpected payload: {payload!r}")


def _seed_worker_or_skip(email: str, workspace: str) -> None:
    # ``dev_login`` bootstraps a fresh workspace through the owner path.
    # Create the workspace with a separate owner first; adding this email
    # to an existing workspace with ``--role worker`` then produces a
    # non-step-up recovery target.
    owner_email = f"recover-mailpit-owner-{uuid.uuid4()}@dev.local"
    _run_dev_login_or_skip(email=owner_email, workspace=workspace, role="owner")
    _run_dev_login_or_skip(email=email, workspace=workspace, role="worker")


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}@dev.local"


def _extract_recovery_url(body_text: str) -> str:
    for raw_line in body_text.splitlines():
        line = raw_line.strip()
        if line.startswith("http") and "/recover/enroll?token=" in line:
            return line
    raise AssertionError(f"no /recover/enroll URL found in body:\n{body_text!r}")


def _token_from_recovery_url(recovery_url: str) -> str:
    parsed = urllib.parse.urlparse(recovery_url)
    if parsed.path != "/recover/enroll":
        raise AssertionError(
            f"unexpected recovery URL path {parsed.path!r}; expected /recover/enroll"
        )
    params = urllib.parse.parse_qs(parsed.query)
    tokens = params.get("token", [])
    if len(tokens) != 1 or not tokens[0]:
        raise AssertionError(f"unexpected recovery URL query: {parsed.query!r}")
    return tokens[0]


def _message_matches_recipient(item: dict[str, Any], address: str) -> bool:
    to_records = item.get("To")
    if not isinstance(to_records, list):
        return False
    target = address.casefold()
    return any(
        isinstance(record, dict)
        and isinstance(record.get("Address"), str)
        and record["Address"].casefold() == target
        for record in to_records
    )


def _wait_for_no_message_to(
    mailpit_url: str,
    *,
    email: str,
    deadline_s: float = 2.0,
    poll_interval_s: float = 0.2,
) -> None:
    end = time.monotonic() + deadline_s
    matching_messages: list[dict[str, Any]] = []
    while time.monotonic() < end:
        matching_messages = [
            item
            for item in fetch_messages(mailpit_url)
            if _message_matches_recipient(item, email)
        ]
        if matching_messages:
            break
        time.sleep(poll_interval_s)
    assert matching_messages == [], f"Mailpit received mail for {email!r}"


def test_known_worker_recovery_email_verifies_and_starts_passkey(
    clean_inbox: tuple[str, str],
) -> None:
    app_url, mailpit_url = clean_inbox
    email = _unique_email("recover-mailpit-worker")
    workspace = f"recover-{uuid.uuid4().hex[:12]}"
    _seed_worker_or_skip(email, workspace)

    _request_recovery_or_skip(app_url, email)

    envelope = wait_for_message(mailpit_url, to=email)
    assert envelope["Subject"] == "crew.day — recover your account"
    internal_id = envelope["ID"]
    assert isinstance(internal_id, str) and internal_id

    detail = fetch_message_detail(mailpit_url, internal_id)
    text_body = detail.get("Text")
    assert isinstance(text_body, str) and text_body
    assert "completing recovery revokes every existing passkey" in text_body

    token = _token_from_recovery_url(_extract_recovery_url(text_body))
    assert len(token) >= 32

    verify_url = (
        f"{app_url}{_RECOVERY_VERIFY_PATH}?{urllib.parse.urlencode({'token': token})}"
    )
    status, verified = _get_json(verify_url)
    assert status == 200, f"unexpected verify status; body={verified!r}"
    recovery_session_id = verified.get("recovery_session_id")
    assert isinstance(recovery_session_id, str) and len(recovery_session_id) == 26

    status, started = _post_json(
        f"{app_url}{_RECOVERY_PASSKEY_START_PATH}",
        {"recovery_session_id": recovery_session_id},
    )
    assert status == 200, f"unexpected passkey start status; body={started!r}"
    assert isinstance(started.get("challenge_id"), str) and started["challenge_id"]
    assert isinstance(started.get("options"), dict) and started["options"]

    replay_status, replay_body = _get_json(verify_url)
    assert replay_status == 409, f"replay should be already_consumed; {replay_body!r}"
    assert replay_body.get("error") == "already_consumed"


def test_unknown_email_response_matches_and_sends_no_mail(
    clean_inbox: tuple[str, str],
) -> None:
    app_url, mailpit_url = clean_inbox
    known_email = _unique_email("recover-mailpit-known")
    unknown_email = _unique_email("recover-mailpit-unknown")
    workspace = f"recover-{uuid.uuid4().hex[:12]}"
    _seed_worker_or_skip(known_email, workspace)

    happy_body = _request_recovery_or_skip(app_url, known_email)
    known_envelope = wait_for_message(mailpit_url, to=known_email)
    assert known_envelope["Subject"] == "crew.day — recover your account"

    miss_body = _request_recovery_or_skip(app_url, unknown_email)
    assert miss_body == happy_body

    _wait_for_no_message_to(mailpit_url, email=unknown_email, deadline_s=3.0)
