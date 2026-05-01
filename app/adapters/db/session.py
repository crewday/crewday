"""Synchronous SQLAlchemy engine, session factory, and Unit-of-Work.

The DB seam is **sync-only** for now. Async can come later under a
separate port if a use case forces it; most domain code is request-
scoped and fine with threadpool-backed sync SQLAlchemy.

Public surface:

* :func:`make_engine` — build a sync :class:`~sqlalchemy.engine.Engine`
  from a URL, normalising async driver prefixes.
* :class:`UnitOfWorkImpl` — concrete ``UnitOfWork`` adapter.
* :func:`make_uow` — convenience factory bound to the default engine.

See ``docs/specs/01-architecture.md`` §"Adapters".
"""

from __future__ import annotations

import logging
import sqlite3
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import TracebackType

from sqlalchemy import Engine, create_engine, event, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import Pool, StaticPool

from app.adapters.db.ports import DbSession
from app.config import get_settings
from app.tenancy.orm_filter import install_tenant_filter

__all__ = [
    "FilteredSession",
    "UnitOfWorkImpl",
    "bind_active_session",
    "get_active_session",
    "make_engine",
    "make_uow",
    "normalise_sync_url",
]

_log = logging.getLogger(__name__)

# Async-driver prefixes we strip when a sync engine is being built. The
# ``.env.example`` template uses ``sqlite+aiosqlite://`` / ``postgresql+
# asyncpg://`` for future-proofing; the sync factory silently rewrites
# them to the matching sync drivers so dev doesn't need to maintain two
# URLs in parallel.
_ASYNC_TO_SYNC_DRIVER: dict[str, str] = {
    "sqlite+aiosqlite": "sqlite",
    "postgresql+asyncpg": "postgresql+psycopg",
    "postgresql+aiopg": "postgresql+psycopg",
}


def normalise_sync_url(url: str) -> str:
    """Rewrite async driver prefixes to their sync equivalents.

    Leaves plain ``sqlite://``, ``postgresql://``,
    ``postgresql+psycopg://`` etc. untouched. The rewrite is logged at
    INFO once per call so the operator can tell what ended up on the
    wire.
    """
    parsed = make_url(url)
    sync_driver = _ASYNC_TO_SYNC_DRIVER.get(parsed.drivername)
    if sync_driver is None:
        return url
    rewritten = parsed.set(drivername=sync_driver)
    _log.info(
        "db: async driver %r rewritten to sync %r for UnitOfWork",
        parsed.drivername,
        sync_driver,
    )
    return rewritten.render_as_string(hide_password=False)


def make_engine(url: str | None = None) -> Engine:
    """Return a sync :class:`~sqlalchemy.engine.Engine` for ``url``.

    ``url`` defaults to :attr:`~app.config.Settings.database_url`. Async
    driver prefixes (``+aiosqlite``, ``+asyncpg``) are rewritten to sync
    equivalents — see :data:`_ASYNC_TO_SYNC_DRIVER` — with an INFO log
    so operators can tell.

    Dialect-specific tuning:

    * **SQLite in-memory** (``sqlite:///:memory:``): use
      :class:`~sqlalchemy.pool.StaticPool` and
      ``check_same_thread=False`` so every checkout sees the *same*
      in-memory database across threads. A fresh pool per checkout
      would hand out empty databases.
    * **SQLite file**: ``check_same_thread=False`` so requests on
      worker threads can reuse a connection; default pool otherwise.
    * **Postgres and everything else**: defaults.
    """
    resolved = url if url is not None else get_settings().database_url
    normalised = normalise_sync_url(resolved)
    parsed = make_url(normalised)

    if parsed.drivername.startswith("sqlite"):
        connect_args: dict[str, object] = {"check_same_thread": False}
        is_memory = parsed.database in (None, "", ":memory:")
        if is_memory:
            engine = create_engine(
                normalised,
                connect_args=connect_args,
                poolclass=StaticPool,
                future=True,
            )
        else:
            engine = create_engine(normalised, connect_args=connect_args, future=True)
        _enable_sqlite_foreign_keys(engine)
        return _dispose_pool_on_gc(engine)

    return _dispose_pool_on_gc(create_engine(normalised, future=True))


def _dispose_pool_on_gc(engine: Engine) -> Engine:
    weakref.finalize(engine, _dispose_pool, engine.pool)
    return engine


