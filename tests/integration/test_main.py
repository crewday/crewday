"""Integration tests for :mod:`app.main` against a real DB.

Unit tests cover the factory's wiring + middleware shape against an
in-memory SQLite URL (no DB reads). The integration suite exists to
verify the bits that require a live engine:

* the OpenAPI surface is reachable once the full factory (middleware
  + routers) is wired against a real engine.

Ops-probe behaviour against a live DB (``/healthz``, ``/readyz``,
``/version``) lives in ``tests/integration/test_health.py`` per the
cd-leif refactor — those probes now live in :mod:`app.api.health`
and that suite covers the full probe surface.

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


class TestFactoryAgainstRealDb:
    """Factory wiring holds up once a live engine is attached."""

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
