"""Migration smoke: cd-0koqx renames doubled-prefix identity constraints."""

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
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.adapters.db.session import make_engine
from app.config import get_settings

pytestmark = pytest.mark.integration

_REVISION_ID = "e8a0b2c4d6f0"
_PREVIOUS_REVISION_ID = "d7f9a1c3e5b8"

_CANONICAL_CHECKS = {
    "user": {"ck_user_agent_approval_mode"},
    "api_token": {"ck_api_token_kind", "ck_api_token_kind_shape"},
    "invite": {"ck_invite_state"},
}
_DOUBLED_CHECKS = {
    "user": {"ck_user_user_agent_approval_mode"},
    "api_token": {
        "ck_api_token_ck_api_token_kind",
        "ck_api_token_ck_api_token_kind_shape",
    },
    "invite": {"ck_invite_ck_invite_state"},
}
_CANONICAL_UNIQUES = {
    "signup_attempt": {"uq_signup_attempt_email_slug"},
    "invite": {"uq_invite_workspace_email_state"},
}
_DOUBLED_UNIQUES = {
    "signup_attempt": {"uq_signup_attempt_uq_signup_attempt_email_slug"},
    "invite": {"uq_invite_uq_invite_workspace_email_state"},
}


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


def _check_names(engine: Engine, table: str) -> set[str]:
    return {c["name"] or "" for c in inspect(engine).get_check_constraints(table)}


def _unique_names(engine: Engine, table: str) -> set[str]:
    return {c["name"] or "" for c in inspect(engine).get_unique_constraints(table)}


def _assert_identity_constraint_names(
    engine: Engine,
    *,
    check_names: dict[str, set[str]],
    unique_names: dict[str, set[str]],
    absent_checks: dict[str, set[str]],
    absent_uniques: dict[str, set[str]],
) -> None:
    for table, expected in check_names.items():
        actual = _check_names(engine, table)
        assert expected <= actual
        assert actual.isdisjoint(absent_checks[table])

    for table, expected in unique_names.items():
        actual = _unique_names(engine, table)
        assert expected <= actual
        assert actual.isdisjoint(absent_uniques[table])


def _replace_unique(
    engine: Engine,
    *,
    table: str,
    old_name: str,
    new_name: str,
    columns: list[str],
) -> None:
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        ops = Operations(ctx)
        with ops.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(old_name, type_="unique")
            batch_op.create_unique_constraint(new_name, columns)


class TestCd0koqxMigration:
    """cd-0koqx repairs identity constraint names without changing rules."""

    def test_upgrade_lands_canonical_names(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path_factory.mktemp("cd-0koqx-mig") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                command.upgrade(_alembic_config(url), "head")

            _assert_identity_constraint_names(
                engine,
                check_names=_CANONICAL_CHECKS,
                unique_names=_CANONICAL_UNIQUES,
                absent_checks=_DOUBLED_CHECKS,
                absent_uniques=_DOUBLED_UNIQUES,
            )
        finally:
            engine.dispose()

    def test_upgrade_repairs_doubled_unique_names(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path_factory.mktemp("cd-0koqx-mig-unique") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                command.upgrade(_alembic_config(url), _PREVIOUS_REVISION_ID)

            _replace_unique(
                engine,
                table="signup_attempt",
                old_name="uq_signup_attempt_email_slug",
                new_name="uq_signup_attempt_uq_signup_attempt_email_slug",
                columns=["email_lower", "desired_slug"],
            )
            _replace_unique(
                engine,
                table="invite",
                old_name="uq_invite_workspace_email_state",
                new_name="uq_invite_uq_invite_workspace_email_state",
                columns=["workspace_id", "pending_email_lower", "state"],
            )

            with _override_database_url(url):
                command.upgrade(_alembic_config(url), _REVISION_ID)

            _assert_identity_constraint_names(
                engine,
                check_names=_CANONICAL_CHECKS,
                unique_names=_CANONICAL_UNIQUES,
                absent_checks=_DOUBLED_CHECKS,
                absent_uniques=_DOUBLED_UNIQUES,
            )
        finally:
            engine.dispose()

    def test_user_check_predicate_still_enforced(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path_factory.mktemp("cd-0koqx-mig-predicate") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                command.upgrade(_alembic_config(url), "head")

            with engine.begin() as conn, pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        'INSERT INTO "user" '
                        "(id, email, email_lower, display_name, "
                        " agent_approval_mode, created_at) "
                        "VALUES ('u-bad', 'u@example.com', 'u@example.com', "
                        " 'User', 'loose', '2026-05-05 00:00:00')"
                    )
                )
        finally:
            engine.dispose()

    def test_downgrade_restores_doubled_names(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path_factory.mktemp("cd-0koqx-mig-down") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = _alembic_config(url)
                command.upgrade(cfg, "head")
                command.downgrade(cfg, _PREVIOUS_REVISION_ID)

            _assert_identity_constraint_names(
                engine,
                check_names=_DOUBLED_CHECKS,
                unique_names=_DOUBLED_UNIQUES,
                absent_checks=_CANONICAL_CHECKS,
                absent_uniques=_CANONICAL_UNIQUES,
            )
        finally:
            engine.dispose()

    def test_upgrade_downgrade_upgrade_is_idempotent(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path_factory.mktemp("cd-0koqx-mig-cycle") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                command.upgrade(_alembic_config(url), "head")
            first_checks = {
                table: _check_names(engine, table) for table in _CANONICAL_CHECKS
            }
            first_uniques = {
                table: _unique_names(engine, table) for table in _CANONICAL_UNIQUES
            }

            with _override_database_url(url):
                cfg = _alembic_config(url)
                command.downgrade(cfg, _PREVIOUS_REVISION_ID)
                command.upgrade(cfg, _REVISION_ID)

            assert first_checks == {
                table: _check_names(engine, table) for table in _CANONICAL_CHECKS
            }
            assert first_uniques == {
                table: _unique_names(engine, table) for table in _CANONICAL_UNIQUES
            }
        finally:
            engine.dispose()