def _dispose_pool(pool: Pool) -> None:
    pool.dispose()


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    """Issue ``PRAGMA foreign_keys=ON`` on every SQLite connection.

    SQLite ships FK enforcement OFF by default for backwards
    compatibility; without this hook the ``ON DELETE CASCADE`` and
    referential-integrity constraints on ``user_workspace`` etc. silently
    do nothing. The pragma is per-connection, so we re-issue it on each
    ``connect`` event the pool hands back. Postgres and other dialects
    ignore this path entirely (the caller guards by drivername).
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(
        dbapi_connection: object, _connection_record: object
    ) -> None:
        if not isinstance(dbapi_connection, sqlite3.Connection):
            # Defensive: ``sqlite+aiosqlite`` and other drivers wrap the
            # DBAPI connection; the pragma statement is safe but we skip
            # anything we don't recognise so Postgres can never trip this.
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


class FilteredSession(Session):
    """:class:`~sqlalchemy.orm.Session` subclass with the tenant filter listener.

    The :func:`~app.tenancy.orm_filter.install_tenant_filter` hook is
    registered on this class exactly once, at module import (see the
    call below the class definition). Production code uses
    ``sessionmaker(..., class_=FilteredSession)`` so every session
    spawned from the default factory inherits the listener — no
    per-sessionmaker registration required.

    Why a class-level install instead of per-sessionmaker:

    cd-nf8p (this fix) recorded a latent SQLAlchemy heisenbug where
    building multiple ``sessionmaker`` instances against the same
    engine could leave a fresh session's ``do_orm_execute`` listener
    list empty even when ``event.contains(factory, ...)`` returned
    ``True``. Installing on the ``Session`` subclass instead makes
    listener attachment immune to per-sessionmaker dispatch churn —
    every ``FilteredSession`` instance carries the listener via the
    class dispatch, regardless of which factory built it.
    """


# Lazy module-level defaults. We do NOT build the engine at import time:
# ``Settings`` requires ``CREWDAY_DATABASE_URL`` in env, so eager
# construction would break test collection on machines where that
# isn't set. The first caller of :func:`_default_sessionmaker` pays
# the one-off cost; subsequent callers reuse the cached factory.
_default_engine: Engine | None = None
_default_sessionmaker_: sessionmaker[FilteredSession] | None = None


# Install the tenant filter on the ``FilteredSession`` class exactly
# once at import time. The :class:`weakref.WeakSet`-based idempotence
# in :func:`install_tenant_filter` makes re-imports safe (the second
# call sees the already-installed class and no-ops). Doing this here
# — rather than inside :func:`_default_sessionmaker` — closes the
# cd-nf8p heisenbug: any production code path that builds multiple
# sessionmakers against the same engine would otherwise risk losing
# the listener silently.
install_tenant_filter(FilteredSession)


# Active publishing session carrier. Synchronous publishers (today:
# the iCal poll worker, plus integration tests) wrap their open
# :class:`Session` with :func:`bind_active_session` so any in-flight
# event-bus subscriber driven by the publisher's ``flush()`` can fetch
# the same session without threading it as an argument. Mirrors the
# :data:`app.tenancy.current._current_ctx` carrier — the two
# context-vars together let a subscriber recover the publisher's
# (session, ctx) without a new DI container.
#
# This is intentionally NOT bound from :class:`UnitOfWorkImpl`: the
# UoW yields a session into FastAPI dep generators that cross asyncio
# thread-pool boundaries, where the :class:`~contextvars.Token`-based
# reset would fire in the wrong context. The explicit
# :func:`bind_active_session` helper keeps the binding scoped to a
# synchronous publish region the caller controls.
_active_session: ContextVar[Session | None] = ContextVar(
    "crewday_active_session",
    default=None,
)


def get_active_session() -> Session | None:
    """Return the active publishing session for this task, or ``None``."""
    return _active_session.get()


@contextmanager
def bind_active_session(session: Session) -> Iterator[None]:
    """Bind ``session`` for the duration of the ``with`` block.

    Use this around synchronous publish points (the iCal poll worker,
    integration tests) so subscribers wired by the FastAPI factory's
    ``_register_stays_subscriptions`` can recover the publishing
    session via :func:`get_active_session` without a DI container.

    The binding is task-local. When the publish path crosses an
    asyncio thread-pool boundary (FastAPI ``db_session`` dep) the
    :class:`~contextvars.Token`-based reset can fire from a different
    context than the ``set``; we set + restore the previous value
    inline rather than rely on token semantics so the helper is safe
    in any execution model.
    """
    previous = _active_session.get()
    _active_session.set(session)
    try:
        yield
    finally:
        _active_session.set(previous)


def _default_sessionmaker() -> sessionmaker[FilteredSession]:
    """Return the process-wide default ``sessionmaker``, building on first use.

    The factory is bound to :class:`FilteredSession`, which already
    carries the tenancy ``do_orm_execute`` hook (installed once at
    module import). Every session spawned from this factory therefore
    auto-injects the ``workspace_id`` filter without needing a
    per-sessionmaker :func:`install_tenant_filter` call — see
    :class:`FilteredSession` for the cd-nf8p rationale.

    UoW unit tests that build their own ``sessionmaker`` with a plain
    :class:`~sqlalchemy.orm.Session` class bypass this entirely and
    don't get the hook — they exercise raw transaction mechanics
    against tables that are not registered as scoped, so the un-hooked
    path is safe there.
    """
    global _default_engine, _default_sessionmaker_
    if _default_sessionmaker_ is None:
        _default_engine = make_engine()
        _default_sessionmaker_ = sessionmaker(
            bind=_default_engine,
            expire_on_commit=False,
            class_=FilteredSession,
        )
    return _default_sessionmaker_


class UnitOfWorkImpl:
    """Concrete :class:`~app.adapters.db.ports.UnitOfWork`.

    Opens a fresh :class:`~sqlalchemy.orm.Session` on ``__enter__``,
    commits on a clean exit, rolls back on an exception, and always
    closes. Exceptions are never swallowed — ``__exit__`` returns
    ``None``.

    Construct one with :func:`make_uow` for production code; pass a
    custom ``session_factory`` for tests that need an isolated engine.
    """

    __slots__ = ("_factory", "_session")

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._factory = session_factory
        self._session: Session | None = None

    def __enter__(self) -> DbSession:
        if self._session is not None:
            raise RuntimeError("UnitOfWorkImpl is not reentrant")
        factory = (
            self._factory if self._factory is not None else _default_sessionmaker()
        )
        self._session = factory()
        return self._session

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        session = self._session
        if session is None:  # pragma: no cover - defensive
            return None
        try:
            if exc_type is None:
                session.commit()
            else:
                session.rollback()
        finally:
            session.close()
            self._session = None
        return None


def make_uow() -> UnitOfWorkImpl:
    """Return a :class:`UnitOfWorkImpl` bound to the default engine."""
    return UnitOfWorkImpl()
