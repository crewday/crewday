"""Run Alembic migrations under the deployment-wide startup lock."""

from __future__ import annotations

import fcntl
import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import SQLAlchemyError

from app.adapters.db.session import normalise_sync_url
from app.config import Settings, get_settings

_log = logging.getLogger(__name__)
_LOCK_NAME = "crew.day:alembic:migrations"
_SQLITE_LOCK_FILE = "alembic-migrations.lock"


def main() -> None:
    """CLI entrypoint for locked startup migrations."""
    run_locked_migrations(get_settings())


def run_locked_migrations(settings: Settings) -> None:
    """Run ``alembic upgrade head`` behind the lock for the configured DB."""
    database_url = normalise_sync_url(settings.database_url)
    dialect = make_url(database_url).get_backend_name()
    if dialect == "postgresql":
        with _postgres_migration_lock(database_url):
            _upgrade_head()
        return
    if dialect == "sqlite":
        with _sqlite_migration_lock(settings.data_dir):
            _upgrade_head()
        return
    raise RuntimeError(f"unsupported migration database dialect: {dialect}")


def _upgrade_head() -> None:
    command.upgrade(AlembicConfig(str(_alembic_ini())), "head")


def _alembic_ini() -> Path:
    return Path(__file__).resolve().parents[2] / "alembic.ini"


@contextmanager
def _postgres_migration_lock(database_url: str) -> Iterator[None]:
    engine = create_engine(database_url, future=True)
    lock_id = _postgres_lock_id()
    try:
        with engine.connect() as connection:
            _log.info("migrations: waiting for Postgres advisory lock")
            connection.execute(
                text("SELECT pg_advisory_lock(:lock_id)"),
                {"lock_id": lock_id},
            )
            try:
                _log.info("migrations: acquired Postgres advisory lock")
                yield
            finally:
                try:
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": lock_id},
                    )
                except SQLAlchemyError as exc:
                    _log.warning(
                        "migrations: failed to release Postgres advisory lock",
                        extra={"error": repr(exc)},
                    )
    finally:
        engine.dispose()


def _postgres_lock_id() -> int:
    digest = hashlib.sha256(_LOCK_NAME.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


@contextmanager
def _sqlite_migration_lock(data_dir: Path) -> Iterator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / _SQLITE_LOCK_FILE
    with lock_path.open("a+b") as lock_file:
        _log.info("migrations: waiting for SQLite file lock at %s", lock_path)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            _log.info("migrations: acquired SQLite file lock at %s", lock_path)
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
