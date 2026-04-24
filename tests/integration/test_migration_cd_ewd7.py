"""Migration smoke: cd-ewd7 ``ical_feed`` extensions round-trip cleanly.

Runs ``alembic upgrade head`` against a scratch SQLite file, confirms
the three added columns plus the widened ``provider`` CHECK land on
``ical_feed``, then ``downgrade -1`` and re-``upgrade head`` to prove
the revision is reversible and idempotent. Seeds a ``gcal`` row on
the head schema and asserts the downgrade narrows it to ``custom``
before the CHECK-swap runs so the rollback doesn't fail on live
data.

A full-suite migration-parity test already lives in
:mod:`tests.integration.test_schema_parity` (SQLite vs Postgres
structural fingerprint). This module narrows the scope to cd-ewd7
so a future breaking change on the revision surface fails with a
message that points straight at the right migration instead of
requiring a diff on the parity snapshot.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, text

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


_REVISION_ID: str = "f2b4c5d6e7a8"
_PREVIOUS_REVISION_ID: str = "e1a3b4c5d6f7"


def _fingerprint_ical_feed(engine: object) -> dict[str, object]:
    """Structural fingerprint of ``ical_feed`` — column + constraint shape.

    Captures column names + nullability, FK targets + ondelete, CHECK
    names, and index names. Compared across the upgrade →
    downgrade → upgrade cycle to prove schema idempotence.
    """
    insp = inspect(engine)  # type: ignore[arg-type]
    cols = {c["name"]: bool(c["nullable"]) for c in insp.get_columns("ical_feed")}
    fks = sorted(
        (
            tuple(fk.get("constrained_columns", []) or []),
            fk.get("referred_table"),
            (fk.get("options", {}) or {}).get("ondelete"),
        )
        for fk in insp.get_foreign_keys("ical_feed")
    )
    checks = sorted(
        (c.get("name") or "") for c in insp.get_check_constraints("ical_feed")
    )
    indexes = sorted(
        (ix.get("name") or "", tuple(ix.get("column_names", []) or []))
        for ix in insp.get_indexes("ical_feed")
    )
    return {"cols": cols, "fks": fks, "checks": checks, "indexes": indexes}


class TestIcalFeedExtensionsMigration:
    """cd-ewd7 migration adds the three columns + widens CHECK, reversibly."""

    def test_upgrade_adds_columns_and_widens_check(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """``alembic upgrade head`` lands the three cd-ewd7 columns."""
        db_path = tmp_path_factory.mktemp("cd-ewd7-mig") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")

            insp = inspect(engine)
            cols = {c["name"]: c for c in insp.get_columns("ical_feed")}
            # Three new columns.
            assert "unit_id" in cols
            assert "poll_cadence" in cols
            assert "last_error" in cols
            # Nullability:
            # * ``unit_id`` nullable (SET NULL FK on unit delete)
            # * ``poll_cadence`` NOT NULL (server default '*/15 * * * *')
            # * ``last_error`` nullable (NULL means healthy)
            assert cols["unit_id"]["nullable"] is True
            assert cols["poll_cadence"]["nullable"] is False
            assert cols["last_error"]["nullable"] is True

            # FK on ``unit_id`` → ``unit.id`` with SET NULL.
            fks = {
                tuple(fk["constrained_columns"]): fk
                for fk in insp.get_foreign_keys("ical_feed")
            }
            assert fks[("unit_id",)]["referred_table"] == "unit"
            assert fks[("unit_id",)]["options"].get("ondelete") == "SET NULL"

            # Widened CHECK admits ``gcal`` + ``generic``. The SQLite
            # inspector returns the original SQL body so we can assert
            # on its text.
            checks = {
                c["name"]: str(c["sqltext"])
                for c in insp.get_check_constraints("ical_feed")
            }
            assert "ck_ical_feed_provider" in checks
            for slug in ("airbnb", "vrbo", "booking", "gcal", "generic", "custom"):
                assert slug in checks["ck_ical_feed_provider"], (
                    f"{slug!r} missing from widened provider CHECK"
                )
        finally:
            engine.dispose()

    def test_downgrade_removes_columns_and_narrows_check(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """``downgrade -1`` from head drops the three columns + narrows CHECK."""
        db_path = tmp_path_factory.mktemp("cd-ewd7-mig-down") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")
                command.downgrade(cfg, _PREVIOUS_REVISION_ID)

            insp = inspect(engine)
            cols = {c["name"]: c for c in insp.get_columns("ical_feed")}
            assert "unit_id" not in cols
            assert "poll_cadence" not in cols
            assert "last_error" not in cols
            # Provider CHECK narrowed back to the v1 alphabet.
            checks = {
                c["name"]: str(c["sqltext"])
                for c in insp.get_check_constraints("ical_feed")
            }
            assert "gcal" not in checks["ck_ical_feed_provider"]
            assert "generic" not in checks["ck_ical_feed_provider"]
            for v1_slug in ("airbnb", "vrbo", "booking", "custom"):
                assert v1_slug in checks["ck_ical_feed_provider"]
        finally:
            engine.dispose()

    def test_upgrade_downgrade_upgrade_is_idempotent(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """The ``upgrade → downgrade → upgrade`` cycle settles on the same shape."""
        db_path = tmp_path_factory.mktemp("cd-ewd7-mig-cycle") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")
            fingerprint_first = _fingerprint_ical_feed(engine)

            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.downgrade(cfg, _PREVIOUS_REVISION_ID)
                command.upgrade(cfg, _REVISION_ID)
            fingerprint_second = _fingerprint_ical_feed(engine)

            assert fingerprint_first == fingerprint_second, (
                "schema shape drifted across upgrade → downgrade → upgrade"
            )
        finally:
            engine.dispose()

    def test_downgrade_collapses_gcal_and_generic_to_custom(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Pre-narrow UPDATE lets the downgrade survive live cd-ewd7 rows.

        A row carrying ``provider = 'gcal'`` or ``'generic'`` on the
        post-cd-ewd7 schema would violate the narrower v1 CHECK on
        rollback. The downgrade runs a ``UPDATE … SET provider =
        'custom'`` before the CHECK swap so the rollback doesn't
        fail on live data. This test seeds two rows (one ``gcal``,
        one ``generic``) and asserts both survive the rollback with
        their providers rewritten to ``custom``.
        """
        db_path = tmp_path_factory.mktemp("cd-ewd7-mig-data") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")

            # Seed FK parents + two feeds. Raw SQL keeps the test
            # free of tenancy / factory plumbing — we only care about
            # the migration's rollback behaviour here.
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO workspace "
                        "(id, slug, name, plan, quota_json, created_at) VALUES "
                        "('01HWA00000000000000000WKSP', 'cd-ewd7', 'Ewd7', 'free', "
                        "'{}', '2026-04-24T12:00:00+00:00')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO property "
                        "(id, name, kind, address, address_json, country, "
                        "locale, default_currency, client_org_id, "
                        "owner_user_id, tags_json, welcome_defaults_json, "
                        "property_notes_md, timezone, lat, lon, created_at, "
                        "updated_at, deleted_at) VALUES "
                        "('01HWA00000000000000000PRP1', 'P', 'residence', "
                        "'addr', '{}', 'FR', NULL, NULL, NULL, NULL, '[]', "
                        "'{}', '', 'Europe/Paris', NULL, NULL, "
                        "'2026-04-24T12:00:00+00:00', "
                        "'2026-04-24T12:00:00+00:00', NULL)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO ical_feed "
                        "(id, workspace_id, property_id, unit_id, url, "
                        "provider, poll_cadence, last_polled_at, last_etag, "
                        "last_error, enabled, created_at) VALUES "
                        "('01HWA00000000000000000FED1', "
                        "'01HWA00000000000000000WKSP', "
                        "'01HWA00000000000000000PRP1', NULL, "
                        "'ciphertext-gcal', 'gcal', '*/15 * * * *', NULL, "
                        "NULL, NULL, 1, '2026-04-24T12:00:00+00:00')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO ical_feed "
                        "(id, workspace_id, property_id, unit_id, url, "
                        "provider, poll_cadence, last_polled_at, last_etag, "
                        "last_error, enabled, created_at) VALUES "
                        "('01HWA00000000000000000FED2', "
                        "'01HWA00000000000000000WKSP', "
                        "'01HWA00000000000000000PRP1', NULL, "
                        "'ciphertext-generic', 'generic', '*/15 * * * *', "
                        "NULL, NULL, NULL, 1, '2026-04-24T12:00:00+00:00')"
                    )
                )

            # Downgrade must NOT fail on the two widened-slug rows.
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.downgrade(cfg, _PREVIOUS_REVISION_ID)

            # Both rows survive with their provider collapsed to
            # ``custom``. (Re-upgrade after inspection so the scratch
            # DB is left at head for any later cleanup.)
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT id, provider FROM ical_feed ORDER BY id")
                ).fetchall()
            assert [tuple(r) for r in rows] == [
                ("01HWA00000000000000000FED1", "custom"),
                ("01HWA00000000000000000FED2", "custom"),
            ]

            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, _REVISION_ID)
        finally:
            engine.dispose()
