"""Integration tests for the §15 HTTP-security-header middleware.

Exercises the full :func:`app.main.create_app` factory across five
representative routes to prove every one carries the §15 header set
in production. Unit-level coverage (nonce shape, per-request
uniqueness, demo carve-out) lives in
``tests/unit/api/test_csp_nonce.py``.

Routes probed:

* ``/`` — SPA fallback (HTML).
* ``/healthz`` — ops probe (JSON, tenancy-skipped path).
* ``/api/openapi.json`` — OpenAPI descriptor.
* ``/w/abc/api/v1/whatever`` — unknown workspace, exercises the
  tenancy middleware's constant-time 404 path.
* ``/anything-spa`` — deep-link SPA route via the catch-all.

See ``docs/specs/15-security-privacy.md`` §"HTTP security headers",
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.config import Settings
from app.main import create_app
from app.tenancy.orm_filter import install_tenant_filter

pytestmark = pytest.mark.integration


@pytest.fixture
def pinned_settings(db_url: str) -> Settings:
    """:class:`Settings` bound to the integration harness's DB URL."""
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-test-security-headers-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        profile="prod",
        vite_dev_url="http://127.0.0.1:5173",
        demo_mode=False,
        demo_frame_ancestors=None,
        hsts_enabled=False,
    )


@pytest.fixture
def pinned_settings_hsts(db_url: str) -> Settings:
    """Same as :func:`pinned_settings` but with HSTS enabled."""
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-test-security-headers-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        profile="prod",
        vite_dev_url="http://127.0.0.1:5173",
        demo_mode=False,
        demo_frame_ancestors=None,
        hsts_enabled=True,
    )


@pytest.fixture
def pinned_settings_demo(db_url: str) -> Settings:
    """Demo mode + ``frame-ancestors`` allowlist (§24 carve-out)."""
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-test-security-headers-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        profile="prod",
        vite_dev_url="http://127.0.0.1:5173",
        demo_mode=True,
        demo_frame_ancestors="https://crew.day",
        hsts_enabled=False,
    )


@pytest.fixture
def real_make_uow(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> Iterator[None]:
    """Redirect the process-wide default UoW to the integration engine.

    Mirrors the helper in ``tests/integration/test_main.py`` so this
    suite can stand alone. Restored on teardown.
    """
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


# Five probe routes covering: HTML SPA root, ops probe, API descriptor,
# unknown-workspace 404 (tenancy path), SPA deep-link.
_PROBE_PATHS = [
    "/",
    "/healthz",
    "/api/openapi.json",
    "/w/abc/api/v1/whatever",
    "/anything-spa",
]


class TestHeaderSetAcrossRoutes:
    """Every response from the factory carries the full §15 header set."""

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_csp_present(
        self,
        pinned_settings: Settings,
        real_make_uow: None,
        path: str,
    ) -> None:
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(path)
        csp = resp.headers.get("content-security-policy")
        assert csp is not None
        # Directive list order + content matches the spec.
        for directive in (
            "default-src 'self'",
            "script-src 'self' 'nonce-",
            "style-src 'self' 'nonce-",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "form-action 'self'",
            "base-uri 'self'",
            "object-src 'none'",
        ):
            assert directive in csp, (path, directive, csp)

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_referrer_policy(
        self,
        pinned_settings: Settings,
        real_make_uow: None,
        path: str,
    ) -> None:
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(path)
        assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_xcontent_type_nosniff(
        self,
        pinned_settings: Settings,
        real_make_uow: None,
        path: str,
    ) -> None:
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(path)
        assert resp.headers["x-content-type-options"] == "nosniff"

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_x_frame_options_deny(
        self,
        pinned_settings: Settings,
        real_make_uow: None,
        path: str,
    ) -> None:
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(path)
        assert resp.headers["x-frame-options"] == "DENY"

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_coop_and_corp(
        self,
        pinned_settings: Settings,
        real_make_uow: None,
        path: str,
    ) -> None:
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(path)
        assert resp.headers["cross-origin-opener-policy"] == "same-origin"
        assert resp.headers["cross-origin-resource-policy"] == "same-origin"

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_permissions_policy_denies_camera_on_non_worker_routes(
        self,
        pinned_settings: Settings,
        real_make_uow: None,
        path: str,
    ) -> None:
        """None of the probe routes are worker routes — camera=()."""
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(path)
        policy = resp.headers["permissions-policy"]
        assert "camera=()" in policy
        assert "geolocation=()" in policy

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_hsts_absent_by_default(
        self,
        pinned_settings: Settings,
        real_make_uow: None,
        path: str,
    ) -> None:
        """Opt-in knob off → no HSTS header on any route."""
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(path)
        assert "strict-transport-security" not in resp.headers

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_hsts_present_when_enabled(
        self,
        pinned_settings_hsts: Settings,
        real_make_uow: None,
        path: str,
    ) -> None:
        """``hsts_enabled=True`` stamps the exact spec'd HSTS value."""
        app = create_app(settings=pinned_settings_hsts)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(path)
        assert (
            resp.headers["strict-transport-security"]
            == "max-age=63072000; includeSubDomains; preload"
        )

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_nonce_per_request_changes(
        self,
        pinned_settings: Settings,
        real_make_uow: None,
        path: str,
    ) -> None:
        """Two hits to the same path emit different nonces.

        The middleware must never reuse a nonce — even across
        identical requests — or a captured bootstrap page could be
        replayed against a fresh load and satisfy CSP.
        """
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        csps = {client.get(path).headers["content-security-policy"] for _ in range(3)}
        # Three distinct CSPs → three distinct nonces.
        assert len(csps) == 3


class TestDemoCspCarveOut:
    """§24 "CSP on demo" — ``frame-ancestors`` widened, ``X-Frame-Options`` dropped."""

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_demo_frame_ancestors_widens(
        self,
        pinned_settings_demo: Settings,
        real_make_uow: None,
        path: str,
    ) -> None:
        app = create_app(settings=pinned_settings_demo)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(path)
        csp = resp.headers["content-security-policy"]
        assert "frame-ancestors https://crew.day" in csp
        assert "frame-ancestors 'none'" not in csp

    @pytest.mark.parametrize("path", _PROBE_PATHS)
    def test_demo_omits_x_frame_options(
        self,
        pinned_settings_demo: Settings,
        real_make_uow: None,
        path: str,
    ) -> None:
        """Browsers honour the legacy header even when CSP permits — the
        carve-out MUST drop it or the allowlist is silently defeated.
        """
        app = create_app(settings=pinned_settings_demo)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(path)
        assert "x-frame-options" not in resp.headers


class TestWorkerRouteCamera:
    """``/w/<slug>/worker/...`` exposes ``camera=(self)`` in Permissions-Policy."""

    def test_worker_path_allows_camera(
        self,
        pinned_settings: Settings,
        real_make_uow: None,
    ) -> None:
        """Even though the slug is unknown (404), the path-based carve-out
        still applies — the CSP + Permissions-Policy are stamped by the
        outer middleware before tenancy resolves the slug.
        """
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/w/any-slug/worker/task/abc/photo")
        policy = resp.headers["permissions-policy"]
        assert "camera=(self)" in policy
