"""Unit tests for ``tests/e2e/_helpers`` pure-Python pieces.

The e2e suite's helpers ship a few pieces of logic that are pure-Python
and unit-testable without Playwright or the dev stack:

* :func:`tests.e2e._helpers.auth.extract_magic_link_token` — regex
  extraction over an email body. The regex is the only fragile part
  of the magic-link round-trip (a future template tweak that nests
  the URL in something the regex doesn't match would silently break
  every magic-link e2e flow). Covering it here means the regression
  surfaces in the cheap unit suite, not the slow Playwright run.
* :func:`tests.e2e._helpers.auth._envelope_matches_recipient` /
  :func:`tests.e2e._helpers.auth._extract_to_addresses` — type-narrowing
  helpers over Mailpit's ``dict[str, Any]`` listing payloads. Pinned
  here so a Mailpit API drift fails the unit suite first.
* :func:`tests.e2e._helpers.sitemap.load_authenticated_routes` —
  reads the SPA build manifest (``app/web/dist/_surface.json``) and
  falls back to :data:`PILOT_AUTHENTICATED_ROUTES` when the file is
  missing or malformed. The fallback is the safety net that keeps
  the §17 360 px walker running when the SPA hasn't been built.

Spec: ``docs/specs/17-testing-quality.md`` §"End-to-end" (the e2e
helpers themselves are infrastructure for §17's GA journey suite).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from playwright.sync_api import BrowserContext

from tests.e2e._helpers.auth import (
    MailpitMessage,
    _envelope_matches_recipient,
    _extract_to_addresses,
    extract_magic_link_token,
    login_with_dev_session,
)
from tests.e2e._helpers.sitemap import (
    PILOT_AUTHENTICATED_ROUTES,
    REQUIRE_SURFACE_MANIFEST_ENV,
    load_authenticated_routes,
)


def _msg(text: str = "", html: str = "") -> MailpitMessage:
    """Build a :class:`MailpitMessage` for token-extraction tests.

    Other fields (``id``, ``subject``, recipients) don't influence the
    extractor; left blank to keep the test cases scannable.
    """
    return MailpitMessage(
        id="msg-id",
        subject="",
        body_text=text,
        body_html=html,
        to_addresses=(),
    )


class TestExtractMagicLinkToken:
    """:func:`extract_magic_link_token` regex behaviour."""

    def test_extracts_from_plain_text_body(self) -> None:
        """Canonical signup-template shape: HTTPS dev-host URL in plain text."""
        body = (
            "Welcome to crew.day.\n"
            "Click here to verify your email and finish signing up:\n"
            "https://dev.crew.day/auth/magic/abc.def-123_xyz\n"
        )
        token = extract_magic_link_token(_msg(text=body))
        assert token == "abc.def-123_xyz"

    def test_falls_back_to_html_body_when_text_empty(self) -> None:
        """HTML-only template path — older templates omit the text alt."""
        body = '<a href="https://dev.crew.day/auth/magic/HtmlOnlyToken_42">Verify</a>'
        token = extract_magic_link_token(_msg(html=body))
        assert token == "HtmlOnlyToken_42"

    def test_prefers_plain_text_when_both_present(self) -> None:
        """Plain text wins to keep the extractor stable across rich-text edits.

        A future HTML rewrite that nests the URL in an aliased anchor
        would still leave the plain-text body untouched; pinning text
        precedence shields the helper from those tweaks.
        """
        text = "Verify: https://dev.crew.day/auth/magic/text-token"
        html = '<a href="https://dev.crew.day/auth/magic/html-token">x</a>'
        token = extract_magic_link_token(_msg(text=text, html=html))
        assert token == "text-token"

    def test_accepts_plain_http_self_hosted_origin(self) -> None:
        """A self-hosted ``CREWDAY_PUBLIC_URL`` may use plain HTTP."""
        body = "http://crewday.lan/auth/magic/self-hosted-tok"
        token = extract_magic_link_token(_msg(text=body))
        assert token == "self-hosted-tok"

    def test_raises_on_missing_url(self) -> None:
        """Body without a magic-link URL surfaces a focused error.

        Without this guard the caller would hit ``IndexError`` deep in
        the consume chain; the focused message tells the developer the
        email template just dropped the URL.
        """
        body = "Welcome — your account is pending review."
        with pytest.raises(RuntimeError, match="no token URL"):
            extract_magic_link_token(_msg(text=body))

    def test_raises_when_both_bodies_empty(self) -> None:
        """Empty body is the same failure mode as a missing URL."""
        with pytest.raises(RuntimeError, match="no token URL"):
            extract_magic_link_token(_msg())

    def test_token_charset_matches_base64url(self) -> None:
        """The extractor accepts the full base64url + dot alphabet.

        ``app.auth.magic_link`` mints tokens as base64url-encoded
        nonces; the regex must accept ``A-Za-z0-9_-`` plus ``.`` for
        the rare padding shape. A char outside that set must NOT
        extend the captured token.
        """
        body = "https://dev.crew.day/auth/magic/aA0_-.X then garbage"
        token = extract_magic_link_token(_msg(text=body))
        assert token == "aA0_-.X"


class TestEnvelopeMatchesRecipient:
    """:func:`_envelope_matches_recipient` Mailpit envelope shape."""

    def test_matches_canonical_lowercase(self) -> None:
        """Mailpit normalises addresses to lower-case."""
        envelope = {"To": [{"Address": "alice@example.com"}]}
        assert _envelope_matches_recipient(envelope, "alice@example.com")

    def test_matches_case_insensitively(self) -> None:
        """Caller may pass mixed-case addresses; case-fold compares both sides."""
        envelope = {"To": [{"Address": "alice@example.com"}]}
        assert _envelope_matches_recipient(envelope, "Alice@Example.com".casefold())

    def test_returns_false_on_mismatch(self) -> None:
        """A different recipient is not a match."""
        envelope = {"To": [{"Address": "bob@example.com"}]}
        assert not _envelope_matches_recipient(envelope, "alice@example.com")

    def test_handles_missing_to_field(self) -> None:
        """Mailpit may omit ``To`` for system-generated entries."""
        assert not _envelope_matches_recipient({}, "alice@example.com")

    def test_handles_non_list_to_field(self) -> None:
        """Defensive: a string ``To`` value (API drift) is ignored, not crashed on."""
        assert not _envelope_matches_recipient(
            {"To": "alice@example.com"}, "alice@example.com"
        )

    def test_skips_non_dict_records(self) -> None:
        """Mixed-shape ``To`` lists drop bad records, keep valid ones."""
        envelope = {
            "To": [
                "garbage",
                {"Address": "alice@example.com"},
            ]
        }
        assert _envelope_matches_recipient(envelope, "alice@example.com")


class TestExtractToAddresses:
    """:func:`_extract_to_addresses` Mailpit detail shape."""

    def test_extracts_addresses_from_record_list(self) -> None:
        """Canonical Mailpit detail shape: list of ``{"Name", "Address"}``."""
        raw = [
            {"Name": "Alice", "Address": "alice@example.com"},
            {"Name": "Bob", "Address": "bob@example.com"},
        ]
        assert _extract_to_addresses(raw) == ("alice@example.com", "bob@example.com")

    def test_returns_empty_tuple_on_non_list(self) -> None:
        """API drift returning a non-list maps to empty, not a crash."""
        assert _extract_to_addresses(None) == ()
        assert _extract_to_addresses("alice@example.com") == ()
        assert _extract_to_addresses({"Address": "alice@example.com"}) == ()

    def test_skips_records_without_address(self) -> None:
        """A record missing ``Address`` (CC group, BCC, …) is dropped silently."""
        raw = [
            {"Name": "Alice"},
            {"Address": "bob@example.com"},
        ]
        assert _extract_to_addresses(raw) == ("bob@example.com",)

    def test_skips_non_string_address(self) -> None:
        """Defensive: a non-string ``Address`` (numeric API drift) is filtered out."""
        raw = [
            {"Address": 12345},
            {"Address": "bob@example.com"},
        ]
        assert _extract_to_addresses(raw) == ("bob@example.com",)


class _FakeContext:
    def __init__(self) -> None:
        self.cookies: list[dict[str, object]] = []

    def add_cookies(self, cookies: list[dict[str, object]]) -> None:
        self.cookies.extend(cookies)


class TestLoginWithDevSession:
    def test_mints_fresh_dev_login_cookie_for_same_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated calls mint fresh rows so a logout cannot poison later tests."""
        calls: list[list[str]] = []

        def fake_run(
            cmd: list[str],
            *,
            capture_output: bool,
            text: bool,
            check: bool,
            timeout: int,
        ) -> SimpleNamespace:
            calls.append(cmd)
            return SimpleNamespace(
                stdout=f"__Host-crewday_session=value-{len(calls)}\n",
                stderr="",
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

        first = _FakeContext()
        second = _FakeContext()
        login_with_dev_session(
            cast(BrowserContext, first),
            base_url="http://127.0.0.1:8100",
            email="same@dev.local",
            workspace_slug="same",
            role="owner",
            service="app-api",
            compose_file=Path("compose.yml"),
        )
        login_with_dev_session(
            cast(BrowserContext, second),
            base_url="http://127.0.0.1:8100",
            email="same@dev.local",
            workspace_slug="same",
            role="owner",
            service="app-api",
            compose_file=Path("compose.yml"),
        )

        assert len(calls) == 2
        assert first.cookies[0]["value"] == "value-1"
        assert second.cookies[0]["value"] == "value-2"


class TestLoadAuthenticatedRoutes:
    """:func:`load_authenticated_routes` build-manifest reader.

    The helper underpins the §17 360 px sitemap walker: the production
    Vite plugin emits ``app/web/dist/_surface.json``, the helper reads
    it at test time, and the test consumes the route list. The
    fallback to :data:`PILOT_AUTHENTICATED_ROUTES` keeps the e2e suite
    runnable when the SPA hasn't been built — pinning the four
    failure modes here means a regression in that fallback surfaces
    in the unit suite, not the slow Playwright run.
    """

    def test_returns_routes_from_valid_manifest(self, tmp_path: Path) -> None:
        """A well-formed manifest's ``authenticated`` list comes back as a tuple."""
        manifest = tmp_path / "_surface.json"
        manifest.write_text(
            '{"version": 1, "authenticated": ["/today", "/dashboard"]}',
            encoding="utf-8",
        )
        assert load_authenticated_routes(manifest_path=manifest) == (
            "/today",
            "/dashboard",
        )

    def test_falls_back_when_manifest_missing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing file → fallback to pilot list, with a WARNING log line."""
        missing = tmp_path / "does-not-exist.json"
        with caplog.at_level(logging.WARNING, logger="tests.e2e._helpers.sitemap"):
            routes = load_authenticated_routes(manifest_path=missing)
        assert routes == PILOT_AUTHENTICATED_ROUTES
        assert any("not found" in rec.message for rec in caplog.records)

    def test_strict_mode_raises_when_manifest_missing(self, tmp_path: Path) -> None:
        """Strict mode is CI's guard against silently using the smoke subset."""
        missing = tmp_path / "does-not-exist.json"
        with pytest.raises(RuntimeError, match="fallback disabled"):
            load_authenticated_routes(
                manifest_path=missing,
                require_manifest=True,
            )

    def test_env_strict_mode_raises_when_manifest_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CI enables strict mode through an env var, not a test-code branch."""
        monkeypatch.setenv(REQUIRE_SURFACE_MANIFEST_ENV, "1")
        missing = tmp_path / "does-not-exist.json"
        with pytest.raises(RuntimeError, match="fallback disabled"):
            load_authenticated_routes(manifest_path=missing)

    def test_falls_back_on_malformed_json(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Invalid JSON → fallback (don't raise)."""
        manifest = tmp_path / "_surface.json"
        manifest.write_text("{not valid json", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="tests.e2e._helpers.sitemap"):
            routes = load_authenticated_routes(manifest_path=manifest)
        assert routes == PILOT_AUTHENTICATED_ROUTES
        assert any("not valid JSON" in rec.message for rec in caplog.records)

    def test_falls_back_when_authenticated_key_missing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Valid JSON without ``authenticated`` → fallback."""
        manifest = tmp_path / "_surface.json"
        manifest.write_text('{"version": 1, "public": ["/login"]}', encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="tests.e2e._helpers.sitemap"):
            routes = load_authenticated_routes(manifest_path=manifest)
        assert routes == PILOT_AUTHENTICATED_ROUTES
        assert any(
            "missing/invalid 'authenticated' list" in rec.message
            for rec in caplog.records
        )

    def test_falls_back_when_authenticated_list_is_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An empty manifest would make the walker visit zero routes."""
        manifest = tmp_path / "_surface.json"
        manifest.write_text('{"version": 1, "authenticated": []}', encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="tests.e2e._helpers.sitemap"):
            routes = load_authenticated_routes(manifest_path=manifest)
        assert routes == PILOT_AUTHENTICATED_ROUTES
        assert any(
            "has no authenticated routes" in rec.message for rec in caplog.records
        )

    def test_strict_mode_rejects_pilot_sized_manifest(self, tmp_path: Path) -> None:
        """CI should read the full SPA surface, not a generated 5-route subset."""
        manifest = tmp_path / "_surface.json"
        manifest.write_text(
            '{"version": 1, "authenticated": ['
            '"/today", "/schedule", "/dashboard", "/properties", "/employees"'
            "]}",
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="pilot smoke subset"):
            load_authenticated_routes(
                manifest_path=manifest,
                require_manifest=True,
            )

    def test_falls_back_when_authenticated_routes_are_not_paths(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Manifest route entries must be SPA paths, not relative or absolute URLs."""
        manifest = tmp_path / "_surface.json"
        manifest.write_text(
            '{"version": 1, "authenticated": ["/today", "dashboard"]}',
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger="tests.e2e._helpers.sitemap"):
            routes = load_authenticated_routes(manifest_path=manifest)
        assert routes == PILOT_AUTHENTICATED_ROUTES
        assert any("non-path entries" in rec.message for rec in caplog.records)

    def test_falls_back_when_authenticated_routes_have_duplicates(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Duplicate routes can make a truncated manifest look large enough."""
        manifest = tmp_path / "_surface.json"
        manifest.write_text(
            '{"version": 1, "authenticated": ["/today", "/today"]}',
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger="tests.e2e._helpers.sitemap"):
            routes = load_authenticated_routes(manifest_path=manifest)
        assert routes == PILOT_AUTHENTICATED_ROUTES
        assert any("duplicate routes" in rec.message for rec in caplog.records)

    def test_falls_back_when_top_level_is_not_object(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A JSON list at the top level → fallback (defensive narrowing)."""
        manifest = tmp_path / "_surface.json"
        manifest.write_text('["/today"]', encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="tests.e2e._helpers.sitemap"):
            routes = load_authenticated_routes(manifest_path=manifest)
        assert routes == PILOT_AUTHENTICATED_ROUTES
        assert any("not a JSON object" in rec.message for rec in caplog.records)

    def test_falls_back_on_non_string_route_entries(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Manifest with a non-string entry → fallback (caught by isinstance check)."""
        manifest = tmp_path / "_surface.json"
        manifest.write_text(
            '{"version": 1, "authenticated": ["/today", 42]}',
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger="tests.e2e._helpers.sitemap"):
            routes = load_authenticated_routes(manifest_path=manifest)
        assert routes == PILOT_AUTHENTICATED_ROUTES
        assert any("non-string entries" in rec.message for rec in caplog.records)

    def test_default_path_resolves_under_app_web_dist(self) -> None:
        """The module-level default path points at ``app/web/dist/_surface.json``."""
        from tests.e2e._helpers.sitemap import SURFACE_MANIFEST_PATH

        # Don't assert the file exists (CI may run before the SPA is
        # built); just pin the resolved location relative to the repo
        # root so a directory-restructure surfaces here rather than
        # silently breaking the live walker.
        parts = SURFACE_MANIFEST_PATH.parts
        assert parts[-4:] == ("app", "web", "dist", "_surface.json")
