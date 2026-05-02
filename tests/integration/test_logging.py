"""Regression: alembic's ``fileConfig`` must not disable app loggers.

Alembic's ``migrations/env.py`` calls
:func:`logging.config.fileConfig` to wire console handlers off
``alembic.ini``. The stdlib default ``disable_existing_loggers=True``
flips ``disabled=True`` on every logger that already exists in
:data:`logging.Logger.manager.loggerDict` and is not enumerated under
``[loggers]`` in the ini (root/sqlalchemy/alembic). Once disabled, a
logger silently drops every record — ``caplog`` then sees nothing
even though the ``warning(...)`` call returned normally, breaking
unit tests that run after the session-scoped ``migrate_once`` fixture
has fired.

The fix is in ``migrations/env.py``: pass
``disable_existing_loggers=False`` to ``fileConfig``. This test guards
the regression by pre-creating representative ``app.*`` loggers (so
they live in the manager's dict and are eligible for disabling), then
running the same ``alembic upgrade head`` path the integration harness
uses, and finally asserting each logger is still ``disabled is False``.

We can't rely on ``migrate_once`` alone: the session fixture runs once
at collection, far earlier than the ``app.*`` modules under test would
import their loggers, so the ``disable_existing_loggers=True`` regression
would silently miss any logger created later in the run. Re-running the
upgrade in-process inside the test reliably reproduces the original
failure mode.

See cd-ydhf for the full story.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.orm import Session

from app.config import get_settings

pytestmark = pytest.mark.integration


# Loggers exercised by the original failing tests:
#   - ``app.auth.session_cookie`` (test_secure_false_falls_back_to_dev_cookie)
#   - ``app.api.factory``         (test_missing_dist_logs_warning)
#   - ``app.api.static``          (other dist/static-fallback assertions)
# Picking three from different namespaces guards against a future
# alembic.ini change that names one of them but forgets the others.
_TARGET_LOGGER_NAMES = (
    "app.auth.session_cookie",
    "app.api.factory",
    "app.api.static",
)


def _alembic_ini() -> Path:
    """Return the repo-root ``alembic.ini`` path."""
    return Path(__file__).resolve().parents[2] / "alembic.ini"


@contextmanager
def _override_database_url(url: str) -> Iterator[None]:
    """Point ``get_settings().database_url`` at ``url`` for one operation.

    Mirrors the helper of the same name in
    ``tests/integration/conftest.py``: ``migrations/env.py`` reads the
    DB URL from ``app.config.get_settings()`` rather than from the
    Alembic config file, so we set ``CREWDAY_DATABASE_URL`` in the
    process environment for the duration of the upgrade and clear
    ``get_settings``'s ``lru_cache`` either side. Without this override
    the test would run alembic against whichever DB the dev ``.env``
    points at (or crash when the var is unset on CI).
    """
    original = os.environ.get("CREWDAY_DATABASE_URL")
    os.environ["CREWDAY_DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("CREWDAY_DATABASE_URL", None)
        else:
            os.environ["CREWDAY_DATABASE_URL"] = original
        get_settings.cache_clear()


class TestAlembicFileConfigKeepsAppLoggersEnabled:
    """``alembic upgrade head`` -> ``fileConfig`` must leave ``app.*`` loggers alive."""

    def test_app_loggers_stay_enabled_through_alembic_upgrade(
        self, db_url: str, db_session: Session
    ) -> None:
        """Re-running ``alembic upgrade head`` must not disable ``app.*`` loggers.

        Why a fresh upgrade rather than relying on ``migrate_once``:
        ``logging.config.fileConfig`` only disables loggers that already
        exist in :data:`logging.Logger.manager.loggerDict` at call time.
        ``migrate_once`` runs once at session start — long before the
        unit-test loggers under threat
        (``app.auth.session_cookie``, ``app.api.factory``, ``app.api.static``)
        have been instantiated by their owning modules. Pre-creating them
        here and then re-running the same upgrade path (which threads
        through ``migrations/env.py:fileConfig``) reproduces the exact
        ordering that broke
        ``tests/unit/auth/test_session.py::TestBuildSessionCookie::test_secure_false_falls_back_to_dev_cookie``
        and
        ``tests/unit/test_main.py::TestSpaCatchAll::test_missing_dist_logs_warning``.

        The ``db_session`` dependency forces the session-scoped
        ``migrate_once`` fixture, so the schema is at head before we
        re-enter ``command.upgrade``; the second invocation against
        the same ``db_url`` is effectively a no-op for the DB
        (``alembic_version`` already matches) but still runs
        ``env.py`` top-to-bottom — which is what the regression cares
        about. The ``_override_database_url`` block makes sure
        ``env.py``'s ``get_settings().database_url`` lookup resolves
        to the test DB rather than whatever the dev ``.env`` /
        ``CREWDAY_DATABASE_URL`` happens to point at.
        """
        del db_session  # only here to force migrate_once / engine setup
        # Save + restore each logger's pre-test state so we can't leak
        # a forced ``disabled=False`` into sibling tests if the fix
        # regresses and the assertion below trips.
        saved: dict[str, tuple[bool, bool]] = {}
        for name in _TARGET_LOGGER_NAMES:
            log = logging.getLogger(name)
            saved[name] = (log.disabled, log.propagate)

        try:
            with _override_database_url(db_url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", db_url)
                command.upgrade(cfg, "head")

            for name in _TARGET_LOGGER_NAMES:
                log = logging.getLogger(name)
                assert log.disabled is False, (
                    f"Logger {name!r} is disabled after alembic upgrade head; "
                    f"migrations/env.py must pass "
                    f"disable_existing_loggers=False to fileConfig."
                )
        finally:
            for name, (disabled, propagate) in saved.items():
                log = logging.getLogger(name)
                log.disabled = disabled
                log.propagate = propagate
