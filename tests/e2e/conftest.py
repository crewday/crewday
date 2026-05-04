"""Shared fixtures for the end-to-end Playwright suite (cd-ndmv).

Spec: ``docs/specs/17-testing-quality.md`` §"End-to-end" + §"Visual
regression" + §"360 px viewport sitemap".

Layered on top of ``pytest-playwright``'s built-in fixtures (``page``,
``context``, ``browser``, ``browser_type``); we only add the cross-
cutting concerns the spec calls out:

* **Base URL.** Read from ``CREWDAY_E2E_BASE_URL`` (default
  ``http://localhost:8100`` for WebAuthn RP-ID validity). The
  ``base_url`` fixture name is the one ``pytest-playwright`` already
  recognises — exporting it here lets every test reach the dev-stack
  loopback without hard-coding an URL.
* **Dev-stack readiness.** A session-scoped fixture pings ``/healthz``
  before any test runs; if the stack is down the suite skips loudly
  (the smoke message tells the developer to bring compose up). This
  is preferable to a parade of opaque connection-refused traces.
* **Tracing / video / screenshot.** ``pytest-playwright``'s
  ``--tracing`` / ``--video`` / ``--screenshot`` CLI flags drive
  artefact emission per §17 ("first failure → trace.zip artefact").
  The plugin defaults each to ``off`` / ``retain-on-failure`` /
  ``only-on-failure`` respectively, so the operator opts in via the
  CLI; ``tests/e2e/README.md`` pins the recommended invocation that
  turns all three on.
* **WebKit auto-skip.** Hosts missing libicu74 cannot launch the
  WebKit driver. We wrap pytest-playwright's ``launch_browser``
  fixture so the "Host system is missing dependencies" error
  surfaces as a focused ``pytest.skip`` (whole-suite when the user
  ran ``--browser webkit`` only, per-test parametrisation otherwise).
  ``tests/e2e/README.md`` documents the install hint; we surface
  the same hint at skip time.

The pytest-playwright defaults already cover screenshot + video on
failure; we extend the storage location so artefacts land beside the
suite (``tests/e2e/_artifacts/``) rather than the repo root.

The full top-level ``tests/conftest.py`` autouse log-isolation
fixture also applies here — Playwright tests don't touch logging
directly, so the harm-vs-help calculus is fine. We do NOT redeclare
the autouse fixture; pytest discovers it through the parent.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from playwright.sync_api import Browser, BrowserType
from playwright.sync_api import Error as PlaywrightError

__all__ = [
    "DEFAULT_BASE_URL",
    "ENV_BASE_URL",
    "base_url",
    "browser_context_args",
    "dev_stack_ready",
    "launch_browser",
]


# The Vite container binds to 127.0.0.1 only, but the e2e suite uses
# localhost so Chromium can complete WebAuthn registration: IP
# literals are not valid RP IDs. This is still loopback-only; the
# public ``dev.crew.day`` host remains blocked by Pangolin badger
# forward-auth for scripted verification.
DEFAULT_BASE_URL: Final[str] = "http://localhost:8100"
ENV_BASE_URL: Final[str] = "CREWDAY_E2E_BASE_URL"

# Where Playwright drops trace.zip / screenshots / videos when its
# ``--tracing`` / ``--screenshot`` / ``--video`` flags fire. Keeps
# artefacts under the suite directory so a CI ``upload-artifact`` step
# only needs to point at one path; ``--output`` is the
# pytest-playwright knob that wires both pieces into the same dir.
_ARTIFACTS_DIR: Final[Path] = Path(__file__).parent / "_artifacts"


def pytest_configure(config: pytest.Config) -> None:
    """Default ``--output`` to the e2e artefact dir if the user hasn't.

    ``pytest-playwright``'s ``--output`` flag controls where it writes
    trace.zip / screenshots / videos when the matching
    ``--tracing`` / ``--screenshot`` / ``--video`` modes fire. Setting
    a per-suite default keeps the artefact tree predictable for both
    local runs and the CI ``upload-artifact`` step (cd-ndmv follow-up).
    """
    output = config.getoption("--output", default=None)
    if not output:
        _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        config.option.output = str(_ARTIFACTS_DIR)


@pytest.fixture(scope="session")
def base_url() -> str:
    """Origin under test. ``pytest-playwright`` consumes this fixture name.

    Resolves to the env var when set, otherwise the loopback default.
    Tests address the SPA via ``page.goto(f"{base_url}/today")`` and
    so on; the helpers in ``_helpers/`` accept the value through their
    ``base_url`` parameter to stay framework-agnostic.
    """
    return os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL).rstrip("/")


@pytest.fixture(scope="session")
def dev_stack_ready(base_url: str) -> str:
    """Ping ``/healthz`` once per session; skip the suite if the stack is down.

    The dev stack runs in Docker via ``mocks/docker-compose.yml``;
    AGENTS.md §"Bring the dev stack up" describes the bring-up. A
    crashed ``app-api`` returns 502 through the Vite proxy, which the
    test would otherwise see as a parade of opaque
    ``net::ERR_*`` traces inside Playwright. We surface the cause once,
    upfront, with the right hint.

    Returns ``base_url`` for chaining (``def test_foo(dev_stack_ready):
    page.goto(dev_stack_ready + "/today")``).
    """
    healthz = f"{base_url}/healthz"
    try:
        with urllib.request.urlopen(healthz, timeout=5) as resp:
            resp.read()
            if resp.status != 200:
                pytest.skip(
                    f"dev stack /healthz returned {resp.status}; "
                    "run `docker compose -f mocks/docker-compose.yml "
                    "-f mocks/docker-compose.e2e.yml up -d --build`"
                )
    except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
        pytest.skip(
            f"dev stack unreachable at {base_url} ({exc!r}); "
            "run `docker compose -f mocks/docker-compose.yml "
            "-f mocks/docker-compose.e2e.yml up -d --build`"
        )
    _assert_webauthn_rp_id_matches_origin(base_url)
    return base_url


def _assert_webauthn_rp_id_matches_origin(base_url: str) -> None:
    """Fail early when e2e runs against a non-e2e WebAuthn config."""
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or ""
    if not host:
        pytest.fail(f"CREWDAY_E2E_BASE_URL has no hostname: {base_url!r}")

    url = f"{base_url}/api/v1/auth/passkey/login/start"
    request = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
        pytest.skip(
            f"dev stack WebAuthn preflight failed at {url} ({exc!r}); "
            "run `docker compose -f mocks/docker-compose.yml "
            "-f mocks/docker-compose.e2e.yml up -d --build`"
        )
    rp_id = payload.get("options", {}).get("rpId")
    if not isinstance(rp_id, str) or not rp_id:
        pytest.fail(f"dev stack WebAuthn preflight returned no rpId: {payload!r}")
    if rp_id == host or host.endswith(f".{rp_id}"):
        return
    pytest.fail(
        f"WebAuthn rp_id {rp_id!r} does not match e2e origin host {host!r}. "
        "The e2e suite must run with the loopback override: "
        "`docker compose -f mocks/docker-compose.yml "
        "-f mocks/docker-compose.e2e.yml up -d --build`. "
        f"When {ENV_BASE_URL}=http://localhost:8100, the stack must advertise "
        "CREWDAY_PUBLIC_URL=http://localhost:8100 and "
        "CREWDAY_WEBAUTHN_RP_ID=localhost."
    )


@pytest.fixture
def browser_context_args(
    browser_context_args: dict[str, object],
    base_url: str,
) -> dict[str, object]:
    """Override ``pytest-playwright``'s context kwargs.

    Pre-seeds ``base_url`` so ``page.goto("/today")`` resolves against
    the dev-stack loopback. Other defaults (locale, timezone) stay on
    Playwright's auto values to mirror what a real worker / manager
    sees.

    The ``# noqa: F811`` is the documented way to extend the upstream
    fixture — pytest's fixture resolution merges the dict, but mypy
    sees the redeclaration. The signature mirrors pytest-playwright's
    own:
    https://playwright.dev/python/docs/test-runners#fixtures.
    """
    return {
        **browser_context_args,
        "base_url": base_url,
        # 360x780 is the §17 "360 px viewport sitemap" mobile target;
        # most pilot journeys use the desktop default but we set it
        # here so the mobile-walk test can override per-test. Default
        # to Playwright's standard 1280x720 desktop until then.
    }


@pytest.fixture(autouse=True)
def _require_dev_stack_for_e2e(dev_stack_ready: str) -> Iterator[None]:
    """Force ``dev_stack_ready`` to run before any e2e test.

    Tests don't always declare ``dev_stack_ready`` directly — the
    helpers consume ``base_url`` instead — so the readiness probe
    needs an autouse hook to gate every test in the directory. Without
    it the first test against a down stack would yield Playwright
    timeouts, not the focused "bring the stack up" hint.
    """
    del dev_stack_ready  # consumed for its side effect (readiness probe)
    yield


# ``pytest_playwright``'s missing-deps message ships inside an
# ``Error`` from ``BrowserType.launch``; we substring-match the stable
# leading sentence so the wrapper survives Playwright's banner edits.
_MISSING_DEPS_MARKER: Final[str] = "Host system is missing dependencies"


@pytest.fixture(scope="session")
def launch_browser(
    browser_type_launch_args: dict[str, Any],
    browser_type: BrowserType,
    connect_options: dict[str, Any] | None,
) -> Callable[..., Browser]:
    """Wrap pytest-playwright's launcher to convert missing-deps to ``skip``.

    ``tests/e2e/README.md`` documents that WebKit needs ``libicu74``
    (and friends) installed before the driver can launch. Without
    this wrapper the missing-deps error becomes a pytest fixture
    ERROR, not a SKIP — turning a documented dev-host limitation
    into a noisy red bar that can mask real regressions.

    We mirror :func:`pytest_playwright.pytest_playwright.launch_browser`
    1:1 (including the ``connect_options`` branch that supports the
    ``--browser-channel`` remote-runner mode) and only add the
    skip-on-missing-deps adapter. The substring marker ``Host system
    is missing dependencies`` is stable across Playwright releases —
    it leads the multi-line banner that points the operator at
    ``sudo playwright install-deps`` / ``apt-get install libicu74``.
    """

    def launch(**kwargs: Any) -> Browser:
        launch_options = {**browser_type_launch_args, **kwargs}
        try:
            if connect_options:
                return browser_type.connect(
                    **{
                        **connect_options,
                        "headers": {
                            "x-playwright-launch-options": json.dumps(launch_options),
                            **(connect_options.get("headers") or {}),
                        },
                    }
                )
            return browser_type.launch(**launch_options)
        except PlaywrightError as exc:
            if _MISSING_DEPS_MARKER in str(exc):
                pytest.skip(
                    f"{browser_type.name} driver missing host dependencies "
                    "(install with `sudo playwright install-deps` or "
                    "`apt-get install libicu74 libxml2`); skipping per "
                    "tests/e2e/README.md."
                )
            raise

    return launch
