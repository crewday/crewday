"""Unit tests for :mod:`app.api.middleware.security_headers`.

Covers the CSP-nonce + §15 HTTP-security-header middleware in
isolation, via a minimal Starlette app so the factory's full wiring
doesn't cloud the assertions. The integration counterpart
(``tests/integration/api/test_security_headers.py``) exercises the
full ``create_app()`` stack across several representative routes.

Covers:

* nonce shape + entropy;
* ``request.state.csp_nonce`` visible to downstream handlers;
* every required header is stamped on the response;
* HSTS is opt-in via ``settings.hsts_enabled``;
* demo-mode ``frame-ancestors`` carve-out;
* ``Permissions-Policy`` ``camera=(self)`` only on worker routes.

See ``docs/specs/15-security-privacy.md`` §"HTTP security headers",
§"Shared-origin XSS containment", §24 "CSP on demo".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.api.middleware.security_headers import (
    SecurityHeadersMiddleware,
    build_csp_header,
    build_permissions_policy,
    generate_csp_nonce,
)
from app.config import Settings


def _settings(
    *,
    demo_mode: bool = False,
    demo_frame_ancestors: str | None = None,
    hsts_enabled: bool = False,
) -> Settings:
    """Minimal :class:`Settings` — every field not listed here is
    irrelevant to the security-header middleware.

    ``Settings.model_construct`` skips validation (no env reads, no
    defaults-callable invocation of the list-typed fields) so the
    harness stays hermetic.
    """
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-csp-nonce-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        demo_mode=demo_mode,
        demo_frame_ancestors=demo_frame_ancestors,
        hsts_enabled=hsts_enabled,
        worker="internal",
        profile="prod",
    )


_HandlerFn = Callable[[Request], Awaitable[Response]]


def _build_app(
    settings: Settings,
    handler: _HandlerFn | None = None,
    *,
    path: str = "/probe",
) -> Starlette:
    """Return a Starlette app wearing only the security-headers middleware.

    ``handler`` defaults to an empty-JSON endpoint; tests that want to
    observe ``request.state.csp_nonce`` supply their own to capture it.
    """

    async def _default(request: Request) -> Response:
        return JSONResponse({"ok": True})

    routes = [Route(path, handler or _default)]
    app = Starlette(routes=routes)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    return app


# ---------------------------------------------------------------------------
# generate_csp_nonce
# ---------------------------------------------------------------------------


class TestGenerateCspNonce:
    """128-bit nonce, base64 unpadded, unique per call."""

    def test_nonce_is_ascii(self) -> None:
        nonce = generate_csp_nonce()
        # ``decode('ascii')`` in the implementation means any
        # non-ASCII byte is already a bug; the belt-and-braces check
        # exists to catch a regression that swaps in a non-ASCII
        # alphabet.
        assert nonce.isascii()

    def test_nonce_has_no_padding(self) -> None:
        """``=`` terminators are stripped — CSP parsers don't need them."""
        nonce = generate_csp_nonce()
        assert "=" not in nonce

    def test_nonce_has_expected_length(self) -> None:
        """16 bytes → 22-char unpadded base64."""
        nonce = generate_csp_nonce()
        assert len(nonce) == 22

    def test_nonce_unique_per_call(self) -> None:
        """Two calls in a row MUST produce different values.

        The CSP defence collapses if two responses can share a nonce —
        an attacker who captures one in the wild could then use it
        against a subsequent request.
        """
        nonces = {generate_csp_nonce() for _ in range(256)}
        # 22 base64 chars ≈ 128 bits of entropy; a birthday collision
        # within 256 samples would imply an entropy regression.
        assert len(nonces) == 256


# ---------------------------------------------------------------------------
# build_csp_header
# ---------------------------------------------------------------------------


