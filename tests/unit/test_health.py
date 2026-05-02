"""Unit tests for :mod:`app.api.health`.

These tests exercise the three ops probes (``/healthz``, ``/readyz``,
``/version``) against a :class:`~fastapi.testclient.TestClient` built
from :func:`~app.main.create_app`. Every DB-touching branch of
``/readyz`` is driven by patching :func:`app.api.health.make_uow` so
the suite stays hermetic — the integration layer at
``tests/integration/test_health.py`` covers the alembic-migrated
round-trip.

Covers (spec §16 "Healthchecks", spec §15 "Rate limiting"):

* ``/healthz`` — unconditional 200 even when the DB is down;
* ``/readyz`` — all four checks pass, each individual failure
  surfaces 503 with only the failing checks in the body;
* ``/version`` — env vars echoed when set, ``"unknown"`` fallback
  otherwise; package version mirrors ``importlib.metadata``.

See ``docs/specs/16-deployment-operations.md`` §"Healthchecks" and
``docs/specs/17-testing-quality.md`` §"Unit".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from types import TracebackType
from typing import Literal
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import app.api.health as health_module
import app.util.version as version_module
from app.api.health import router as health_router
from app.config import Settings
from app.util.clock import FrozenClock

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 4, 20, 11, 0, 0, tzinfo=UTC)

# Module-level singleton — ruff B008 forbids calling :class:`SecretStr`
# at arg-default time, so we build the default once here and pass it in
# via ``_DEFAULT_ROOT_KEY``.
_DEFAULT_ROOT_KEY: SecretStr = SecretStr("unit-test-root-key")


def _settings(
    *,
    root_key: SecretStr | None = _DEFAULT_ROOT_KEY,
    smtp_host: str | None = None,
    smtp_from: str | None = None,
    smtp_use_tls: bool = True,
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO",
) -> Settings:
    """Return a :class:`Settings` shaped for the ops-probe tests.

    Uses ``sqlite:///:memory:`` for ``database_url`` so the factory
    boots without env reads; individual readyz tests monkeypatch
    :func:`app.api.health.make_uow` to control the DB-open outcome.
    """
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=root_key,
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        smtp_host=smtp_host,
        smtp_from=smtp_from,
        smtp_use_tls=smtp_use_tls,
        log_level=log_level,
        cors_allow_origins=[],
    )


def _bare_app(settings: Settings, *, clock: FrozenClock | None = None) -> FastAPI:
    """Return a minimal :class:`FastAPI` mounting only the health router.

    Bypasses :func:`app.main.create_app` so the unit tests don't have
    to pay the full middleware + router wiring on every case. The
    handlers read ``app.state.settings`` and (optionally)
    ``app.state.clock`` — mirror the shape the factory installs.
    """
    app = FastAPI()
    app.state.settings = settings
    if clock is not None:
        app.state.clock = clock
    app.include_router(health_router)
    return app


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _fake_session(
    *,
    dialect_name: str = "sqlite",
    heartbeat: datetime | None = None,
    heartbeat_raises: Exception | None = None,
) -> MagicMock:
    """Build a :class:`MagicMock` that passes ``isinstance(x, Session)``.

    ``MagicMock(spec=Session)`` satisfies the production handler's
    ``isinstance(session, Session)`` narrow without a live engine
    while keeping mypy-strict happy (the spec attribute leaves
    :class:`MagicMock` typed as ``MagicMock``, not a broken
    ``Session`` subclass, so there's no LSP violation on our test
    overrides).

    Wires just the call sites the health probe uses:

    * :meth:`execute` — records into ``mock.executed``;
    * :meth:`get_bind` — returns a mock whose ``dialect.name`` is
      ``dialect_name``;
    * :meth:`connection` — returns a sentinel consumed by the patched
      :class:`MigrationContext.configure`;
    * :meth:`scalars` — the heartbeat probe reads
      ``session.scalars(...).first()`` which flows through here.
    """
    session = MagicMock(spec=Session)
    session.executed = []

    def _record_execute(stmt: object) -> object:
        session.executed.append(str(stmt))
        return MagicMock()

    session.execute.side_effect = _record_execute
    session.get_bind.return_value.dialect.name = dialect_name
    if heartbeat_raises is not None:
        session.scalars.side_effect = heartbeat_raises
    else:
        session.scalars.return_value.first.return_value = heartbeat
    return session


class _FakeUow:
    """Context manager that yields a pinned ``MagicMock`` session."""

    def __init__(
        self,
        session: MagicMock | None = None,
        *,
        enter_raises: Exception | None = None,
    ) -> None:
        self._session = session
        self._enter_raises = enter_raises

    def __enter__(self) -> MagicMock:
        if self._enter_raises is not None:
            raise self._enter_raises
        assert self._session is not None
        return self._session

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        return None


def _install_uow(
    monkeypatch: pytest.MonkeyPatch, uow: _FakeUow | MagicMock | Exception
) -> None:
    """Redirect :func:`app.api.health.make_uow` to ``uow``.

    Accepts a :class:`_FakeUow`, a bare ``MagicMock`` session (wrapped
    in a default :class:`_FakeUow`), or an :class:`Exception` (raised
    on ``make_uow()`` entry — matches a dead pool).
    """
    if isinstance(uow, Exception):
        err = uow

        def _raiser() -> _FakeUow:
            raise err

        monkeypatch.setattr(health_module, "make_uow", _raiser)
        return
    if isinstance(uow, MagicMock):
        uow = _FakeUow(uow)
    monkeypatch.setattr(health_module, "make_uow", lambda: uow)


def _pin_migrations_current(
    monkeypatch: pytest.MonkeyPatch,
    *,
    db_heads: set[str] | None = None,
    script_heads: set[str] | None = None,
    db_heads_raises: Exception | None = None,
    script_heads_raises: Exception | None = None,
) -> None:
    """Patch alembic heads reads to pin deterministic outputs.

    Defaults to ``{"h1"}`` on both sides (the happy "in sync" path).
    """
    db = db_heads if db_heads is not None else {"h1"}
    script = script_heads if script_heads is not None else {"h1"}

    class _FakeMigrationCtx:
        @staticmethod
        def configure(connection: object) -> _FakeMigrationCtx:
            return _FakeMigrationCtx()

        def get_current_heads(self) -> tuple[str, ...]:
            if db_heads_raises is not None:
                raise db_heads_raises
            return tuple(db)

    class _FakeScriptDir:
        @staticmethod
        def from_config(cfg: object) -> _FakeScriptDir:
            if script_heads_raises is not None:
                raise script_heads_raises
            return _FakeScriptDir()

        def get_heads(self) -> tuple[str, ...]:
            return tuple(script)

    monkeypatch.setattr(health_module, "MigrationContext", _FakeMigrationCtx)
    monkeypatch.setattr(health_module, "ScriptDirectory", _FakeScriptDir)


@pytest.fixture
def healthy_uow(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """Install a fake UoW where every readyz check passes."""
    fresh_heartbeat = _NOW - timedelta(seconds=5)
    session = _fake_session(heartbeat=fresh_heartbeat)
    _install_uow(monkeypatch, session)
    _pin_migrations_current(monkeypatch)
    yield session


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


class TestHealthz:
    """``/healthz`` — liveness, never touches the DB."""

    def test_returns_200_ok_json(self) -> None:
        resp = _client(_bare_app(_settings())).get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_healthz_untouched_when_db_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dead DB must not 5xx liveness."""
        _install_uow(monkeypatch, OperationalError("pool dead", None, Exception()))
        resp = _client(_bare_app(_settings())).get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_healthz_never_opens_uow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Assert the probe does not call :func:`make_uow` at all."""
        called = {"n": 0}

        def _sentinel() -> _FakeUow:
            called["n"] += 1
            return _FakeUow(_fake_session())

        monkeypatch.setattr(health_module, "make_uow", _sentinel)
        _client(_bare_app(_settings())).get("/healthz")
        assert called["n"] == 0


# ---------------------------------------------------------------------------
# /readyz — happy path
# ---------------------------------------------------------------------------


class TestReadyzAllPass:
    """All four checks pass → 200 with ``checks: []``."""

    def test_returns_200_when_every_check_passes(self, healthy_uow: MagicMock) -> None:
        app = _bare_app(_settings(), clock=FrozenClock(_NOW))
        resp = _client(app).get("/readyz")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"status": "ok", "checks": []}

    def test_db_ping_emitted_once(self, healthy_uow: MagicMock) -> None:
        """``SELECT 1`` lands exactly once per probe."""
        app = _bare_app(_settings(), clock=FrozenClock(_NOW))
        _client(app).get("/readyz")
        # One SELECT 1; SQLite dialect path skips the statement-timeout.
        select_ones = [s for s in healthy_uow.executed if "SELECT 1" in s]
        assert len(select_ones) == 1


# ---------------------------------------------------------------------------
# /readyz — individual check failures
# ---------------------------------------------------------------------------


class TestReadyzDbDown:
    """DB unreachable → three DB-backed failures in one probe."""

    def test_db_open_raises_surfaces_503(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_uow(monkeypatch, OperationalError("cannot connect", None, Exception()))
        _pin_migrations_current(monkeypatch)
        app = _bare_app(_settings(), clock=FrozenClock(_NOW))
        resp = _client(app).get("/readyz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        check_names = {c["check"] for c in body["checks"]}
        assert {"db", "migrations", "worker_heartbeat"}.issubset(check_names)

    def test_db_down_body_lists_only_failing_checks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Root key is OK → must not appear in the 503 body."""
        _install_uow(monkeypatch, OperationalError("cannot connect", None, Exception()))
        _pin_migrations_current(monkeypatch)
        app = _bare_app(_settings(), clock=FrozenClock(_NOW))
        resp = _client(app).get("/readyz")
        body = resp.json()
        names = {c["check"] for c in body["checks"]}
        assert "root_key" not in names


