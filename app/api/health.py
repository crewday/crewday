"""Ops probes — ``/healthz``, ``/readyz``, ``/version``.

Three unconditional endpoints the reverse proxy, container
orchestrator, and ``crewday admin status`` CLI scrape to decide
whether the process is alive and ready to serve traffic (spec §16
"Healthchecks").

* ``/healthz`` — process liveness. Returns 200 the moment the ASGI
  server is accepting requests. **Never touches the DB** — a degraded
  database must not 5xx liveness or the reverse proxy will kill a
  process that was otherwise working (cache reads, static responses,
  recovery paths).
* ``/readyz`` — full readiness gate. 200 only when every dependency
  the request-serving path needs is up: DB reachable, schema at head,
  the background worker has bumped its heartbeat within the last 60 s,
  and the root key is loaded. Any failure surfaces 503 with a
  ``checks`` list of only the failing probes — operators + dashboards
  can see exactly what's wrong without trawling logs.
* ``/version`` — the build metadata baked into the image by the
  release pipeline. Version comes from ``importlib.metadata``; git
  sha / build timestamp / image digest come from env vars
  (``CREWDAY_GIT_SHA`` / ``CREWDAY_BUILD_AT`` /
  ``CREWDAY_IMAGE_DIGEST``). Each falls back to ``"unknown"`` when
  unset so dev shells render sensibly without a release process.

**Tenancy.** All three paths live in
:data:`app.tenancy.middleware.SKIP_PATHS` so the tenancy middleware
passes them through without resolving a workspace. The
:class:`~app.adapters.db.ops.models.WorkerHeartbeat` read is
deployment-wide (not workspace-scoped), so the tenancy filter leaves
it alone too.

**Rate limiting** (spec §15 "Rate limiting"). Ops probes are
unlimited. ``/healthz`` does zero work; ``/readyz`` runs its DB
reads with explicit query-level timeouts and a tight ``LIMIT 1`` so a
degraded DB can't turn a probe into a DoS lever.

See ``docs/specs/16-deployment-operations.md`` §"Recipe A",
§"Healthchecks"; ``docs/specs/15-security-privacy.md`` §"Rate
limiting".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Final

from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapters.db.ops.models import WorkerHeartbeat
from app.adapters.db.session import make_uow
from app.config import Settings
from app.tenancy.current import tenant_agnostic
from app.util.clock import Clock, SystemClock

__all__ = ["router"]

_log = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])

# Package name read by ``/version``. Matches ``pyproject.toml``
# ``[project].name`` — kept local so a rename lands in one place here
# without coupling to :mod:`app.main`'s copy (both resolve the same
# string, that's fine).
_PACKAGE_NAME: Final[str] = "crewday"

# Sentinel for ``/version`` fields when the package/env var isn't
# populated. Matches :mod:`app.main`'s fallback shape so dev shells
# without a release pipeline still render a coherent payload.
_UNKNOWN: Final[str] = "unknown"

# Env var names the release pipeline populates before baking the
# image. Resolving at request time (not at import time) keeps the
# factory test-friendly — a test can ``monkeypatch.setenv`` and the
# next probe reflects the change without rebuilding the app.
_GIT_SHA_ENV: Final[str] = "CREWDAY_GIT_SHA"
_BUILD_AT_ENV: Final[str] = "CREWDAY_BUILD_AT"
_IMAGE_DIGEST_ENV: Final[str] = "CREWDAY_IMAGE_DIGEST"

# Freshness window for the worker heartbeat. A worker bumps
# ``worker_heartbeat.heartbeat_at`` every 30 s; readiness fails when
# no row is newer than this window (§16 "Healthchecks").
_HEARTBEAT_STALE_AFTER: Final[timedelta] = timedelta(seconds=60)

# Alembic config file the release image ships. Resolved lazily (not
# at import time) because tests may monkeypatch
# :attr:`~pathlib.Path.is_file` — keeping the call site inside the
# handler keeps test surface flat.
_ALEMBIC_INI: Final[Path] = Path(__file__).resolve().parents[2] / "alembic.ini"


# ---------------------------------------------------------------------------
# Check result value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CheckResult:
    """Outcome of one readiness probe.

    Pinned fields only — the wire shape is
    ``{"check": str, "ok": bool, "detail": str | None}``. ``detail``
    is populated on failures so operators can distinguish
    "db_unreachable" from "migrations_behind" without grepping logs.
    """

    check: str
    ok: bool
    detail: str | None


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    """Process liveness — unconditional 200.

    The ASGI server answering this path is the only signal. No DB,
    no disk, no auth, no tenancy — a crashed DB must not turn a
    healthy process into a restart loop.
    """
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# /readyz
# ---------------------------------------------------------------------------


@router.get("/readyz", include_in_schema=False)
def readyz(request: Request) -> JSONResponse:
    """Full readiness gate — DB, migrations, worker HB, root key.

    Returns 200 with ``{"status": "ok"}`` when every check passes.
    Otherwise 503 with ``{"status": "degraded", "checks": [...]}``
    listing **only the failing checks** so a dashboard can colour by
    ``checks[].check`` without post-filtering.

    Each check is wrapped in a narrow ``except`` so one probe's crash
    can't mask another's signal. The DB-ping check opens a fresh
    :class:`~app.adapters.db.session.UnitOfWorkImpl` so a crashed pool
    recovers on the next scrape (no stale connection pinning across
    probes).
    """
    settings = _settings_from_request(request)
    clock = _clock_from_request(request)
    now = clock.now()

    failing: list[_CheckResult] = []

    # Root-key check — pure settings read, runs first so a mis-
    # configured deployment fails readiness even when the DB is down.
    if not _check_root_key(settings):
        failing.append(
            _CheckResult(check="root_key", ok=False, detail="root_key_missing"),
        )

    # The three DB-backed checks share one UoW so a down database
    # surfaces as three failures in one probe (matches the 503 body
    # schema — every failing check is listed).
    db_result = _check_db_and_migrations_and_heartbeat(now=now)
    failing.extend(r for r in db_result if not r.ok)

    if failing:
        _log.warning(
            "readyz: failing checks",
            extra={
                "event": "ops.readyz.degraded",
                "failing": [r.check for r in failing],
            },
        )
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "checks": [
                    {"check": r.check, "ok": r.ok, "detail": r.detail} for r in failing
                ],
            },
        )

    return JSONResponse(status_code=200, content={"status": "ok", "checks": []})


# ---------------------------------------------------------------------------
# /version
# ---------------------------------------------------------------------------


@router.get("/version", include_in_schema=False)
def version() -> dict[str, str]:
    """Return the build metadata baked by the release pipeline.

    Every field has a ``"unknown"`` fallback so a dev shell without
    a release build returns a shape-complete payload.
    """
    return {
        "version": _resolve_version(),
        "git_sha": os.environ.get(_GIT_SHA_ENV, _UNKNOWN) or _UNKNOWN,
        "build_at": os.environ.get(_BUILD_AT_ENV, _UNKNOWN) or _UNKNOWN,
        "image_digest": os.environ.get(_IMAGE_DIGEST_ENV, _UNKNOWN) or _UNKNOWN,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_version() -> str:
    """Return the installed package version or ``"unknown"``.

    Falls back when running from a non-installed source checkout
    (e.g. under pytest without ``pip install -e .``). We swallow the
    specific :class:`PackageNotFoundError` narrowly — any other
    :mod:`importlib.metadata` failure is a bug we want to see.
    """
    try:
        return pkg_version(_PACKAGE_NAME)
    except PackageNotFoundError:
        return _UNKNOWN


def _settings_from_request(request: Request) -> Settings:
    """Pull the :class:`Settings` stashed on ``app.state`` by the factory.

    Going through ``app.state`` rather than calling
    :func:`~app.config.get_settings` directly keeps the handler
    test-friendly: a test that pins a fresh :class:`Settings` via
    ``create_app(settings=...)`` has that exact instance reach the
    probe instead of the lru_cached default.
    """
    settings = request.app.state.settings
    assert isinstance(settings, Settings)
    return settings


def _clock_from_request(request: Request) -> Clock:
    """Return the :class:`Clock` stashed on ``app.state`` or the system clock.

    The factory doesn't install a clock by default; tests that want
    deterministic timing stamp one onto ``app.state.clock`` via a
    ``monkeypatch`` so the heartbeat-staleness branch can be
    exercised without wall-clock dependency.
    """
    clock = getattr(request.app.state, "clock", None)
    if isinstance(clock, Clock):
        return clock
    return SystemClock()


def _check_root_key(settings: Settings) -> bool:
    """Return ``True`` when ``CREWDAY_ROOT_KEY`` is populated.

    The root key drives every HKDF subkey (session cookies, magic
    link pepper, email hash, ...). A deployment that boots without
    one is technically alive but cannot serve authenticated traffic —
    readiness says "don't route to me".
    """
    root_key = settings.root_key
    if root_key is None:
        return False
    return bool(root_key.get_secret_value())


def _check_db_and_migrations_and_heartbeat(*, now: datetime) -> list[_CheckResult]:
    """Open one UoW and run the three DB-backed checks.

    Returns a list of three :class:`_CheckResult` — one per check.
    On a DB open failure, the migrations + heartbeat checks don't
    re-attempt the connection; they short-circuit to a failure with
    a pinned ``detail`` so the probe body still lists every missing
    dependency.
    """
    # DB reachability — ``SELECT 1`` bounded to 100 ms so a hung
    # server can't stall the probe. The statement-level timeout is
    # dialect-specific:
    #   * SQLite ignores the hint entirely (local file, no wait).
    #   * Postgres honours ``statement_timeout`` as a session var.
    # We emit it for both via ``SET LOCAL`` where supported; the
    # SQLite path silently drops the instruction (parsed-but-ignored
    # by the ``sqlite`` DBAPI).
    try:
        with make_uow() as session:
            assert isinstance(session, Session)
            _apply_statement_timeout_ms(session, 100)
            session.execute(text("SELECT 1"))

            db_result = _CheckResult(check="db", ok=True, detail=None)
            migrations_result = _check_migrations_current(session)
            heartbeat_result = _check_worker_heartbeat(session, now=now)
    except SQLAlchemyError as exc:
        _log.warning(
            "readyz: db probe failed",
            extra={"event": "ops.readyz.db_error", "error": repr(exc)},
        )
        # DB is down — the other DB-backed checks can't run either.
        # Emit three failures so the 503 body is honest about what's
        # missing, not just the first one.
        return [
            _CheckResult(check="db", ok=False, detail="db_unreachable"),
            _CheckResult(
                check="migrations",
                ok=False,
                detail="db_unreachable",
            ),
            _CheckResult(
                check="worker_heartbeat",
                ok=False,
                detail="db_unreachable",
            ),
        ]

    return [db_result, migrations_result, heartbeat_result]


def _apply_statement_timeout_ms(session: Session, timeout_ms: int) -> None:
    """Emit a best-effort per-statement timeout on ``session``.

    Postgres: ``SET LOCAL statement_timeout`` binds to the current
    transaction and reverts on commit/rollback. SQLite: no-op (the
    statement is parsed but has no effect); the local file gives us
    effective-zero latency anyway.
    """
    bind = session.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        session.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))


def _check_migrations_current(session: Session) -> _CheckResult:
    """Return ``_CheckResult`` for the "schema at alembic head" gate.

    Uses :class:`alembic.runtime.migration.MigrationContext` against a
    live connection to read the ``alembic_version`` row(s), and
    :class:`alembic.script.ScriptDirectory` to resolve the versioned
    heads the shipped code expects. A deployment is "current" when
    the DB heads set == script heads set.

    Any error from the alembic read — missing ``alembic_version``
    table, unreachable script tree, IO fault — surfaces as a failed
    check with a pinned ``detail``; we don't let alembic's
    exceptions bubble past the probe.
    """
    try:
        connection = session.connection()
        ctx = MigrationContext.configure(connection=connection)
        db_heads: set[str] = set(ctx.get_current_heads())
    except (SQLAlchemyError, CommandError) as exc:
        # ``SQLAlchemyError`` covers the common "alembic_version table
        # missing / unreadable" path; ``CommandError`` is the narrow
        # alembic-specific wrapper it raises when a head recorded in
        # the DB can't be resolved against the (out-of-band) script
        # tree. Both must 503 — never bubble to 500.
        _log.warning(
            "readyz: migration head read failed",
            extra={"event": "ops.readyz.migrations_read_error", "error": repr(exc)},
        )
        return _CheckResult(
            check="migrations",
            ok=False,
            detail="alembic_version_unreadable",
        )

    if not db_heads:
        return _CheckResult(
            check="migrations",
            ok=False,
            detail="alembic_version_empty",
        )

    try:
        script_dir = ScriptDirectory.from_config(
            AlembicConfig(str(_ALEMBIC_INI)),
        )
        script_heads: set[str] = set(script_dir.get_heads())
    except (OSError, RuntimeError, CommandError) as exc:
        # Alembic raises ``RuntimeError`` for missing script tree and
        # ``alembic.util.exc.CommandError`` for script-tree issues
        # (multiple unmerged heads, broken ``depends_on``, missing
        # revision files). ``OSError`` covers IO faults. None should
        # ever fire in a well-built image, but failing closed — with a
        # 503 instead of a bubbled 500 — is the right call so the
        # probe stays a reliable signal.
        _log.warning(
            "readyz: alembic script tree unreadable",
            extra={"event": "ops.readyz.script_tree_error", "error": repr(exc)},
        )
        return _CheckResult(
            check="migrations",
            ok=False,
            detail="alembic_script_tree_unreadable",
        )

    if db_heads != script_heads:
        return _CheckResult(
            check="migrations",
            ok=False,
            detail="migrations_behind",
        )
    return _CheckResult(check="migrations", ok=True, detail=None)


def _check_worker_heartbeat(session: Session, *, now: datetime) -> _CheckResult:
    """Return ``_CheckResult`` for the worker-heartbeat freshness gate.

    ``worker_heartbeat`` is cross-tenant ops plumbing, so the read
    runs inside :func:`tenant_agnostic`. ``MAX(heartbeat_at)`` is a
    single-row aggregate — cheap on an empty table, constant-time on
    a populated one (the table holds one row per named worker, not
    one row per tick).
    """
    stmt = select(func.max(WorkerHeartbeat.heartbeat_at))
    # justification: worker_heartbeat is deployment-wide ops plumbing
    # (not workspace-scoped); readyz runs before any WorkspaceContext.
    with tenant_agnostic():
        latest = session.scalars(stmt).first()

    if latest is None:
        return _CheckResult(
            check="worker_heartbeat",
            ok=False,
            detail="no_heartbeat",
        )

    # Normalise to aware UTC. SQLite's ``DateTime(timezone=True)``
    # round-trips timezone info only when the driver is configured
    # to do so; defensively attach UTC when a naive value comes back
    # so the arithmetic below is well-defined.
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)

    if now - latest > _HEARTBEAT_STALE_AFTER:
        return _CheckResult(
            check="worker_heartbeat",
            ok=False,
            detail="heartbeat_stale",
        )
    return _CheckResult(check="worker_heartbeat", ok=True, detail=None)