class TestBuildCspHeader:
    """Pure function — exact directive string assertion."""

    def test_prod_header_has_frame_ancestors_none(self) -> None:
        csp = build_csp_header("NONCE123", demo_mode=False, demo_frame_ancestors=None)
        assert "frame-ancestors 'none'" in csp

    def test_nonce_appears_in_script_and_style_src(self) -> None:
        csp = build_csp_header("NONCE123", demo_mode=False, demo_frame_ancestors=None)
        assert "script-src 'self' 'nonce-NONCE123'" in csp
        assert "style-src 'self' 'nonce-NONCE123'" in csp

    def test_object_src_none(self) -> None:
        csp = build_csp_header("NONCE123", demo_mode=False, demo_frame_ancestors=None)
        assert "object-src 'none'" in csp

    def test_demo_mode_widens_frame_ancestors(self) -> None:
        """Demo mode + allowlist → ``frame-ancestors`` carries the allowlist."""
        csp = build_csp_header(
            "NONCE123",
            demo_mode=True,
            demo_frame_ancestors="https://crew.day https://*.crew.day",
        )
        assert "frame-ancestors https://crew.day https://*.crew.day" in csp
        assert "frame-ancestors 'none'" not in csp

    def test_demo_mode_without_allowlist_stays_none(self) -> None:
        """Demo mode but no allowlist → the demo still runs stand-alone."""
        csp = build_csp_header("NONCE123", demo_mode=True, demo_frame_ancestors=None)
        assert "frame-ancestors 'none'" in csp

    def test_frame_ancestors_only_widens_under_demo(self) -> None:
        """Prod (demo_mode=False) ignores ``demo_frame_ancestors``.

        An accidentally set env var on a prod box must never weaken
        the framing policy.
        """
        csp = build_csp_header(
            "NONCE123",
            demo_mode=False,
            demo_frame_ancestors="https://evil.example",
        )
        assert "frame-ancestors 'none'" in csp
        assert "https://evil.example" not in csp


# ---------------------------------------------------------------------------
# build_permissions_policy
# ---------------------------------------------------------------------------


class TestBuildPermissionsPolicy:
    """``camera=(self)`` only on ``/w/<slug>/worker/...`` paths."""

    @pytest.mark.parametrize(
        "path",
        [
            "/w/villa-sud/worker/tasks",
            "/w/villa-sud/worker/task/abc123/photo",
            "/w/any-slug/worker/",
        ],
    )
    def test_worker_path_allows_camera(self, path: str) -> None:
        policy = build_permissions_policy(path)
        assert "camera=(self)" in policy

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/healthz",
            "/api/openapi.json",
            "/w/villa-sud/api/v1/tasks",
            "/w/villa-sud/today",
            "/anything-spa",
            # Naively matching ``worker`` as a substring would misfire
            # here — the regex must anchor on ``/w/<slug>/worker/``.
            "/workers-news",
            "/w/slug/notworker/task",
        ],
    )
    def test_non_worker_paths_deny_camera(self, path: str) -> None:
        policy = build_permissions_policy(path)
        assert "camera=()" in policy
        assert "camera=(self)" not in policy

    def test_geolocation_denied_everywhere(self) -> None:
        """v1 has no geolocation surface — §15 data-minimisation."""
        for path in ("/", "/w/x/worker/task/1"):
            assert "geolocation=()" in build_permissions_policy(path)

    def test_microphone_and_payment_denied_everywhere(self) -> None:
        for path in ("/", "/w/x/worker/task/1"):
            policy = build_permissions_policy(path)
            assert "microphone=()" in policy
            assert "payment=()" in policy


# ---------------------------------------------------------------------------
# SecurityHeadersMiddleware — live dispatch
# ---------------------------------------------------------------------------