class TestReadyzMigrationsBehind:
    """``alembic_version`` heads mismatch the script tree → 503."""

    def test_db_heads_empty_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = _fake_session(heartbeat=_NOW - timedelta(seconds=5))
        _install_uow(monkeypatch, session)
        _pin_migrations_current(monkeypatch, db_heads=set())
        app = _bare_app(_settings(), clock=FrozenClock(_NOW))
        resp = _client(app).get("/readyz")
        assert resp.status_code == 503
        body = resp.json()
        names = {c["check"] for c in body["checks"]}
        assert names == {"migrations"}
        migrations_row = next(c for c in body["checks"] if c["check"] == "migrations")
        assert migrations_row["detail"] == "alembic_version_empty"

    def test_db_heads_differ_from_script_heads_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _fake_session(heartbeat=_NOW - timedelta(seconds=5))
        _install_uow(monkeypatch, session)
        _pin_migrations_current(
            monkeypatch, db_heads={"old_rev"}, script_heads={"new_rev"}
        )
        app = _bare_app(_settings(), clock=FrozenClock(_NOW))
        resp = _client(app).get("/readyz")
        assert resp.status_code == 503
        row = next(c for c in resp.json()["checks"] if c["check"] == "migrations")
        assert row["detail"] == "migrations_behind"

    def test_alembic_version_read_fault_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A SQLAlchemy error on the heads read must 503, not bubble."""
        session = _fake_session(heartbeat=_NOW - timedelta(seconds=5))
        _install_uow(monkeypatch, session)
        _pin_migrations_current(
            monkeypatch,
            db_heads_raises=OperationalError("no table", None, Exception()),
        )
        app = _bare_app(_settings(), clock=FrozenClock(_NOW))
        resp = _client(app).get("/readyz")
        assert resp.status_code == 503
        row = next(c for c in resp.json()["checks"] if c["check"] == "migrations")
        assert row["detail"] == "alembic_version_unreadable"

    def test_alembic_command_error_on_db_heads_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``CommandError`` from the db-heads read must 503, not bubble."""
        from alembic.util.exc import CommandError

        session = _fake_session(heartbeat=_NOW - timedelta(seconds=5))
        _install_uow(monkeypatch, session)
        _pin_migrations_current(
            monkeypatch,
            db_heads_raises=CommandError("unknown revision 'deadbeef'"),
        )
        app = _bare_app(_settings(), clock=FrozenClock(_NOW))
        resp = _client(app).get("/readyz")
        assert resp.status_code == 503
        row = next(c for c in resp.json()["checks"] if c["check"] == "migrations")
        assert row["detail"] == "alembic_version_unreadable"

    def test_alembic_script_tree_command_error_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``CommandError`` from the script-tree read must 503, not bubble.

        Alembic raises ``CommandError`` (a bare ``Exception`` subclass,
        not ``RuntimeError`` / ``OSError``) for a broken migrations
        tree — multiple unmerged heads, missing ``depends_on`` rev,
        or a partial checkout. Without the explicit catch the probe
        would 500 instead of surfacing a coherent 503.
        """
        from alembic.util.exc import CommandError

        session = _fake_session(heartbeat=_NOW - timedelta(seconds=5))
        _install_uow(monkeypatch, session)
        _pin_migrations_current(
            monkeypatch,
            script_heads_raises=CommandError("Multiple heads are present"),
        )
        app = _bare_app(_settings(), clock=FrozenClock(_NOW))
        resp = _client(app).get("/readyz")
        assert resp.status_code == 503
        row = next(c for c in resp.json()["checks"] if c["check"] == "migrations")
        assert row["detail"] == "alembic_script_tree_unreadable"


class TestReadyzHeartbeat:
    """``worker_heartbeat`` MUST be newer than the freshness window."""

    def test_empty_heartbeat_table_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = _fake_session(heartbeat=None)
        _install_uow(monkeypatch, session)
        _pin_migrations_current(monkeypatch)
        app = _bare_app(_settings(), clock=FrozenClock(_NOW))
        resp = _client(app).get("/readyz")
        assert resp.status_code == 503
        row = next(c for c in resp.json()["checks"] if c["check"] == "worker_heartbeat")
        assert row["detail"] == "no_heartbeat"

    def test_stale_heartbeat_over_60s_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = _NOW - timedelta(seconds=61)
        session = _fake_session(heartbeat=stale)
        _install_uow(monkeypatch, session)
        _pin_migrations_current(monkeypatch)
        app = _bare_app(_settings(), clock=FrozenClock(_NOW))
        resp = _client(app).get("/readyz")
        assert resp.status_code == 503
        row = next(c for c in resp.json()["checks"] if c["check"] == "worker_heartbeat")
        assert row["detail"] == "heartbeat_stale"

    def test_boundary_60s_exactly_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """60 s exactly is the **last** accepted instant (inclusive boundary)."""
        at_boundary = _NOW - timedelta(seconds=60)
        session = _fake_session(heartbeat=at_boundary)
        _install_uow(monkeypatch, session)
        _pin_migrations_current(monkeypatch)
        app = _bare_app(_settings(), clock=FrozenClock(_NOW))
        resp = _client(app).get("/readyz")
        assert resp.status_code == 200

    def test_naive_heartbeat_treated_as_utc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SQLite can round-trip naive datetimes; the probe normalises."""
        fresh_naive = (_NOW - timedelta(seconds=5)).replace(tzinfo=None)
        session = _fake_session(heartbeat=fresh_naive)
        _install_uow(monkeypatch, session)
        _pin_migrations_current(monkeypatch)
        app = _bare_app(_settings(), clock=FrozenClock(_NOW))
        resp = _client(app).get("/readyz")
        assert resp.status_code == 200


