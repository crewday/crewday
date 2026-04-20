"""Integration tests for :mod:`app.main` against a real DB.

Unit tests cover the factory's wiring + middleware shape against an
in-memory SQLite URL (no DB reads). The integration suite exists to
verify the bits that require a live engine:

* ``/readyz`` returns 200 when the UoW's ``SELECT 1`` succeeds;
* ``/readyz`` returns 503 when the engine's pool is poisoned;
* end-to-end healthcheck round-trip (``/healthz`` → ``/readyz`` →
  ``/version``) against the alembic-migrated schema the integration
  harness ships.

See ``docs/specs/16-deployment-operations.md`` §"Healthchecks",
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
        root_key=SecretStr("integration-test-main-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
    )


@pytest.fixture
def real_make_uow(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> Iterator[None]:
    """Redirect the process-wide default UoW to the integration engine.

    The factory's ``/readyz`` handler opens ``make_uow()`` directly —
    it does not know about FastAPI dep overrides. Patching the
    module-level defaults keeps the integration test self-contained
    without touching env vars. The original values are restored on
    teardown so no state leaks into sibling tests.
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


class TestOpsProbesAgainstRealDb:
    """End-to-end ``/healthz`` + ``/readyz`` + ``/version`` flow."""

    def test_full_healthcheck_roundtrip(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        """All three ops probes pass against a migrated DB."""
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)

        healthz = client.get("/healthz")
        assert healthz.status_code == 200
        assert healthz.json() == {"status": "ok"}

        readyz = client.get("/readyz")
        assert readyz.status_code == 200
        assert readyz.json() == {"status": "ok"}

        version = client.get("/version")
        assert version.status_code == 200
        assert "version" in version.json()

    def test_readyz_returns_503_when_db_unreachable(
        self, pinned_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dead engine must surface as ``503 db_unreachable``.

        We point ``make_uow()`` at a URL that cannot connect (invalid
        driver args). The UoW open must raise a
        :class:`sqlalchemy.exc.SQLAlchemyError`, which the handler
        catches and maps to 503 — any other exception would crash
        the probe and reveal the regression immediately.
        """
        from app.adapters.db.session import make_engine

        dead_engine = make_engine("sqlite:///this/path/does/not/exist/crewday.db")
        dead_factory = sessionmaker(
            bind=dead_engine, expire_on_commit=False, class_=Session
        )
        original_engine = _session_mod._default_engine
        original_factory = _session_mod._default_sessionmaker_
        _session_mod._default_engine = dead_engine
        _session_mod._default_sessionmaker_ = dead_factory
        try:
            app = create_app(settings=pinned_settings)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/readyz")
            assert resp.status_code == 503
            assert resp.json()["status"] == "degraded"
        finally:
            _session_mod._default_engine = original_engine
            _session_mod._default_sessionmaker_ = original_factory
            dead_engine.dispose()

    def test_version_matches_pyproject(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        """``/version`` mirrors the installed package version."""
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as pkg_version

        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/version")
        assert resp.status_code == 200
        body = resp.json()
        try:
            expected = pkg_version("crewday")
        except PackageNotFoundError:
            expected = "0.0.0+unknown"
        assert body == {"version": expected}

    def test_readyz_reopens_uow_per_probe(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        """A fresh UoW per probe means a recovered engine immediately
        reports healthy again — no stale connection pinning across
        scrapes.
        """
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)

        # Two probes back-to-back must both succeed.
        first = client.get("/readyz")
        second = client.get("/readyz")
        assert first.status_code == 200
        assert second.status_code == 200

    def test_api_openapi_json_reachable_with_live_db(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        """The OpenAPI surface stays reachable after full factory wiring."""
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/openapi.json")
        assert resp.status_code == 200
        body = resp.json()
        # FastAPI-emitted OpenAPI always carries an ``info.title``;
        # we pinned it to ``crewday`` in the factory.
        assert body["info"]["title"] == "crewday"
