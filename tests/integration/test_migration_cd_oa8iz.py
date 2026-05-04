"""Migration smoke: cd-oa8iz token rotation overlap columns."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text

from app.adapters.db.session import make_engine
from tests.integration.test_migration_cd_i1qe import _override_database_url

pytestmark = pytest.mark.integration


_REVISION_ID: str = "a4c6e8f0b2d4"
_PREVIOUS_REVISION_ID: str = "f7b9d1e3a5c8"


def _alembic_ini() -> Path:
    return Path(__file__).resolve().parents[2] / "alembic.ini"


class TestApiTokenPreviousHashMigration:
    def test_upgrade_adds_nullable_columns_and_backfills_null(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path_factory.mktemp("cd-oa8iz-mig") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, _PREVIOUS_REVISION_ID)

            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO user "
                        "(id, email, email_lower, display_name, created_at) "
                        "VALUES ('01HWA00000000000000000USER', 'u@e.co', "
                        "'u@e.co', 'U', '2026-05-04T12:00:00+00:00')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO workspace "
                        "(id, slug, name, plan, quota_json, created_at) "
                        "VALUES ('01HWA00000000000000000WKSP', 'w', 'W', "
                        "'free', '{}', '2026-05-04T12:00:00+00:00')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO api_token "
                        "(id, user_id, workspace_id, kind, delegate_for_user_id, "
                        "subject_user_id, label, scope_json, prefix, hash, created_at) "
                        "VALUES ('01HWA00000000000000000TOKN', "
                        "'01HWA00000000000000000USER', "
                        "'01HWA00000000000000000WKSP', 'scoped', NULL, NULL, "
                        "'scoped-tok', '{}', 'pre_sc12', 'h_scoped', "
                        "'2026-05-04T12:00:00+00:00')"
                    )
                )

            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, _REVISION_ID)

            columns = {c["name"]: c for c in inspect(engine).get_columns("api_token")}
            assert columns["previous_hash"]["nullable"] is True
            assert columns["previous_hash_expires_at"]["nullable"] is True

            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT previous_hash, previous_hash_expires_at "
                        "FROM api_token WHERE id = '01HWA00000000000000000TOKN'"
                    )
                ).one()
            assert row[0] is None
            assert row[1] is None
        finally:
            engine.dispose()

    def test_downgrade_removes_previous_hash_columns(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path_factory.mktemp("cd-oa8iz-mig-down") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, _REVISION_ID)
                command.downgrade(cfg, _PREVIOUS_REVISION_ID)

            columns = {c["name"]: c for c in inspect(engine).get_columns("api_token")}
            assert "previous_hash" not in columns
            assert "previous_hash_expires_at" not in columns
        finally:
            engine.dispose()
