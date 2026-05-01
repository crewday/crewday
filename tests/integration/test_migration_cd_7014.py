"""Migration smoke for cd-7014 task completed state rename."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import exc, text

from app.adapters.db.session import make_engine
from app.config import get_settings

pytestmark = pytest.mark.integration

_REVISION_ID = "e6f8a0c2d4b6"
_PREVIOUS_REVISION_ID = "d5f7a9c1e3b5"


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


class TestTaskCompletedStateMigration:
    """cd-7014 backfills rows and narrows the occurrence state CHECK."""

    def test_upgrade_backfills_done_and_rejects_new_done(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path_factory.mktemp("cd-7014-mig") / "mig.db"
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
                        "INSERT INTO workspace "
                        "(id, slug, name, plan, quota_json, created_at) "
                        "VALUES ('01HWA00000000000000000WKSP', 'state-rename', "
                        "'State Rename', 'free', '{}', '2026-05-01T00:00:00Z')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO occurrence "
                        "(id, workspace_id, starts_at, ends_at, state, "
                        "priority, photo_evidence, linked_instruction_ids, "
                        "inventory_consumption_json, is_personal, created_at) "
                        "VALUES ('01HWA00000000000000000OCC1', "
                        "'01HWA00000000000000000WKSP', "
                        "'2026-05-01T10:00:00Z', '2026-05-01T11:00:00Z', "
                        ":done_state, 'normal', 'disabled', '[]', '{}', 0, "
                        "'2026-05-01T00:00:00Z')"
                    ),
                    {"done_state": "done"},
                )

            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, _REVISION_ID)

            with engine.begin() as conn:
                state = conn.execute(
                    text(
                        "SELECT state FROM occurrence "
                        "WHERE id = '01HWA00000000000000000OCC1'"
                    )
                ).scalar_one()
                assert state == "completed"

                conn.execute(
                    text(
                        "INSERT INTO occurrence "
                        "(id, workspace_id, starts_at, ends_at, state, "
                        "priority, photo_evidence, linked_instruction_ids, "
                        "inventory_consumption_json, is_personal, created_at) "
                        "VALUES ('01HWA00000000000000000OCC2', "
                        "'01HWA00000000000000000WKSP', "
                        "'2026-05-01T12:00:00Z', '2026-05-01T13:00:00Z', "
                        "'completed', 'normal', 'disabled', '[]', '{}', 0, "
                        "'2026-05-01T00:00:00Z')"
                    )
                )

                with pytest.raises(exc.IntegrityError):
                    conn.execute(
                        text(
                            "INSERT INTO occurrence "
                            "(id, workspace_id, starts_at, ends_at, state, "
                            "priority, photo_evidence, linked_instruction_ids, "
                            "inventory_consumption_json, is_personal, created_at) "
                            "VALUES ('01HWA00000000000000000OCC3', "
                            "'01HWA00000000000000000WKSP', "
                            "'2026-05-01T14:00:00Z', '2026-05-01T15:00:00Z', "
                            ":done_state, 'normal', 'disabled', '[]', '{}', 0, "
                            "'2026-05-01T00:00:00Z')"
                        ),
                        {"done_state": "done"},
                    )
        finally:
            engine.dispose()
