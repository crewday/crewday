"""Migration smoke: cd-jtrc renames the doubled-prefix WebAuthnChallenge CHECK.

The original cd-8m4 ``webauthn_challenge`` migration emitted the
subject CHECK constraint with the convention-doubled name
``ck_webauthn_challenge_ck_webauthn_challenge_subject``. cd-jtrc
realigns the on-disk name to the canonical
``ck_webauthn_challenge_subject`` while keeping the predicate intact.

This module proves:

* upgrading from the prior head lands the canonical name on disk;
* the predicate still rejects rows that violate the "exactly one
  subject" invariant after the rename;
* downgrading restores the doubled name (so a redeploy of an older
  app version still finds the constraint it expects);
* the upgrade -> downgrade -> upgrade cycle settles on the same
  shape (no drift across the round-trip).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.adapters.db.session import make_engine
from app.config import get_settings

pytestmark = pytest.mark.integration

_REVISION_ID: str = "a0b2c4d6e8f1"
_PREVIOUS_REVISION_ID: str = "f9c1d3e5b7a2"
_CANONICAL_NAME: str = "ck_webauthn_challenge_subject"
_DOUBLED_NAME: str = "ck_webauthn_challenge_ck_webauthn_challenge_subject"


def _alembic_ini() -> Path:
    return Path(__file__).resolve().parents[2] / "alembic.ini"


@contextmanager
def _override_database_url(url: str) -> Iterator[None]:
    """Temporarily point :func:`app.config.get_settings` at ``url``."""
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


def _check_names(engine: Engine, table: str) -> set[str]:
    insp = inspect(engine)
    return {c["name"] or "" for c in insp.get_check_constraints(table)}


class TestCdJtrcMigration:
    """cd-jtrc renames the doubled-prefix subject CHECK, reversibly."""

    def test_upgrade_lands_canonical_name(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Head DB carries ``ck_webauthn_challenge_subject`` only."""
        db_path = tmp_path_factory.mktemp("cd-jtrc-mig") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")

            names = _check_names(engine, "webauthn_challenge")
            assert _CANONICAL_NAME in names
            assert _DOUBLED_NAME not in names
        finally:
            engine.dispose()

    def test_predicate_still_enforced_after_rename(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """A row with neither ``user_id`` nor ``signup_session_id`` is rejected."""
        db_path = tmp_path_factory.mktemp("cd-jtrc-mig-predicate") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")

            with engine.begin() as conn, pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        "INSERT INTO webauthn_challenge "
                        "(id, challenge, exclude_credentials, "
                        " created_at, expires_at) "
                        "VALUES ('x', X'00', '[]', "
                        "'2026-05-01 00:00', '2026-05-01 01:00')"
                    )
                )
        finally:
            engine.dispose()

    def test_downgrade_restores_doubled_name(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """``downgrade -1`` puts the doubled-prefix name back on disk."""
        db_path = tmp_path_factory.mktemp("cd-jtrc-mig-down") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")
                command.downgrade(cfg, _PREVIOUS_REVISION_ID)

            names = _check_names(engine, "webauthn_challenge")
            assert _DOUBLED_NAME in names
            assert _CANONICAL_NAME not in names
        finally:
            engine.dispose()

    def test_upgrade_downgrade_upgrade_is_idempotent(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Round-trip yields the same canonical CHECK name."""
        db_path = tmp_path_factory.mktemp("cd-jtrc-mig-cycle") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")
            first = _check_names(engine, "webauthn_challenge")

            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.downgrade(cfg, _PREVIOUS_REVISION_ID)
                command.upgrade(cfg, _REVISION_ID)
            second = _check_names(engine, "webauthn_challenge")

            assert first == second
            assert _CANONICAL_NAME in second
            assert _DOUBLED_NAME not in second
        finally:
            engine.dispose()
