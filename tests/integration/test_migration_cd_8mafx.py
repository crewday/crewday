"""Migration smoke: cd-8mafx renames the LLM prompt revision CHECK."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, inspect

from app.adapters.db.session import make_engine, normalise_sync_url
from app.config import get_settings

pytestmark = pytest.mark.integration

_REVISION_ID = "d7f9a1c3e5b8"
_PREVIOUS_REVISION_ID = "c6e8f0a2d4b6"
_TABLE = "llm_prompt_template_revision"
_PREDICATE = "version >= 1"
_CANONICAL_NAME = "ck_llm_prompt_template_revision_version_min"
_SQLITE_LEGACY_NAME = (
    "ck_llm_prompt_template_revision_llm_prompt_template_revision_version"
)
_POSTGRES_LEGACY_NAME = "ck_llm_prompt_template_revision_llm_prompt_template_rev_c3ca"


def _alembic_ini() -> Path:
    return Path(__file__).resolve().parents[2] / "alembic.ini"


@contextmanager
def _override_database_url(url: str) -> Iterator[None]:
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


def _alembic_config(url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(_alembic_ini()))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _check_names(engine: Engine) -> set[str]:
    return {c["name"] or "" for c in inspect(engine).get_check_constraints(_TABLE)}


def _replace_check(engine: Engine, old_name: str, new_name: str) -> None:
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        ops = Operations(ctx)
        with ops.batch_alter_table(_TABLE, schema=None) as batch_op:
            batch_op.drop_constraint(old_name, type_="check")
            batch_op.create_check_constraint(new_name, _PREDICATE)


class TestCd8mafxMigration:
    """cd-8mafx repairs legacy CHECK names without changing the predicate."""

    def test_upgrade_renames_legacy_sqlite_constraint(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path_factory.mktemp("cd-8mafx-mig") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = _alembic_config(url)
                command.upgrade(cfg, _PREVIOUS_REVISION_ID)

            _replace_check(engine, _CANONICAL_NAME, _SQLITE_LEGACY_NAME)
            assert _SQLITE_LEGACY_NAME in _check_names(engine)

            with _override_database_url(url):
                command.upgrade(_alembic_config(url), _REVISION_ID)

            names = _check_names(engine)
            assert _CANONICAL_NAME in names
            assert _SQLITE_LEGACY_NAME not in names
        finally:
            engine.dispose()

    def test_upgrade_renames_legacy_postgres_constraint(self) -> None:
        try:
            from testcontainers.postgres import PostgresContainer
        except ImportError as exc:  # pragma: no cover - dep is in dev group
            pytest.skip(f"testcontainers not installed: {exc}")

        pg_cm = PostgresContainer("postgres:15-alpine", driver="psycopg")
        try:
            pg_cm.__enter__()
        except Exception as exc:
            pytest.skip(f"Docker/PostgresContainer unavailable: {exc}")

        try:
            url = normalise_sync_url(pg_cm.get_connection_url())
            engine = make_engine(url)
            try:
                with _override_database_url(url):
                    cfg = _alembic_config(url)
                    command.upgrade(cfg, _PREVIOUS_REVISION_ID)

                _replace_check(engine, _CANONICAL_NAME, _POSTGRES_LEGACY_NAME)
                assert _POSTGRES_LEGACY_NAME in _check_names(engine)

                with _override_database_url(url):
                    command.upgrade(_alembic_config(url), _REVISION_ID)

                names = _check_names(engine)
                assert _CANONICAL_NAME in names
                assert _POSTGRES_LEGACY_NAME not in names
            finally:
                engine.dispose()
        finally:
            pg_cm.__exit__(None, None, None)

    def test_downgrade_restores_legacy_sqlite_constraint(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path_factory.mktemp("cd-8mafx-mig-down") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = _alembic_config(url)
                command.upgrade(cfg, _REVISION_ID)
                command.downgrade(cfg, _PREVIOUS_REVISION_ID)

            names = _check_names(engine)
            assert _SQLITE_LEGACY_NAME in names
            assert _CANONICAL_NAME not in names
        finally:
            engine.dispose()
