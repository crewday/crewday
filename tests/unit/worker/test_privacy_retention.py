"""Unit tests for :mod:`app.worker.tasks.privacy`.

The scheduler suite only asserts that ``run_retention_rotation`` is
*registered*; nothing executes the wrapper itself. This module drives
the real wrapper against an in-memory SQLite engine with a frozen
clock, pinning the §15 "Retention defaults" contract:

* ``rotate_operational_logs`` runs and archives rows past a table's
  retention window to ``$DATA_DIR/archive/<table>.jsonl.gz``;
* the aged rows are deleted from the live table;
* the wrapper surfaces the underlying ``RetentionResult`` tuple shape.

See ``docs/specs/15-security-privacy.md`` §"Retention defaults".
"""

from __future__ import annotations

import gzip
import importlib
import json
import pkgutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.privacy import RETENTION_DEFAULT_DAYS, RetentionResult
from app.tenancy import tenant_agnostic
from app.util.clock import FrozenClock
from app.worker.tasks import privacy as worker_privacy

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture(autouse=True)
def _patch_make_uow(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """Re-route ``make_uow()`` to the in-memory engine for the wrapper.

    ``run_retention_rotation`` opens its own UoW and relies on it to
    commit; the fake mirrors :class:`UnitOfWorkImpl` (commit on clean
    exit, rollback on error) so the delete lands in the same database
    the assertions read back.
    """

    @contextmanager
    def _fake_make_uow() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(worker_privacy, "make_uow", _fake_make_uow)


def _seed_workspace(session: Session, workspace_id: str = "w1") -> None:
    session.add(
        Workspace(
            id=workspace_id,
            slug=workspace_id,
            name=workspace_id,
            plan="free",
            quota_json={},
            settings_json={},
            default_timezone="UTC",
            default_locale="en",
            default_currency="USD",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


def _seed_aged_audit_log(
    session: Session, *, workspace_id: str, created_at: datetime
) -> str:
    audit_id = "aud-old"
    session.add(
        AuditLog(
            id=audit_id,
            workspace_id=workspace_id,
            actor_id="u1",
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            entity_kind="user",
            entity_id="u1",
            action="test",
            diff={},
            correlation_id="corr",
            scope_kind="workspace",
            created_at=created_at,
        )
    )
    return audit_id


def test_run_retention_rotation_archives_and_deletes_aged_rows(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    # §15 "Retention defaults": audit_log default is 2 years. Pin the
    # spec value here, then age the row past that real window (derived,
    # not a magic constant) so the test can't silently bless a changed
    # default and still archives + deletes under the frozen clock.
    assert RETENTION_DEFAULT_DAYS["audit_log"] == 730  # §15: 2 years
    aged_at = _NOW - timedelta(days=RETENTION_DEFAULT_DAYS["audit_log"] + 30)
    with session_factory() as seed, tenant_agnostic():
        _seed_workspace(seed)
        audit_id = _seed_aged_audit_log(seed, workspace_id="w1", created_at=aged_at)
        seed.commit()

    results = worker_privacy.run_retention_rotation(
        data_dir=tmp_path,
        clock=FrozenClock(_NOW),
    )

    # Wrapper returns the RetentionResult tuple, filtered to rows that
    # actually moved. Exactly one table had aged rows.
    assert isinstance(results, tuple)
    assert len(results) == 1
    (result,) = results
    assert isinstance(result, RetentionResult)
    assert result.table == "audit_log"
    assert result.workspace_id == "w1"
    assert result.archived_rows == 1

    # Rows landed in $DATA_DIR/archive/<table>.jsonl.gz (§15).
    archive = tmp_path / "archive" / "audit_log.jsonl.gz"
    assert archive.exists()
    with gzip.open(archive, "rt", encoding="utf-8") as fh:
        archived = json.loads(fh.readline())
    assert archived["id"] == audit_id

    # And the aged row is gone from the live table.
    with session_factory() as check, tenant_agnostic():
        remaining = check.scalars(select(AuditLog.id)).all()
    assert audit_id not in remaining


def test_run_retention_rotation_no_aged_rows_returns_empty(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    # A fresh audit row inside the retention window is left untouched;
    # the wrapper returns an empty tuple and writes no archive.
    with session_factory() as seed, tenant_agnostic():
        _seed_workspace(seed)
        _seed_aged_audit_log(seed, workspace_id="w1", created_at=_NOW)
        seed.commit()

    results = worker_privacy.run_retention_rotation(
        data_dir=tmp_path,
        clock=FrozenClock(_NOW),
    )

    assert results == ()
    assert not (tmp_path / "archive").exists()
    with session_factory() as check, tenant_agnostic():
        assert check.scalars(select(AuditLog.id)).all() == ["aud-old"]
