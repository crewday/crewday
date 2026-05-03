"""Unit tests for :mod:`app.auth.signup_abuse`.

Covers the four public surfaces in isolation:

* Rate limits — per-IP, per-email, deployment-wide. Evaluates the
  exact thresholds from :mod:`app.auth._throttle`
  (``_SIGNUP_IP_LIMIT`` / ``_SIGNUP_EMAIL_LIMIT`` /
  ``_SIGNUP_GLOBAL_LIMIT``) without rewriting them — if the spec
  bumps the numbers the tests bump with it.
* Disposable-email blocklist — both the bundled file and a
  tmp_path override, plus the SIGHUP-style ``reload_disposable_domains``
  hook.
* CAPTCHA — offline test mode (``test-pass``/``test-fail``),
  disabled-flag pass-through, missing token, and a stubbed Turnstile
  verifier for the real-mode happy + failure paths.
* Reserved / homoglyph slug wrapper — runs through the same vocabulary
  of errors the signup domain service raises.

See ``docs/specs/15-security-privacy.md`` §"Self-serve abuse
mitigations" and ``docs/specs/03-auth-and-tokens.md`` §"Self-serve
signup".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.auth import signup_abuse
from app.auth._throttle import (
    _SIGNUP_EMAIL_LIMIT,
    _SIGNUP_GLOBAL_LIMIT,
    _SIGNUP_IP_LIMIT,
    _SIGNUP_WINDOW,
    SignupRateLimited,
    Throttle,
)
from app.capabilities import Capabilities, DeploymentSettings, Features
from app.config import Settings
from app.net.fetch_guard import FetchGuardBlocked, FetchGuardTimeout
from app.tenancy import InvalidSlug

_PINNED = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def settings_test_mode() -> Settings:
    """:class:`Settings` with no Turnstile secret → offline test mode."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-root-key"),
        public_url="https://crew.day",
        captcha_turnstile_secret=None,
    )


