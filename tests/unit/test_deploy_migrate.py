from __future__ import annotations

import fcntl
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.deploy import migrate


def _settings(database_url: str, data_dir: Path) -> Settings:
    return Settings(database_url=database_url, data_dir=data_dir)


def test_sqlite_migration_uses_data_dir_file_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_upgrade_head() -> None:
        calls.append("upgrade")

    monkeypatch.setattr(migrate, "_upgrade_head", fake_upgrade_head)

    migrate.run_locked_migrations(
        _settings("sqlite+aiosqlite:///crewday.db", tmp_path),
    )

    assert calls == ["upgrade"]
    assert (tmp_path / "alembic-migrations.lock").is_file()


def test_sqlite_migration_failure_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_upgrade_head() -> None:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(migrate, "_upgrade_head", fail_upgrade_head)

    with pytest.raises(RuntimeError, match="migration failed"):
        migrate.run_locked_migrations(
            _settings("sqlite:///crewday.db", tmp_path),
        )


def test_postgres_migration_uses_advisory_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            events.append("connect")
            return self

        def __exit__(self, *args: object) -> None:
            events.append("disconnect")

        def execute(self, statement: object, params: object) -> None:
            sql = str(statement)
            if "pg_advisory_lock" in sql:
                events.append("lock")
            elif "pg_advisory_unlock" in sql:
                events.append("unlock")

    class FakeEngine:
        def connect(self) -> FakeConnection:
            return FakeConnection()

        def dispose(self) -> None:
            events.append("dispose")

    def fake_create_engine(database_url: str, *, future: bool) -> FakeEngine:
        assert database_url == "postgresql+psycopg://crewday@example.test/db"
        assert future is True
        return FakeEngine()

    def fake_upgrade_head() -> None:
        events.append("upgrade")

    monkeypatch.setattr(migrate, "create_engine", fake_create_engine)
    monkeypatch.setattr(migrate, "_upgrade_head", fake_upgrade_head)

    migrate.run_locked_migrations(
        _settings("postgresql+asyncpg://crewday@example.test/db", tmp_path),
    )

    assert events == [
        "connect",
        "lock",
        "upgrade",
        "unlock",
        "disconnect",
        "dispose",
    ]


def test_postgres_migration_failure_still_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *args: object) -> None:
            events.append("disconnect")

        def execute(self, statement: object, params: object) -> None:
            sql = str(statement)
            if "pg_advisory_lock" in sql:
                events.append("lock")
            elif "pg_advisory_unlock" in sql:
                events.append("unlock")

    class FakeEngine:
        def connect(self) -> FakeConnection:
            return FakeConnection()

        def dispose(self) -> None:
            events.append("dispose")

    def fail_upgrade_head() -> None:
        events.append("upgrade")
        raise RuntimeError("migration failed")

    monkeypatch.setattr(
        migrate,
        "create_engine",
        lambda database_url, *, future: FakeEngine(),
    )
    monkeypatch.setattr(migrate, "_upgrade_head", fail_upgrade_head)

    with pytest.raises(RuntimeError, match="migration failed"):
        migrate.run_locked_migrations(
            _settings("postgresql://crewday@example.test/db", tmp_path),
        )

    assert events == ["lock", "upgrade", "unlock", "disconnect", "dispose"]


def test_postgres_lock_serializes_concurrent_upgrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advisory_lock = threading.Lock()
    first_upgrade_started = threading.Event()
    release_first_upgrade = threading.Event()
    events: list[str] = []
    events_lock = threading.Lock()

    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, statement: object, params: object) -> None:
            sql = str(statement)
            if "pg_advisory_unlock" in sql:
                advisory_lock.release()
                return
            if "pg_advisory_lock" in sql:
                advisory_lock.acquire()

    class FakeEngine:
        def connect(self) -> FakeConnection:
            return FakeConnection()

        def dispose(self) -> None:
            return None

    def fake_upgrade_head() -> None:
        thread_name = threading.current_thread().name
        with events_lock:
            events.append(f"{thread_name}:start")
        if thread_name == "first":
            first_upgrade_started.set()
            assert release_first_upgrade.wait(timeout=1.0)
        with events_lock:
            events.append(f"{thread_name}:end")

    monkeypatch.setattr(
        migrate,
        "create_engine",
        lambda database_url, *, future: FakeEngine(),
    )
    monkeypatch.setattr(migrate, "_upgrade_head", fake_upgrade_head)

    def run_migrations() -> None:
        migrate.run_locked_migrations(
            _settings("postgresql://crewday@example.test/db", tmp_path),
        )

    first = threading.Thread(target=run_migrations, name="first")
    second = threading.Thread(target=run_migrations, name="second")

    first.start()
    assert first_upgrade_started.wait(timeout=1.0)
    second.start()
    time.sleep(0.05)

    with events_lock:
        assert events == ["first:start"]

    release_first_upgrade.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert events == ["first:start", "first:end", "second:start", "second:end"]


def test_postgres_unlock_error_does_not_hide_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        def __enter__(self) -> FakeConnection:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, statement: object, params: object) -> None:
            if "pg_advisory_unlock" in str(statement):
                raise SQLAlchemyError("connection closed")

    class FakeEngine:
        def connect(self) -> FakeConnection:
            return FakeConnection()

        def dispose(self) -> None:
            return None

    monkeypatch.setattr(
        migrate,
        "create_engine",
        lambda database_url, *, future: FakeEngine(),
    )
    monkeypatch.setattr(migrate, "_upgrade_head", lambda: None)

    migrate.run_locked_migrations(
        _settings("postgresql://crewday@example.test/db", tmp_path),
    )


def test_sqlite_failure_releases_file_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_upgrade_head() -> None:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(migrate, "_upgrade_head", fail_upgrade_head)

    with pytest.raises(RuntimeError, match="migration failed"):
        migrate.run_locked_migrations(
            _settings("sqlite:///crewday.db", tmp_path),
        )

    with (tmp_path / "alembic-migrations.lock").open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def test_unsupported_database_dialect_fails_before_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_upgrade_head() -> None:
        raise AssertionError("migration should not run")

    monkeypatch.setattr(migrate, "_upgrade_head", fail_upgrade_head)

    with pytest.raises(
        RuntimeError,
        match="unsupported migration database dialect: mysql",
    ):
        migrate.run_locked_migrations(
            _settings("mysql+pymysql://crewday@example.test/db", tmp_path),
        )
