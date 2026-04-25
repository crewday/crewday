"""Migration smoke: cd-l2r9 availability + holiday tables round-trip cleanly.

Runs ``alembic upgrade head`` against a scratch SQLite file, confirms
the four cd-l2r9 tables (``user_leave``, ``user_weekly_availability``,
``user_availability_override``, ``public_holiday``) land with their
CHECKs / UNIQUEs / FKs / hot-path indexes intact, then ``downgrade -1``
+ re-``upgrade head`` to prove the revision is reversible and
idempotent.

A full-suite migration-parity test already lives in
:mod:`tests.integration.test_schema_parity` (SQLite vs Postgres
structural fingerprint). This module narrows the scope to cd-l2r9 so
a future breaking change on the revision surface fails with a message
that points straight at the right migration instead of requiring a
diff on the parity snapshot.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, inspect

from app.adapters.db.session import make_engine
from app.config import get_settings

pytestmark = pytest.mark.integration


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


_REVISION_ID: str = "e7a9b1c3d5f6"
_PREVIOUS_REVISION_ID: str = "d6f8a0c2b5e7"

_NEW_TABLES: tuple[str, ...] = (
    "user_leave",
    "user_weekly_availability",
    "user_availability_override",
    "public_holiday",
)


def _fingerprint_table(engine: Engine, table_name: str) -> dict[str, object]:
    """Structural fingerprint of a table — column + constraint shape.

    Captures column names + nullability, FK targets + ondelete, CHECK
    names, UNIQUE column tuples, and index names. Compared across the
    ``upgrade → downgrade → upgrade`` cycle to prove schema idempotence.
    """
    insp = inspect(engine)
    cols = {c["name"]: bool(c["nullable"]) for c in insp.get_columns(table_name)}
    fks = sorted(
        (
            tuple(fk.get("constrained_columns", []) or []),
            fk.get("referred_table"),
            (fk.get("options", {}) or {}).get("ondelete"),
        )
        for fk in insp.get_foreign_keys(table_name)
    )
    checks = sorted(
        (c.get("name") or "") for c in insp.get_check_constraints(table_name)
    )
    uniques = sorted(
        (uq.get("name") or "", tuple(uq.get("column_names", []) or []))
        for uq in insp.get_unique_constraints(table_name)
    )
    indexes = sorted(
        (ix.get("name") or "", tuple(ix.get("column_names", []) or []))
        for ix in insp.get_indexes(table_name)
    )
    return {
        "cols": cols,
        "fks": fks,
        "checks": checks,
        "uniques": uniques,
        "indexes": indexes,
    }


class TestCdL2r9Migration:
    """cd-l2r9 lands the four availability + holiday tables, reversibly."""

    def test_upgrade_creates_all_four_tables(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """``alembic upgrade head`` lands the four cd-l2r9 tables."""
        db_path = tmp_path_factory.mktemp("cd-l2r9-mig") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")

            insp = inspect(engine)
            tables = set(insp.get_table_names())
            for name in _NEW_TABLES:
                assert name in tables, f"{name!r} did not land at head"

            # ``user_leave`` shape: enum CHECK + range CHECK + soft
            # delete + hot-path index.
            ul_cols = {c["name"]: c for c in insp.get_columns("user_leave")}
            assert ul_cols["deleted_at"]["nullable"] is True
            assert ul_cols["approved_at"]["nullable"] is True
            assert ul_cols["category"]["nullable"] is False
            assert ul_cols["starts_on"]["nullable"] is False
            ul_checks = {
                c["name"]
                for c in insp.get_check_constraints("user_leave")
                if c.get("name")
            }
            assert "ck_user_leave_category" in ul_checks
            assert "ck_user_leave_range" in ul_checks
            ul_indexes = {ix["name"] for ix in insp.get_indexes("user_leave")}
            assert "ix_user_leave_workspace_user" in ul_indexes

            # ``user_weekly_availability`` shape: weekday CHECK +
            # hours-pairing CHECK + UNIQUE triple.
            uwa_cols = {
                c["name"]: c for c in insp.get_columns("user_weekly_availability")
            }
            assert uwa_cols["weekday"]["nullable"] is False
            assert uwa_cols["starts_local"]["nullable"] is True
            assert uwa_cols["ends_local"]["nullable"] is True
            # No ``deleted_at`` — the §06 spec records there is one
            # live row per (user, weekday).
            assert "deleted_at" not in uwa_cols
            uwa_checks = {
                c["name"]
                for c in insp.get_check_constraints("user_weekly_availability")
                if c.get("name")
            }
            assert "ck_user_weekly_availability_weekday_range" in uwa_checks
            assert "ck_user_weekly_availability_hours_pairing" in uwa_checks
            uwa_uniques = {
                uq["name"]
                for uq in insp.get_unique_constraints("user_weekly_availability")
            }
            assert "uq_user_weekly_availability_user_weekday" in uwa_uniques

            # ``user_availability_override`` shape: hours-pairing
            # CHECK + UNIQUE triple + soft delete.
            uao_cols = {
                c["name"]: c for c in insp.get_columns("user_availability_override")
            }
            assert uao_cols["available"]["nullable"] is False
            assert uao_cols["approval_required"]["nullable"] is False
            assert uao_cols["deleted_at"]["nullable"] is True
            uao_checks = {
                c["name"]
                for c in insp.get_check_constraints("user_availability_override")
                if c.get("name")
            }
            assert "ck_user_availability_override_hours_pairing" in uao_checks
            uao_uniques = {
                uq["name"]
                for uq in insp.get_unique_constraints("user_availability_override")
            }
            assert "uq_user_availability_override_user_date" in uao_uniques

            # ``public_holiday`` shape: three CHECKs + UNIQUE +
            # two hot-path indexes.
            ph_cols = {c["name"]: c for c in insp.get_columns("public_holiday")}
            assert ph_cols["scheduling_effect"]["nullable"] is False
            assert ph_cols["country"]["nullable"] is True
            assert ph_cols["recurrence"]["nullable"] is True
            assert ph_cols["payroll_multiplier"]["nullable"] is True
            ph_checks = {
                c["name"]
                for c in insp.get_check_constraints("public_holiday")
                if c.get("name")
            }
            assert "ck_public_holiday_scheduling_effect" in ph_checks
            assert "ck_public_holiday_recurrence" in ph_checks
            assert "ck_public_holiday_reduced_hours_pairing" in ph_checks
            ph_uniques = {
                uq["name"] for uq in insp.get_unique_constraints("public_holiday")
            }
            assert "uq_public_holiday_workspace_date_country" in ph_uniques
            ph_indexes = {ix["name"] for ix in insp.get_indexes("public_holiday")}
            assert "ix_public_holiday_workspace_date" in ph_indexes
            assert "ix_public_holiday_workspace_deleted" in ph_indexes

            # Every table FKs ``workspace.id`` ON DELETE CASCADE.
            for table in _NEW_TABLES:
                fks = insp.get_foreign_keys(table)
                ws_fk = next(fk for fk in fks if fk["referred_table"] == "workspace")
                assert ws_fk["constrained_columns"] == ["workspace_id"]
                assert (ws_fk.get("options") or {}).get("ondelete") == "CASCADE"
        finally:
            engine.dispose()

    def test_downgrade_drops_all_four_tables(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """``downgrade -1`` from head drops the four cd-l2r9 tables."""
        db_path = tmp_path_factory.mktemp("cd-l2r9-mig-down") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")
                command.downgrade(cfg, _PREVIOUS_REVISION_ID)

            insp = inspect(engine)
            tables = set(insp.get_table_names())
            for name in _NEW_TABLES:
                assert name not in tables, (
                    f"{name!r} survived the downgrade — drop_table did not fire"
                )
        finally:
            engine.dispose()

    def test_upgrade_downgrade_upgrade_is_idempotent(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """The ``upgrade → downgrade → upgrade`` cycle settles on the same shape."""
        db_path = tmp_path_factory.mktemp("cd-l2r9-mig-cycle") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")
            fingerprints_first = {
                name: _fingerprint_table(engine, name) for name in _NEW_TABLES
            }

            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.downgrade(cfg, _PREVIOUS_REVISION_ID)
                command.upgrade(cfg, _REVISION_ID)
            fingerprints_second = {
                name: _fingerprint_table(engine, name) for name in _NEW_TABLES
            }

            for name in _NEW_TABLES:
                assert fingerprints_first[name] == fingerprints_second[name], (
                    f"{name!r} schema shape drifted across upgrade → "
                    "downgrade → upgrade"
                )
        finally:
            engine.dispose()