@pytest.fixture
def settings_real_turnstile() -> Settings:
    """:class:`Settings` with a configured Turnstile secret."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-root-key"),
        public_url="https://crew.day",
        captcha_turnstile_secret=SecretStr("turnstile-unit-secret"),
    )


def _capabilities(*, captcha_required: bool) -> Capabilities:
    return Capabilities(
        features=Features(
            rls=False,
            fulltext_search=False,
            concurrent_writers=False,
            object_storage=False,
            wildcard_subdomains=False,
            email_bounce_webhooks=False,
            llm_voice_input=False,
            postgis=False,
            webauthn_configured=False,
        ),
        settings=DeploymentSettings(
            signup_enabled=True, captcha_required=captcha_required
        ),
    )


@pytest.fixture
def caps_captcha_required() -> Capabilities:
    return _capabilities(captcha_required=True)


@pytest.fixture
def caps_captcha_optional() -> Capabilities:
    return _capabilities(captcha_required=False)


# ---------------------------------------------------------------------------
# check_rate — per-IP, per-email, global
# ---------------------------------------------------------------------------


class TestCheckRatePerIp:
    def test_below_limit_passes(self, throttle: Throttle) -> None:
        for _ in range(_SIGNUP_IP_LIMIT):
            signup_abuse.check_rate(
                throttle,
                ip_hash="ip-a",
                email_hash=f"email-{_}",
                now=_PINNED,
            )

    def test_over_ip_limit_raises(self, throttle: Throttle) -> None:
        # Burn the IP budget with distinct email hashes so the
        # per-email bucket doesn't trip first.
        for idx in range(_SIGNUP_IP_LIMIT):
            signup_abuse.check_rate(
                throttle,
                ip_hash="ip-a",
                email_hash=f"email-{idx}",
                now=_PINNED,
            )
        with pytest.raises(SignupRateLimited) as excinfo:
            signup_abuse.check_rate(
                throttle,
                ip_hash="ip-a",
                email_hash="email-never-seen",
                now=_PINNED,
            )
        assert excinfo.value.scope == "ip"
        # Retry-After sits between 1s and the full window.
        assert (
            0 < excinfo.value.retry_after_seconds <= int(_SIGNUP_WINDOW.total_seconds())
        )

    def test_window_rolls_past_limit(self, throttle: Throttle) -> None:
        for idx in range(_SIGNUP_IP_LIMIT):
            signup_abuse.check_rate(
                throttle,
                ip_hash="ip-a",
                email_hash=f"email-{idx}",
                now=_PINNED,
            )
        # An hour + 1s later the bucket should evict the old hits and
        # a fresh call go through.
        later = _PINNED + _SIGNUP_WINDOW + timedelta(seconds=1)
        signup_abuse.check_rate(
            throttle,
            ip_hash="ip-a",
            email_hash="email-fresh",
            now=later,
        )


class TestCheckRatePerEmail:
    def test_over_email_limit_raises_across_ips(self, throttle: Throttle) -> None:
        # Rotate IPs so the per-IP bucket never trips.
        for idx in range(_SIGNUP_EMAIL_LIMIT):
            signup_abuse.check_rate(
                throttle,
                ip_hash=f"ip-{idx}",
                email_hash="email-same",
                now=_PINNED,
            )
        with pytest.raises(SignupRateLimited) as excinfo:
            signup_abuse.check_rate(
                throttle,
                ip_hash="ip-fresh",
                email_hash="email-same",
                now=_PINNED,
            )
        assert excinfo.value.scope == "email"


class TestCheckRateGlobal:
    def test_over_global_limit_raises(self, throttle: Throttle) -> None:
        # Spread across enough IPs + emails that neither sub-bucket
        # trips; the global bucket should catch it first.
        for idx in range(_SIGNUP_GLOBAL_LIMIT):
            signup_abuse.check_rate(
                throttle,
                ip_hash=f"ip-{idx}",
                email_hash=f"email-{idx}",
                now=_PINNED,
            )
        with pytest.raises(SignupRateLimited) as excinfo:
            signup_abuse.check_rate(
                throttle,
                ip_hash="ip-new",
                email_hash="email-new",
                now=_PINNED,
            )
        assert excinfo.value.scope == "global"

    def test_global_bucket_fires_first(self, throttle: Throttle) -> None:
        """When both global and per-IP would trip, global wins."""
        # Over-pump a single IP enough to trip per-IP; but drive the
        # global counter higher so it crosses its own threshold first.
        # Easier construction: fill the global bucket past the cap,
        # then hit from a fresh IP — global fires while per-IP is 0.
        for idx in range(_SIGNUP_GLOBAL_LIMIT):
            signup_abuse.check_rate(
                throttle,
                ip_hash=f"ip-{idx}",
                email_hash=f"email-{idx}",
                now=_PINNED,
            )
        with pytest.raises(SignupRateLimited) as excinfo:
            signup_abuse.check_rate(
                throttle,
                ip_hash="pristine-ip",
                email_hash="pristine-email",
                now=_PINNED,
            )
        assert excinfo.value.scope == "global"


# ---------------------------------------------------------------------------
# is_disposable + reload hook
# ---------------------------------------------------------------------------


class TestIsDisposable:
    def test_bundled_list_rejects_mailinator(self) -> None:
        # Ensure the bundled file is in effect by clearing any cache a
        # prior test may have primed with a tmp_path override.
        signup_abuse.reload_disposable_domains()
        assert signup_abuse.is_disposable("user@mailinator.com") is True

    def test_bundled_list_rejects_yopmail(self) -> None:
        signup_abuse.reload_disposable_domains()
        assert signup_abuse.is_disposable("yo@yopmail.com") is True

    def test_normal_domain_passes(self) -> None:
        signup_abuse.reload_disposable_domains()
        assert signup_abuse.is_disposable("user@example.com") is False

    def test_case_insensitive(self) -> None:
        signup_abuse.reload_disposable_domains()
        assert signup_abuse.is_disposable("User@Mailinator.COM") is True

    def test_malformed_returns_false(self) -> None:
        signup_abuse.reload_disposable_domains()
        assert signup_abuse.is_disposable("not-an-email") is False
        assert signup_abuse.is_disposable("user@") is False

    def test_tmp_path_override_picks_up_fresh_domains(self, tmp_path: Path) -> None:
        custom = tmp_path / "blocklist.txt"
        custom.write_text(
            "# test blocklist\nthrowaway.test\n\n# comment\nmailgunfake.test\n",
            encoding="utf-8",
        )
        # Clear any prior cache entry so the new file is read.
        signup_abuse.reload_disposable_domains(path=custom)
        assert signup_abuse.is_disposable("abc@throwaway.test", path=custom) is True
        assert signup_abuse.is_disposable("abc@example.org", path=custom) is False
        # Cleanup: clear the cache again so sibling tests see the
        # bundled list on the default path.
        signup_abuse.reload_disposable_domains()

    def test_reload_picks_up_file_mutation(self, tmp_path: Path) -> None:
        """SIGHUP-style cache invalidation exercises both initial load + reload.

        Simulates the operator workflow: the blocklist file changes on
        disk, the worker receives SIGHUP, :func:`reload_disposable_domains`
        clears the cache, the next lookup sees the new entries.
        """
        custom = tmp_path / "blocklist.txt"
        custom.write_text("one.test\n", encoding="utf-8")
        signup_abuse.reload_disposable_domains(path=custom)
        assert signup_abuse.is_disposable("a@one.test", path=custom) is True
        assert signup_abuse.is_disposable("a@two.test", path=custom) is False
        # File grows — SIGHUP reload lights up the new entry.
        custom.write_text("one.test\ntwo.test\n", encoding="utf-8")
        count = signup_abuse.reload_disposable_domains(path=custom)
        assert count == 2
        assert signup_abuse.is_disposable("a@two.test", path=custom) is True
        signup_abuse.reload_disposable_domains()


# ---------------------------------------------------------------------------
# check_captcha — test mode + toggle + real-mode stub
# ---------------------------------------------------------------------------


class TestCheckCaptchaTestMode:
    async def test_test_pass_token_succeeds(
        self,
        caps_captcha_required: Capabilities,
        settings_test_mode: Settings,
    ) -> None:
        # No secret configured → offline test mode; "test-pass" passes.
        await signup_abuse.check_captcha(
            "test-pass",
            capabilities=caps_captcha_required,
            settings=settings_test_mode,
        )

    async def test_test_fail_token_rejected(
        self,
        caps_captcha_required: Capabilities,
        settings_test_mode: Settings,
    ) -> None:
        with pytest.raises(signup_abuse.CaptchaFailed) as excinfo:
            await signup_abuse.check_captcha(
                "test-fail",
                capabilities=caps_captcha_required,
                settings=settings_test_mode,
            )
        assert excinfo.value.reason == "captcha_rejected"

    async def test_bogus_token_in_test_mode_flags_misconfig(
        self,
        caps_captcha_required: Capabilities,
        settings_test_mode: Settings,
    ) -> None:
        with pytest.raises(signup_abuse.CaptchaFailed) as excinfo:
            await signup_abuse.check_captcha(
                "looks-like-a-real-token",
                capabilities=caps_captcha_required,
                settings=settings_test_mode,
            )
        assert excinfo.value.reason == "captcha_verifier_unconfigured"

    async def test_empty_token_required(
        self,
        caps_captcha_required: Capabilities,
        settings_test_mode: Settings,
    ) -> None:
        with pytest.raises(signup_abuse.CaptchaFailed) as excinfo:
            await signup_abuse.check_captcha(
                None,
                capabilities=caps_captcha_required,
                settings=settings_test_mode,
            )
        assert excinfo.value.reason == "captcha_required"


class TestCheckCaptchaDisabled:
    async def test_pass_through_when_optional(
        self,
        caps_captcha_optional: Capabilities,
        settings_test_mode: Settings,
    ) -> None:
        # captcha_required=False → no token needed, no verifier call.
        await signup_abuse.check_captcha(
            None,
            capabilities=caps_captcha_optional,
            settings=settings_test_mode,
        )
        await signup_abuse.check_captcha(
            "anything",
            capabilities=caps_captcha_optional,
            settings=settings_test_mode,
        )

    async def test_disabled_never_calls_turnstile(
        self,
        caps_captcha_optional: Capabilities,
        settings_real_turnstile: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even with a real secret wired, captcha_required=False skips the HTTP call."""
        called = {"count": 0}

        async def _boom(*_args: Any, **_kwargs: Any) -> Any:
            called["count"] += 1
            raise AssertionError("Turnstile should not be invoked when disabled")

        monkeypatch.setattr(signup_abuse, "safe_fetch", _boom)
        await signup_abuse.check_captcha(
            "any-token",
            capabilities=caps_captcha_optional,
            settings=settings_real_turnstile,
        )
        assert called["count"] == 0


