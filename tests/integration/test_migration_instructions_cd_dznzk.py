"""Migration smoke: cd-dznzk adds multi-property instruction scope."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, inspect, text

from app.adapters.db.session import make_engine
from app.config import get_settings

pytestmark = pytest.mark.integration

_REVISION_ID = "cddznzkmultiprop"
_PREVIOUS_REVISION_ID = "cdnxhg9agentdoc"
_PINNED = "2026-05-30 12:00:00+00:00"


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


def _seed_legacy_instruction(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspace "
                "(id, slug, name, plan, quota_json, created_at, settings_json, "
                "default_timezone, default_locale, default_currency, updated_at) "
                "VALUES (:id, :slug, :name, :plan, :quota_json, :created_at, "
                ":settings_json, :timezone, :locale, :currency, :updated_at)"
            ),
            {
                "id": "01HWA00000000000000000WSPB",
                "slug": "instruction-backfill",
                "name": "Instruction backfill",
                "plan": "free",
                "quota_json": "{}",
                "created_at": _PINNED,
                "settings_json": "{}",
                "timezone": "UTC",
                "locale": "en",
                "currency": "USD",
                "updated_at": _PINNED,
            },
        )
        conn.execute(
            text(
                "INSERT INTO instruction "
                "(id, workspace_id, slug, title, scope_kind, scope_id, tags, "
                "created_at) "
                "VALUES (:id, :workspace_id, :slug, :title, :scope_kind, "
                ":scope_id, :tags, :created_at)"
            ),
            {
                "id": "01HWA00000000000000000INSB",
                "workspace_id": "01HWA00000000000000000WSPB",
                "slug": "legacy-property",
                "title": "Legacy property",
                "scope_kind": "property",
                "scope_id": "01HWA00000000000000000PRPB",
                "tags": "[]",
                "created_at": _PINNED,
            },
        )


class TestInstructionsCdDznzkMigration:
    """The migration creates the join table and backfills legacy rows."""

    def test_backfills_existing_property_scope_ids(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path_factory.mktemp("cd-dznzk-instructions-mig") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                command.upgrade(_alembic_config(url), _PREVIOUS_REVISION_ID)

            _seed_legacy_instruction(engine)

            with _override_database_url(url):
                command.upgrade(_alembic_config(url), _REVISION_ID)

            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT workspace_id, instruction_id, property_id, created_at "
                        "FROM instruction_property_scope"
                    )
                ).all()
            assert rows == [
                (
                    "01HWA00000000000000000WSPB",
                    "01HWA00000000000000000INSB",
                    "01HWA00000000000000000PRPB",
                    _PINNED,
                )
            ]
        finally:
            engine.dispose()

    def test_downgrade_drops_property_scope_table(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path_factory.mktemp("cd-dznzk-instructions-down") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                command.upgrade(_alembic_config(url), _REVISION_ID)
                command.downgrade(_alembic_config(url), _PREVIOUS_REVISION_ID)

            assert "instruction_property_scope" not in inspect(engine).get_table_names()
        finally:
            engine.dispose()