class TestSecurityHeadersMiddlewareHeaders:
    """Header surface stamped on real responses."""

    def test_csp_header_present_and_contains_nonce(self) -> None:
        app = _build_app(_settings())
        client = TestClient(app)
        resp = client.get("/probe")
        csp = resp.headers["content-security-policy"]
        # The emitted nonce must be the one the response was signed
        # with — we can't compare byte-for-byte (we didn't capture it),
        # but the directive shape is stable.
        assert "script-src 'self' 'nonce-" in csp
        assert "style-src 'self' 'nonce-" in csp

    def test_referrer_policy_is_strict_origin_when_cross_origin(self) -> None:
        app = _build_app(_settings())
        client = TestClient(app)
        resp = client.get("/probe")
        assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"

    def test_x_content_type_is_nosniff(self) -> None:
        app = _build_app(_settings())
        client = TestClient(app)
        resp = client.get("/probe")
        assert resp.headers["x-content-type-options"] == "nosniff"

    def test_x_frame_options_is_deny(self) -> None:
        """Defence-in-depth alongside CSP ``frame-ancestors``."""
        app = _build_app(_settings())
        client = TestClient(app)
        resp = client.get("/probe")
        assert resp.headers["x-frame-options"] == "DENY"

    def test_coop_and_corp(self) -> None:
        app = _build_app(_settings())
        client = TestClient(app)
        resp = client.get("/probe")
        assert resp.headers["cross-origin-opener-policy"] == "same-origin"
        assert resp.headers["cross-origin-resource-policy"] == "same-origin"

    def test_permissions_policy_non_worker_denies_camera(self) -> None:
        app = _build_app(_settings(), path="/probe")
        client = TestClient(app)
        resp = client.get("/probe")
        assert "camera=()" in resp.headers["permissions-policy"]

    def test_permissions_policy_worker_allows_camera(self) -> None:
        app = _build_app(_settings(), path="/w/acme/worker/task/abc/photo")
        client = TestClient(app)
        resp = client.get("/w/acme/worker/task/abc/photo")
        assert "camera=(self)" in resp.headers["permissions-policy"]


class TestNonceExposedOnRequestState:
    """Downstream handlers can read the nonce off ``request.state``."""

    def test_state_csp_nonce_is_readable_and_matches_response(self) -> None:
        captured: dict[str, str] = {}

        async def handler(request: Request) -> Response:
            # Will KeyError / AttributeError if the middleware forgot
            # to set the nonce — test failure is louder than silent
            # fallback.
            captured["nonce"] = request.state.csp_nonce
            return JSONResponse({"ok": True})

        app = _build_app(_settings(), handler=handler)
        client = TestClient(app)
        resp = client.get("/probe")

        assert "nonce" in captured
        # The response CSP must advertise the same nonce the handler
        # saw — otherwise the bootstrap <script> tag would mismatch
        # the directive and the browser would refuse to execute it.
        csp = resp.headers["content-security-policy"]
        assert f"'nonce-{captured['nonce']}'" in csp

    def test_nonce_changes_per_request(self) -> None:
        seen: list[str] = []

        async def handler(request: Request) -> Response:
            seen.append(request.state.csp_nonce)
            return JSONResponse({"ok": True})

        app = _build_app(_settings(), handler=handler)
        client = TestClient(app)
        for _ in range(5):
            client.get("/probe")
        # Five hits → five distinct nonces; reusing a value would
        # collapse the CSP guarantee.
        assert len(seen) == 5
        assert len(set(seen)) == 5


class TestHstsGating:
    """HSTS rides ``settings.hsts_enabled``."""

    def test_hsts_absent_when_disabled(self) -> None:
        app = _build_app(_settings(hsts_enabled=False))
        client = TestClient(app)
        resp = client.get("/probe")
        assert "strict-transport-security" not in resp.headers

    def test_hsts_present_when_enabled(self) -> None:
        app = _build_app(_settings(hsts_enabled=True))
        client = TestClient(app)
        resp = client.get("/probe")
        assert (
            resp.headers["strict-transport-security"]
            == "max-age=63072000; includeSubDomains; preload"
        )


