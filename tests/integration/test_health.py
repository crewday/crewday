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
from sqlalchemy import Connection, insert
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
def real_make_uow(db_session: Session) -> Iterator[None]:
    """Redirect the process-wide default UoW onto the savepointed connection.

    ``/readyz`` opens :func:`app.adapters.db.session.make_uow` directly
    — FastAPI dep overrides do not apply. Patching the module-level
    defaults keeps the test self-contained; teardown restores the
    originals so sibling tests see a clean slate.

    Crucially we bind ``_default_sessionmaker_`` to the *connection*
    that backs ``db_session`` (rather than to the engine), with
    ``join_transaction_mode="create_savepoint"``. That way the
    handler's independent UoW lands on the same outer transaction the
    test's SAVEPOINT lives on, so:

    * heartbeat rows written through ``db_session`` are visible to
      the handler's read-side probe (same connection, same SAVEPOINT);
    * the outer rollback at ``db_session`` teardown undoes every
      handler-side write too — no per-test cleanup fixture needed.

    The pysqlite SAVEPOINT-isolation fix lives in the
    :func:`tests.integration.conftest.db_session` fixture (per-connection
    ``isolation_level=None`` + manual ``BEGIN`` / ``ROLLBACK``) — without
    it pysqlite would auto-commit on ``RELEASE SAVEPOINT`` and writes
    would still leak across tests.
    """
    # ``db_session`` is bound to a live :class:`Connection` (not the
    # raw engine), so ``get_bind()`` returns a Connection instance —
    # narrow with ``isinstance`` so the type ignore on ``.engine``
    # is unnecessary and mypy stays clean.
    bind = db_session.get_bind()
    assert isinstance(bind, Connection)
    factory = sessionmaker(
        bind=bind,
        expire_on_commit=False,
        class_=Session,
        join_transaction_mode="create_savepoint",
    )
    install_tenant_filter(factory)
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = bind.engine
    _session_mod._default_sessionmaker_ = factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


def _insert_heartbeat(session: Session, *, at: datetime) -> None:
    """Insert a fresh ``worker_heartbeat`` row through the test session.

    Routed through the SAVEPOINT-backed ``db_session`` so the outer
    rollback at fixture teardown reverts the row alongside any writes
    the handler's UoW makes. The flush is explicit — the handler reads
    via a sibling session on the same connection, and that session
    only sees rows the writer has already pushed to the connection.
    """
    session.execute(
        insert(WorkerHeartbeat).values(
            id=new_ulid(),
            worker_name="scheduler",
            heartbeat_at=at,
        ),
    )
    session.flush()


# ---------------------------------------------------------------------------
# Integration cases
# ---------------------------------------------------------------------------


class TestReadyzAgainstRealDb:
    """End-to-end ``/readyz`` against the migrated schema."""

    def test_readyz_200_when_heartbeat_fresh(
        self,
        pinned_settings: Settings,
        real_make_uow: None,
        db_session: Session,
    ) -> None:
        """Every check passes → 200 ``{status: ok, checks: []}``.

        Writes one ``worker_heartbeat`` row at "now"; the real
        alembic heads read against the migrated schema must match
        the in-repo script head; the pinned :class:`Settings` has a
        non-empty root key. No other setup required.
        """
        _insert_heartbeat(db_session, at=datetime.now(UTC))

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
        db_session: Session,
    ) -> None:
        """Heartbeat older than 60 s → 503 with ``heartbeat_stale``."""
        stale = datetime.now(UTC) - timedelta(seconds=120)
        _insert_heartbeat(db_session, at=stale)

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