class TestReadyzRootKey:
    """The root key must be populated on :class:`Settings`."""

    def test_missing_root_key_fails(
        self, monkeypatch: pytest.MonkeyPatch, healthy_uow: MagicMock
    ) -> None:
        app = _bare_app(_settings(root_key=None), clock=FrozenClock(_NOW))
        resp = _client(app).get("/readyz")
        assert resp.status_code == 503
        names = {c["check"] for c in resp.json()["checks"]}
        assert names == {"root_key"}

    def test_empty_root_key_string_fails(
        self, monkeypatch: pytest.MonkeyPatch, healthy_uow: MagicMock
    ) -> None:
        """A :class:`SecretStr` wrapping the empty string is not "set"."""
        app = _bare_app(_settings(root_key=SecretStr("")), clock=FrozenClock(_NOW))
        resp = _client(app).get("/readyz")
        assert resp.status_code == 503
        row = next(c for c in resp.json()["checks"] if c["check"] == "root_key")
        assert row["detail"] == "root_key_missing"


class TestReadyzBodyShape:
    """The 503 body lists ONLY failing checks + the ``status`` field."""

    def test_single_failure_lists_only_that_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the heartbeat is stale → body mentions only ``worker_heartbeat``."""
        stale = _NOW - timedelta(seconds=120)
        session = _fake_session(heartbeat=stale)
        _install_uow(monkeypatch, session)
        _pin_migrations_current(monkeypatch)
        app = _bare_app(_settings(), clock=FrozenClock(_NOW))
        body = _client(app).get("/readyz").json()
        names = {c["check"] for c in body["checks"]}
        assert names == {"worker_heartbeat"}
        assert body["status"] == "degraded"


# ---------------------------------------------------------------------------
# /version
# ---------------------------------------------------------------------------


class TestVersion:
    """``/version`` — env vars + pyproject version, "unknown" fallbacks."""

    def test_returns_installed_version_when_package_present(self) -> None:
        resp = _client(_bare_app(_settings())).get("/version")
        assert resp.status_code == 200
        body = resp.json()
        try:
            expected = pkg_version("crewday")
        except PackageNotFoundError:
            expected = "unknown"
        assert body["version"] == expected

    def test_version_falls_back_when_package_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(_name: str) -> str:
            raise PackageNotFoundError("crewday")

        # ``resolve_package_version`` lives in ``app.util.version`` and
        # both the factory and ``/version`` import it from there; patch
        # the helper's underlying ``importlib.metadata`` lookup so both
        # call sites observe the missing-package branch.
        monkeypatch.setattr(version_module, "_pkg_version", _raise)
        body = _client(_bare_app(_settings())).get("/version").json()
        assert body["version"] == "unknown"

    def test_env_vars_echoed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CREWDAY_GIT_SHA", "abc123")
        monkeypatch.setenv("CREWDAY_BUILD_AT", "2026-04-20T10:00:00Z")
        monkeypatch.setenv("CREWDAY_IMAGE_DIGEST", "sha256:deadbeef")
        body = _client(_bare_app(_settings())).get("/version").json()
        assert body["git_sha"] == "abc123"
        assert body["build_at"] == "2026-04-20T10:00:00Z"
        assert body["image_digest"] == "sha256:deadbeef"

    def test_env_vars_unknown_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CREWDAY_GIT_SHA", raising=False)
        monkeypatch.delenv("CREWDAY_BUILD_AT", raising=False)
        monkeypatch.delenv("CREWDAY_IMAGE_DIGEST", raising=False)
        body = _client(_bare_app(_settings())).get("/version").json()
        assert body["git_sha"] == "unknown"
        assert body["build_at"] == "unknown"
        assert body["image_digest"] == "unknown"

    def test_empty_env_var_falls_back_to_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty-string env var must not surface as ``""`` in the body."""
        monkeypatch.setenv("CREWDAY_GIT_SHA", "")
        body = _client(_bare_app(_settings())).get("/version").json()
        assert body["git_sha"] == "unknown"

    def test_version_payload_shape(self) -> None:
        body = _client(_bare_app(_settings())).get("/version").json()
        assert set(body.keys()) == {"version", "git_sha", "build_at", "image_digest"}