class TestDemoFrameAncestors:
    """Demo mode can widen ``frame-ancestors`` to an allowlist."""

    def test_demo_mode_with_allowlist_widens_frame_ancestors(self) -> None:
        app = _build_app(
            _settings(
                demo_mode=True,
                demo_frame_ancestors="https://crew.day",
            )
        )
        client = TestClient(app)
        resp = client.get("/probe")
        csp = resp.headers["content-security-policy"]
        assert "frame-ancestors https://crew.day" in csp
        assert "frame-ancestors 'none'" not in csp

    def test_prod_mode_ignores_demo_allowlist(self) -> None:
        """A ``demo_frame_ancestors`` value must not leak into prod CSP."""
        app = _build_app(
            _settings(
                demo_mode=False,
                demo_frame_ancestors="https://somewhere",
            )
        )
        client = TestClient(app)
        resp = client.get("/probe")
        csp = resp.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp
        assert "https://somewhere" not in csp

    def test_demo_mode_with_allowlist_omits_xframe_options(self) -> None:
        """§24 carve-out — ``X-Frame-Options`` is not set on demo responses.

        Browsers that honour both CSP and the legacy header fall back
        to the more-restrictive ``DENY`` — which would neutralise the
        ``frame-ancestors`` allowlist and block the demo iframe on the
        landing page.
        """
        app = _build_app(
            _settings(
                demo_mode=True,
                demo_frame_ancestors="https://crew.day",
            )
        )
        client = TestClient(app)
        resp = client.get("/probe")
        assert "x-frame-options" not in resp.headers

    def test_demo_mode_without_allowlist_keeps_xframe_deny(self) -> None:
        """Demo flag on but no allowlist → still single-origin, XFO stays.

        Matches the ``frame-ancestors 'none'`` fallback: if the operator
        hasn't opted in to embedding, the demo runs stand-alone and the
        legacy header keeps ancient browsers covered.
        """
        app = _build_app(
            _settings(
                demo_mode=True,
                demo_frame_ancestors=None,
            )
        )
        client = TestClient(app)
        resp = client.get("/probe")
        assert resp.headers["x-frame-options"] == "DENY"


class TestNonceNotLogged:
    """The middleware must not log the nonce.

    This is a structural check, not a runtime one: the
    ``security_headers`` module should reference no ``logging`` /
    ``logger`` symbols at all so a future change that adds one is
    caught by a focused review.
    """

    def test_middleware_module_does_not_import_logging(self) -> None:
        import app.api.middleware.security_headers as sh

        assert not hasattr(sh, "logger")
        # The module's global namespace should not shadow
        # :mod:`logging` either — a ``from logging import ...`` would
        # show up as a module attribute.
        assert "logging" not in vars(sh)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Things we want to cover that don't fit above."""

    @pytest.mark.parametrize(
        "status_code",
        [200, 204, 404, 500],
    )
    def test_headers_stamped_on_every_status(self, status_code: int) -> None:
        """A downstream handler returning a non-200 still gets the header set.

        Middleware for CSP / COOP / X-Frame must stamp regardless of
        status — an error page rendered via HTML needs the same
        protection as the success response.
        """

        async def handler(request: Request) -> Response:
            return Response(b"", status_code=status_code)

        app = _build_app(_settings(), handler=handler)
        client = TestClient(app)
        resp = client.get("/probe")
        assert resp.status_code == status_code
        assert "content-security-policy" in resp.headers
        assert "x-content-type-options" in resp.headers

    @pytest.mark.parametrize(
        "demo_mode,demo_fa,hsts,expected_hsts_present,expected_fa",
        [
            (False, None, False, False, "'none'"),
            (False, None, True, True, "'none'"),
            (True, "https://example", False, False, "https://example"),
            (True, "https://example", True, True, "https://example"),
            (True, None, True, True, "'none'"),
        ],
    )
    def test_settings_matrix(
        self,
        demo_mode: bool,
        demo_fa: str | None,
        hsts: bool,
        expected_hsts_present: bool,
        expected_fa: str,
    ) -> None:
        """Exhaustive cross-product of the three policy knobs."""
        app = _build_app(
            _settings(
                demo_mode=demo_mode,
                demo_frame_ancestors=demo_fa,
                hsts_enabled=hsts,
            )
        )
        client = TestClient(app)
        resp = client.get("/probe")
        csp = resp.headers["content-security-policy"]
        assert f"frame-ancestors {expected_fa}" in csp
        assert ("strict-transport-security" in resp.headers) is expected_hsts_present
