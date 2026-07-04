"""Mailpit polling helpers for integration tests.

A handful of integration tests sit on top of a real Mailpit sink and
need to assert on the delivered envelope: ``test_mail_smtp.py`` for the
:class:`SMTPMailer` adapter, ``auth/test_magic_link_mailpit.py`` for the
end-to-end magic-link round-trip, and the upcoming signup / recovery /
quote round-trips queued behind cd-m1ls / cd-3ld1 / cd-yff4.

Without a shared helper the polling loop, header endpoint, and detail
endpoint get copy-pasted four times — which is exactly what cd-o62m's
acceptance criteria forbids ("do not copy-paste the polling loop four
times"). This module owns the contract; the callers stay short.

All public helpers take an ``api_url`` (e.g. ``http://127.0.0.1:8026``)
so the same code drives both an in-stack Mailpit (the dev compose stack
publishes to ``127.0.0.1:8026``) and a per-test :mod:`testcontainers`
Mailpit (random host port).

See ``docs/specs/10-messaging-notifications.md`` §"Transport" and
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import pytest

__all__ = [
    "DEFAULT_DEADLINE_S",
    "MailpitMessage",
    "app_reachable",
    "app_url",
    "clean_inbox",
    "clean_mailpit",
    "fetch_headers",
    "fetch_message_detail",
    "fetch_messages",
    "is_reachable",
    "mailpit_test_lock",
    "mailpit_url",
    "purge_inbox",
    "readyz_failures",
    "stack_endpoints",
    "wait_for_http",
    "wait_for_message",
]


DEFAULT_DEADLINE_S: Final[float] = 10.0
_MAILPIT_LOCK_PATH = Path("/tmp/crewday-mailpit-tests.lock")
_DEFAULT_APP_URL: Final[str] = "http://127.0.0.1:8100"
_DEFAULT_MAILPIT_URL: Final[str] = "http://127.0.0.1:8026"


# Re-exporting Mailpit's ``messages`` array element shape under a name
# the call sites can read. The actual JSON has no fixed schema we
# control, so :class:`dict[str, Any]` is the honest type — callers
# narrow on the specific keys they touch (``MessageID``, ``Subject``,
# ``ID``) just like the existing test_mail_smtp.py does.
MailpitMessage = dict[str, Any]


@contextmanager
def mailpit_test_lock() -> Iterator[None]:
    with _MAILPIT_LOCK_PATH.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def fetch_messages(api_url: str, *, timeout: float = 5.0) -> list[MailpitMessage]:
    """Return the ``messages`` array from Mailpit's ``/api/v1/messages``.

    Mailpit's JSON shape — ``{"total": N, "messages": [...]}`` — is
    documented and stable; the runtime ``isinstance`` guards keep mypy
    honest without pretending we know more than we do about
    third-party JSON.
    """
    with urllib.request.urlopen(f"{api_url}/api/v1/messages", timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"Mailpit returned non-object payload: {payload!r}")
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        raise AssertionError(f"Mailpit 'messages' is not a list: {messages!r}")
    return [item for item in messages if isinstance(item, dict)]


def fetch_headers(
    api_url: str, internal_id: str, *, timeout: float = 5.0
) -> dict[str, list[str]]:
    """Return Mailpit's per-message header map (``/api/v1/message/{id}/headers``).

    Mailpit normalises header keys to ``Canonical-Case`` and yields
    each header's values as a list of strings — exactly the shape the
    caller wants for ``Reply-To`` / ``X-…`` assertions.
    """
    with urllib.request.urlopen(
        f"{api_url}/api/v1/message/{internal_id}/headers", timeout=timeout
    ) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"Mailpit headers payload is not a dict: {payload!r}")
    out: dict[str, list[str]] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, list):
            continue
        str_values = [v for v in value if isinstance(v, str)]
        out[key] = str_values
    return out


def fetch_message_detail(
    api_url: str, internal_id: str, *, timeout: float = 5.0
) -> dict[str, Any]:
    """Return Mailpit's full message detail (``/api/v1/message/{id}``).

    Includes ``Text`` and ``HTML`` rendered bodies — the caller asserts
    on body content (e.g. magic-link URL inside the plain-text body).
    """
    with urllib.request.urlopen(
        f"{api_url}/api/v1/message/{internal_id}", timeout=timeout
    ) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"Mailpit detail payload is not a dict: {payload!r}")
    return payload


def wait_for_message(
    api_url: str,
    *,
    message_id: str | None = None,
    to: str | None = None,
    deadline_s: float = DEFAULT_DEADLINE_S,
    poll_interval_s: float = 0.2,
) -> MailpitMessage:
    """Poll Mailpit until a matching envelope arrives, then return it.

    Exactly one of ``message_id`` or ``to`` must be supplied:

    * ``message_id`` — match the top-level ``MessageID`` field
      (angle-brackets-stripped). This is the strongest match: the
      caller minted the ID themselves (e.g. via ``SMTPMailer.send``'s
      return value) and wants to assert against *this* send,
      independent of inbox ordering.
    * ``to`` — match the recipient's ``Address`` on the first ``To``
      record. Right shape when the caller drove a flow that mints the
      Message-ID server-side and only the recipient is known up
      front (the magic-link bootstrap path).

    Raises :class:`AssertionError` if no envelope matches within
    ``deadline_s``. The default 10 s deadline matches what the
    existing :mod:`test_mail_smtp` test used; adjust on a flow with a
    deliberately slower mailer.
    """
    if (message_id is None) == (to is None):
        raise ValueError("wait_for_message requires exactly one of message_id= or to=")
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        items = fetch_messages(api_url)
        for item in items:
            if message_id is not None and item.get("MessageID") == message_id:
                return item
            if to is not None and _matches_recipient(item, to):
                return item
        time.sleep(poll_interval_s)
    selector = f"MessageID={message_id!r}" if message_id is not None else f"to={to!r}"
    raise AssertionError(
        f"Mailpit at {api_url} never received a message matching {selector} "
        f"within {deadline_s}s"
    )


def _matches_recipient(item: MailpitMessage, address: str) -> bool:
    """Return ``True`` when ``item['To']`` contains ``address``.

    Mailpit stores ``To`` as a list of ``{"Name", "Address"}`` records;
    we only match on ``Address`` (lower-cased) so a caller passing
    ``"Alice@Example.com"`` still matches the canonical form Mailpit
    stores. ``EmailAddress``-style mailbox parsing is overkill here —
    the helper's contract is "did this address receive an email", and
    case-insensitive equality covers it.
    """
    to_records = item.get("To")
    if not isinstance(to_records, list):
        return False
    target = address.casefold()
    for record in to_records:
        if not isinstance(record, dict):
            continue
        addr = record.get("Address")
        if isinstance(addr, str) and addr.casefold() == target:
            return True
    return False


def purge_inbox(api_url: str, *, timeout: float = 5.0) -> None:
    """Delete every message stored in Mailpit (``DELETE /api/v1/messages``).

    Test isolation knob: the dev-stack Mailpit is shared across
    sessions, so a test that asserts ``count == 1`` after sending one
    message will fail if a previous run left envelopes behind. Call
    this at the start of a test (or in a fixture) to start from a
    clean inbox.

    Mailpit returns ``200 OK`` with an empty body on success. On the
    rare path where the call fails (Mailpit down, transient HTTP error)
    we re-raise — the caller wanted a clean inbox and didn't get one,
    silencing the failure would let the test assert against stale
    state.
    """
    req = urllib.request.Request(
        f"{api_url}/api/v1/messages",
        method="DELETE",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # Drain the body so the connection returns to the pool cleanly.
        resp.read()


def is_reachable(api_url: str, *, timeout: float = 2.0) -> bool:
    """Return ``True`` when Mailpit's ``/livez`` answers 2xx.

    Used by integration tests to skip cleanly when the dev compose
    stack isn't up. Catches the union of network-layer failures
    (no listener, DNS, refused) so the caller writes a one-liner skip
    guard instead of a five-line ``except`` ladder.
    """
    try:
        with urllib.request.urlopen(f"{api_url}/livez", timeout=timeout) as resp:
            status = int(resp.status)
            resp.read()
    except urllib.error.URLError, ConnectionError, OSError:
        return False
    return 200 <= status < 300


def wait_for_http(api_url: str, *, deadline_s: float = 15.0) -> None:
    """Poll Mailpit's ``/livez`` until it returns 2xx or we time out.

    Right shape for the testcontainers fixture: the container is up
    well before Mailpit finishes binding its listeners, and an
    ``urlopen`` against a not-yet-open port throws
    :class:`ConnectionRefusedError`. For "is the dev stack up?" use
    :func:`is_reachable` instead — that one returns a bool rather than
    raising.
    """
    end = time.monotonic() + deadline_s
    last_exc: BaseException | None = None
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(f"{api_url}/livez", timeout=2) as resp:
                if 200 <= resp.status < 300:
                    resp.read()
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_exc = exc
        time.sleep(0.2)
    raise RuntimeError(
        f"Mailpit HTTP API at {api_url} never came up in {deadline_s}s "
        f"(last error: {last_exc!r})"
    )


# ---------------------------------------------------------------------------
# Dev-stack endpoint discovery + skip gating
# ---------------------------------------------------------------------------
#
# The dev-stack Mailpit round-trip tests (auth recovery / magic-link /
# billing quote) all share the same reachability + readiness skip guard
# and inbox-purge seeding. Rather than copy-paste the four-part boilerplate
# into each module, the fixtures below own the contract; the callers import
# ``stack_endpoints`` + ``clean_inbox`` (dev-stack HTTP flows) or
# ``clean_mailpit`` (an in-process client whose SMTP mailer targets Mailpit,
# e.g. the signup round-trip).


def app_url() -> str:
    """Return the dev-stack app-api base URL (``CREWDAY_TEST_APP_URL``)."""
    return os.environ.get("CREWDAY_TEST_APP_URL", _DEFAULT_APP_URL)


def mailpit_url() -> str:
    """Return the Mailpit HTTP API base URL (``CREWDAY_TEST_MAILPIT_URL``)."""
    return os.environ.get("CREWDAY_TEST_MAILPIT_URL", _DEFAULT_MAILPIT_URL)


def app_reachable(url: str, *, timeout: float = 2.0) -> bool:
    """Return ``True`` when ``GET {url}/healthz`` answers 2xx.

    The app-api factory mounts ``/healthz`` unconditionally, so a 2xx
    there is the cheapest unauthenticated "is the app serving?" probe.
    """
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=timeout) as resp:
            status = int(resp.status)
            resp.read()
    except urllib.error.URLError, ConnectionError, OSError:
        return False
    return 200 <= status < 300


def readyz_failures(url: str, *, timeout: float = 2.0) -> list[str] | None:
    """Return failing ``/readyz`` check symbols, or ``None`` when ready.

    A long-lived dev-stack container can drift behind the repo's
    migration head; ``/healthz`` still answers 200 (the ASGI server is
    up) but writes that touch a missing column fail at commit time,
    surfacing later as a confusing downstream error. Probing ``/readyz``
    lets the fixture distinguish "app down" from "app up but migrations
    behind / worker stalled" and skip with a precise remediation hint.

    Returns ``None`` on 200; on a 503 returns the ``checks[].check``
    symbols (e.g. ``["migrations"]``); on any network / parse failure
    returns a one-element fallback so the caller still skips coherently.
    """
    try:
        with urllib.request.urlopen(f"{url}/readyz", timeout=timeout) as resp:
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
    except ValueError, UnicodeDecodeError:
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
    """Yield ``(app_url, mailpit_url)`` after gating on the dev stack.

    Skips the whole module when the app-api is unreachable, not ready
    (``/readyz`` failing — the migration-drift trap gets a precise
    hint), or Mailpit is unreachable, so a host without the compose
    stack up records a clean skip rather than a noisy failure. Module
    scope: one reachability probe per module; per-test isolation is the
    function-scoped :func:`clean_inbox` purge.
    """
    resolved_app_url = app_url()
    resolved_mailpit_url = mailpit_url()
    if not app_reachable(resolved_app_url):
        pytest.skip(
            f"app-api not reachable at {resolved_app_url}; start the dev stack "
            "with `docker compose -f docker-compose.dev.yml up -d`"
        )
    failing_checks = readyz_failures(resolved_app_url)
    if failing_checks is not None:
        pytest.skip(
            f"app-api at {resolved_app_url} is not ready (failing: "
            f"{failing_checks}); if 'migrations' is listed, restart the dev "
            "stack — `docker compose -f docker-compose.dev.yml restart app-api` "
            "— to pick up new revisions"
        )
    if not is_reachable(resolved_mailpit_url):
        pytest.skip(
            f"Mailpit not reachable at {resolved_mailpit_url}; start the dev "
            "stack with `docker compose -f docker-compose.dev.yml up -d`"
        )
    yield resolved_app_url, resolved_mailpit_url


@pytest.fixture
def clean_inbox(stack_endpoints: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Purge Mailpit under the cross-worker lock before a dev-stack test.

    The dev-stack Mailpit persists between runs, so an earlier test or
    another agent's run can leave envelopes behind. Purging at fixture
    entry gives every test a known-empty inbox without coupling cases.
    """
    _, resolved_mailpit_url = stack_endpoints
    with mailpit_test_lock():
        purge_inbox(resolved_mailpit_url)
        yield stack_endpoints


@pytest.fixture
def clean_mailpit() -> Iterator[str]:
    """Skip when Mailpit is unreachable, purge the inbox, yield its URL.

    Right shape for tests driving an in-process client whose SMTP mailer
    targets the dev-stack Mailpit sink (the signup round-trip): they need
    a clean inbox but never touch the dev-stack app-api, so they gate on
    Mailpit reachability alone. The reachability probe runs inside the
    cross-worker lock so a concurrent purge cannot race the check.
    """
    resolved_mailpit_url = mailpit_url()
    with mailpit_test_lock():
        if not is_reachable(resolved_mailpit_url):
            pytest.skip(
                f"Mailpit not reachable at {resolved_mailpit_url}; start the "
                "dev stack with `docker compose -f docker-compose.dev.yml up "
                "-d --build`"
            )
        purge_inbox(resolved_mailpit_url)
        yield resolved_mailpit_url
