"""Unit tests for :mod:`app.worker.tasks.workspace_purge`.

The scheduler suite only asserts that ``purge_due_workspace_deletions``
is *registered*; nothing executes the wrapper. This module drives the
real wrapper across all three branches of its storage-resolution guard
(``workspace_purge.py`` lines 36-43):

* (a) storage present -> delegates to ``purge_due_workspaces`` and
  surfaces the underlying report;
* (b) storage unavailable + ``require_storage=False`` -> warns and
  returns an empty report (``purged=0``) without opening a UoW. This
  pins the documented silent-safe path so it can never regress into a
  worse silent no-op;
* (c) storage unavailable + ``require_storage=True`` -> raises
  ``RuntimeError`` before touching the database.

See ``docs/specs/15-security-privacy.md`` §"Right to erasure".
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.services.workspace.deletion_service import WorkspacePurgeReport
from app.tenancy import tenant_agnostic
from app.util.clock import FrozenClock
from app.worker.tasks import workspace_purge as worker_purge
from tests._fakes.storage import InMemoryStorage
from tests.factories.identity import bootstrap_user, bootstrap_workspace

_PINNED = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)


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


@pytest.fixture
def patch_make_uow(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """Re-route ``make_uow()`` to the in-memory engine (commit on exit)."""

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

    monkeypatch.setattr(worker_purge, "make_uow", _fake_make_uow)


def _forbid_make_uow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail if the wrapper opens a UoW — the no-storage guard must not."""

    @contextmanager
    def _boom() -> Iterator[Session]:
        raise AssertionError("make_uow must not be opened on the no-storage path")
        yield  # pragma: no cover - unreachable, satisfies the generator type

    monkeypatch.setattr(worker_purge, "make_uow", _boom)


def _stub_no_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the ``storage is None`` resolution branch, hermetically.

    ``storage=None`` makes the wrapper resolve via
    ``_build_storage(get_settings())``; both are stubbed so the test
    never reads process env and always lands on ``resolved_storage is
    None``.
    """
    monkeypatch.setattr(worker_purge, "get_settings", lambda: None)
    monkeypatch.setattr(worker_purge, "_build_storage", lambda _settings: None)


# ---------------------------------------------------------------------------
# Branch (a): storage present -> delegate + surface the report
# ---------------------------------------------------------------------------


def test_branch_a_delegates_and_reports_purged_rows(
    session_factory: sessionmaker[Session],
    patch_make_uow: None,
) -> None:
    with session_factory() as seed, tenant_agnostic():
        owner = bootstrap_user(seed, email="due@example.com", display_name="Due")
        workspace = bootstrap_workspace(
            seed,
            slug="due-delete",
            name="Due Delete",
            owner_user_id=owner.id,
        )
        workspace.archived_at = _PINNED - timedelta(days=15)
        workspace.delete_requested_at = _PINNED - timedelta(days=15)
        workspace.purge_after = _PINNED - timedelta(days=1)
        workspace_id = workspace.id
        seed.commit()

    report = worker_purge.purge_due_workspace_deletions(
        clock=FrozenClock(_PINNED),
        storage=InMemoryStorage(),
    )

    assert isinstance(report, WorkspacePurgeReport)
    assert report.purged == 1
    assert report.workspace_ids == (workspace_id,)

    with session_factory() as check, tenant_agnostic():
        assert check.get(Workspace, workspace_id) is None


def test_branch_a_forwards_storage_clock_and_limit(
    monkeypatch: pytest.MonkeyPatch,
    patch_make_uow: None,
) -> None:
    # Spy the delegation target to pin exact argument forwarding without
    # seeding a full DB. The wrapper still runs its real body (clock /
    # storage resolution, UoW open, isinstance guard).
    sentinel = WorkspacePurgeReport(
        purged=3, workspace_ids=("w1", "w2", "w3"), deleted_blob_hashes=()
    )
    calls: list[dict[str, object]] = []

    def _spy(
        session: Session, *, storage: object, clock: object, limit: int | None
    ) -> WorkspacePurgeReport:
        calls.append(
            {
                "session": session,
                "storage": storage,
                "clock": clock,
                "limit": limit,
            }
        )
        return sentinel

    monkeypatch.setattr(worker_purge, "purge_due_workspaces", _spy)

    storage = InMemoryStorage()
    clock = FrozenClock(_PINNED)
    report = worker_purge.purge_due_workspace_deletions(
        clock=clock,
        storage=storage,
        limit=5,
    )

    assert report is sentinel
    assert len(calls) == 1
    call = calls[0]
    assert isinstance(call["session"], Session)
    assert call["storage"] is storage
    assert call["clock"] is clock
    assert call["limit"] == 5


# ---------------------------------------------------------------------------
# Branch (b): storage unavailable + require_storage=False -> warn, purged=0
# ---------------------------------------------------------------------------


def test_branch_b_no_storage_warns_and_returns_empty_report(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _stub_no_storage(monkeypatch)
    _forbid_make_uow(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=worker_purge.__name__):
        report = worker_purge.purge_due_workspace_deletions(
            clock=FrozenClock(_PINNED),
            storage=None,
            require_storage=False,
        )

    assert report == WorkspacePurgeReport(
        purged=0, workspace_ids=(), deleted_blob_hashes=()
    )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert getattr(warnings[0], "event", None) == "workspace.purge.skipped_no_storage"


# ---------------------------------------------------------------------------
# Branch (c): storage unavailable + require_storage=True -> raise
# ---------------------------------------------------------------------------


def test_branch_c_no_storage_with_require_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_no_storage(monkeypatch)
    _forbid_make_uow(monkeypatch)

    with pytest.raises(RuntimeError, match="storage backend is unavailable"):
        worker_purge.purge_due_workspace_deletions(
            clock=FrozenClock(_PINNED),
            storage=None,
            require_storage=True,
        )
