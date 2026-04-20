"""Integration tests for :mod:`app.api.health` against a real DB.

Unit tests (``tests/unit/test_health.py``) cover each probe branch
against a patched UoW + fake session. This suite exists for what the
unit layer can't assert:

* ``/readyz`` returns 200 against a **migrated** SQLite / Postgres DB
  with a freshly-written :class:`WorkerHeartbeat` row and a real
  alembic-heads read;
* ``/readyz`` returns 503 when the heartbeat row is older than 60 s —
  exercising the end-to-end SQL path the worker will write through;
* ``/readyz`` returns 503 when the DB has no heartbeat row at all;
* the alembic-heads compare path reads the real ``alembic_version``
  row written by the harness and matches the in-repo script head.

See ``docs/specs/16-deployment-operations.md`` §"Healthchecks" and
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, delete, text
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.ops.models import WorkerHeartbeat
from app.config import Settings
from app.main import create_app
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid

pytestmark = pytest.mark.integration


@pytest.fixture
def pinned_settings(db_url: str) -> Settings:
    """:class:`Settings` bound to the integration harness's DB URL."""
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-test-health-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
    )


@pytest.fixture
def real_make_uow(engine: Engine) -> Iterator[None]:
    """Redirect the process-wide default UoW to the integration engine.

    ``/readyz`` opens :func:`app.adapters.db.session.make_uow` directly
    — FastAPI dep overrides do not apply. Patching the module-level
    defaults keeps the test self-contained; teardown restores the
    originals so sibling tests see a clean slate.
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


@pytest.fixture
def clean_heartbeat(engine: Engine) -> Iterator[None]:
    """Delete every ``worker_heartbeat`` row before and after each test.

    The shared session fixture in ``tests/integration/conftest.py`` uses
    a SAVEPOINT pattern that doesn't fully isolate ``INSERT`` statements
    on SQLite (the pysqlite driver commits the outer transaction's
    writes on ``RELEASE SAVEPOINT`` due to a long-standing isolation-
    level quirk). Rather than fight that — and because
    ``worker_heartbeat`` is a tiny cross-tenant ops table — each test
    clears the table explicitly against the engine so no sibling test
    inherits leftover rows.
    """
    with engine.begin() as conn:
        conn.execute(delete(WorkerHeartbeat))
    yield
    with engine.begin() as conn:
        conn.execute(delete(WorkerHeartbeat))


def _insert_heartbeat(engine: Engine, *, at: datetime) -> None:
    """Insert a fresh ``worker_heartbeat`` row via the engine directly.

    Writes through ``engine.begin()`` so the row is COMMITTED (visible
    to the handler's independent UoW) without going through the
    SAVEPOINT-based ``db_session`` fixture. The writer-side domain
    module is out of scope for cd-leif — this test-local helper
    bootstraps the row so the read-side probe has something to see.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO worker_heartbeat (id, worker_name, heartbeat_at) "
                "VALUES (:id, :name, :at)"
            ),
            {"id": new_ulid(), "name": "scheduler", "at": at},
        )


# ---------------------------------------------------------------------------
# Integration cases
# ---------------------------------------------------------------------------


class TestReadyzAgainstRealDb:
    """End-to-end ``/readyz`` against the migrated schema."""

    def test_readyz_200_when_heartbeat_fresh(
        self,
        pinned_settings: Settings,
        real_make_uow: None,
        clean_heartbeat: None,
        engine: Engine,
    ) -> None:
        """Every check passes → 200 ``{status: ok, checks: []}``.

        Writes one ``worker_heartbeat`` row at "now"; the real
        alembic heads read against the migrated schema must match
        the in-repo script head; the pinned :class:`Settings` has a
        non-empty root key. No other setup required.
        """
        _insert_heartbeat(engine, at=datetime.now(UTC))

        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/readyz")
        assert resp.status_code == 200, resp.json()
        body = resp.json()
        assert body == {"status": "ok", "checks": []}

    def test_readyz_503_when_heartbeat_missing(
        self,
        pinned_settings: Settings,
        real_make_uow: None,
        clean_heartbeat: None,
    ) -> None:
        """No ``worker_heartbeat`` row → 503 with ``no_heartbeat``."""
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/readyz")
        assert resp.status_code == 503
        body = resp.json()
        row = next(c for c in body["checks"] if c["check"] == "worker_heartbeat")
        assert row["detail"] == "no_heartbeat"

    def test_readyz_503_when_heartbeat_stale(
        self,
        pinned_settings: Settings,
        real_make_uow: None,
        clean_heartbeat: None,
        engine: Engine,
    ) -> None:
        """Heartbeat older than 60 s → 503 with ``heartbeat_stale``."""
        stale = datetime.now(UTC) - timedelta(seconds=120)
        _insert_heartbeat(engine, at=stale)

        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/readyz")
        assert resp.status_code == 503
        body = resp.json()
        row = next(c for c in body["checks"] if c["check"] == "worker_heartbeat")
        assert row["detail"] == "heartbeat_stale"

    def test_healthz_bypasses_db_even_with_real_engine(
        self,
        pinned_settings: Settings,
        real_make_uow: None,
    ) -> None:
        """``/healthz`` stays 200 no matter what the DB looks like."""
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_version_shape_against_real_wiring(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        """``/version`` keys are pinned — shape is a public contract."""
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        body = client.get("/version").json()
        assert set(body.keys()) == {"version", "git_sha", "build_at", "image_digest"}