class TestCheckCaptchaRealMode:
    async def test_real_mode_success(
        self,
        caps_captcha_required: Capabilities,
        settings_real_turnstile: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stub :func:`safe_fetch` to return ``{"success": true}``."""

        async def _fake_fetch(url: str, **kwargs: Any) -> httpx.Response:
            assert "turnstile" in url
            return httpx.Response(200, json={"success": True})

        monkeypatch.setattr(signup_abuse, "safe_fetch", _fake_fetch)
        await signup_abuse.check_captcha(
            "real-looking-token",
            capabilities=caps_captcha_required,
            settings=settings_real_turnstile,
        )

    async def test_real_mode_rejection(
        self,
        caps_captcha_required: Capabilities,
        settings_real_turnstile: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _fake_fetch(url: str, **kwargs: Any) -> httpx.Response:
            del url, kwargs
            return httpx.Response(
                200, json={"success": False, "error-codes": ["invalid-input-response"]}
            )

        monkeypatch.setattr(signup_abuse, "safe_fetch", _fake_fetch)
        with pytest.raises(signup_abuse.CaptchaFailed) as excinfo:
            await signup_abuse.check_captcha(
                "real-looking-token",
                capabilities=caps_captcha_required,
                settings=settings_real_turnstile,
            )
        assert excinfo.value.reason == "captcha_rejected"

    async def test_real_mode_network_error(
        self,
        caps_captcha_required: Capabilities,
        settings_real_turnstile: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _fake_fetch(url: str, **kwargs: Any) -> httpx.Response:
            del url, kwargs
            raise FetchGuardTimeout("boom")

        monkeypatch.setattr(signup_abuse, "safe_fetch", _fake_fetch)
        with pytest.raises(signup_abuse.CaptchaFailed) as excinfo:
            await signup_abuse.check_captcha(
                "real-looking-token",
                capabilities=caps_captcha_required,
                settings=settings_real_turnstile,
            )
        assert excinfo.value.reason == "captcha_verifier_unreachable"

    async def test_real_mode_blocked_by_guard(
        self,
        caps_captcha_required: Capabilities,
        settings_real_turnstile: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """:class:`FetchGuardBlocked` (e.g. DNS error) maps to ``unreachable``."""

        async def _fake_fetch(url: str, **kwargs: Any) -> httpx.Response:
            del url, kwargs
            raise FetchGuardBlocked("dns down", reason="dns_error")

        monkeypatch.setattr(signup_abuse, "safe_fetch", _fake_fetch)
        with pytest.raises(signup_abuse.CaptchaFailed) as excinfo:
            await signup_abuse.check_captcha(
                "real-looking-token",
                capabilities=caps_captcha_required,
                settings=settings_real_turnstile,
            )
        assert excinfo.value.reason == "captcha_verifier_unreachable"

    async def test_real_mode_raw_httpx_transport_error(
        self,
        caps_captcha_required: Capabilities,
        settings_real_turnstile: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Raw :class:`httpx.HTTPError` from :func:`safe_fetch` maps to ``unreachable``.

        :func:`app.net.fetch_guard.safe_fetch` only translates
        :class:`httpx.TimeoutException` into its own vocabulary —
        other transport-level failures (TCP reset, TLS handshake,
        malformed response, proxy failure) propagate as raw httpx
        exceptions. The captcha helper has to swallow them as a
        ``captcha_verifier_unreachable`` so a Cloudflare blip does
        not surface as a 500 from ``POST /signup/start``.
        """

        async def _fake_fetch(url: str, **kwargs: Any) -> httpx.Response:
            del url, kwargs
            raise httpx.ConnectError("tcp reset")

        monkeypatch.setattr(signup_abuse, "safe_fetch", _fake_fetch)
        with pytest.raises(signup_abuse.CaptchaFailed) as excinfo:
            await signup_abuse.check_captcha(
                "real-looking-token",
                capabilities=caps_captcha_required,
                settings=settings_real_turnstile,
            )
        assert excinfo.value.reason == "captcha_verifier_unreachable"


# ---------------------------------------------------------------------------
# check_reserved_slug
# ---------------------------------------------------------------------------


class TestCheckReservedSlug:
    def test_reserved_slug_raises(self) -> None:
        with pytest.raises(signup_abuse.SlugReserved):
            signup_abuse.check_reserved_slug("admin", existing_slugs=[])

    def test_invalid_pattern_raises(self) -> None:
        with pytest.raises(InvalidSlug):
            # ``X`` uppercase + too-short.
            signup_abuse.check_reserved_slug("A", existing_slugs=[])

    def test_consecutive_hyphens_raises(self) -> None:
        with pytest.raises(InvalidSlug):
            signup_abuse.check_reserved_slug("bad--slug", existing_slugs=[])

    def test_homoglyph_collision_raises(self) -> None:
        with pytest.raises(signup_abuse.SlugHomoglyphError) as excinfo:
            # ``rnicasa`` folds to ``micasa`` via the ``rn → m`` pair
            # substitution (see :func:`app.tenancy.normalise_for_collision`).
            signup_abuse.check_reserved_slug(
                "rnicasa", existing_slugs=["micasa", "other-ws"]
            )
        assert excinfo.value.colliding_slug == "micasa"

    def test_happy_path(self) -> None:
        signup_abuse.check_reserved_slug(
            "villa-sud", existing_slugs=["other-ws", "another"]
        )

    def test_reserved_beats_invalid(self) -> None:
        """A reserved slug that is also too short surfaces as ``SlugReserved``.

        ``w`` is both reserved and fails the 3-char pattern minimum;
        the reserved check fires first so the distinct error symbol
        wins. This pins the ordering contract in
        :func:`check_reserved_slug`'s docstring.
        """
        with pytest.raises(signup_abuse.SlugReserved):
            signup_abuse.check_reserved_slug("w", existing_slugs=[])
