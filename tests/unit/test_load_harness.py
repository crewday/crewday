"""Focused tests for the Locust load harness and deterministic seeder."""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from scripts.seed_load import (
    LoadSeedConfig,
    deterministic_ids,
    ensure_load_seed,
    stable_load_id,
)


def _load_all_models() -> None:
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
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


def test_locustfile_imports_without_running_load() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util; "
                "import sys; "
                "spec = importlib.util.spec_from_file_location("
                "'crewday_load_locustfile', 'tests/load/locustfile.py'); "
                "module = importlib.util.module_from_spec(spec); "
                "assert spec.loader is not None; "
                "sys.modules['crewday_load_locustfile'] = module; "
                "spec.loader.exec_module(module); "
                "assert module.CLOCK_IN_P95_BUDGET_MS == 500; "
                "assert module.OCCURRENCES_LIST_P95_BUDGET_MS == 250; "
                "assert module.CONFIG.host == 'http://127.0.0.1:8100'; "
                "assert module.CONFIG.time_prefix == "
                "'/w/load/api/v1/time/shifts'; "
                "assert module.CONFIG.tasks_prefix == "
                "'/w/load/api/v1/tasks/tasks'; "
                "assert module.CrewdayLoadUser.abstract is True; "
                "assert module.ClockingInUser is not None; "
                "assert module.OccurrencesListUser is not None; "
                "assert module.TurnoverDayUser is not None; "
                "assert len(module._PNG_5_MIB) == 5 * 1024 * 1024; "
                "from app.adapters.storage.mime import FiletypeMimeSniffer; "
                "assert FiletypeMimeSniffer().sniff(module._PNG_5_MIB, "
                "hint='image/png') == 'image/png'"
            ),
        ],
        check=False,
        cwd=".",
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_locust_budget_enforcement_sets_process_exit_code() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util; "
                "import sys; "
                "from locust.env import Environment; "
                "spec = importlib.util.spec_from_file_location("
                "'crewday_load_locustfile', 'tests/load/locustfile.py'); "
                "module = importlib.util.module_from_spec(spec); "
                "assert spec.loader is not None; "
                "sys.modules['crewday_load_locustfile'] = module; "
                "spec.loader.exec_module(module); "
                "empty = Environment(); "
                "module.enforce_latency_budgets(empty); "
                "assert empty.process_exit_code == 1; "
                "breach = Environment(); "
                "breach.stats.log_request('POST', 'clock_in', 600, 1); "
                "breach.stats.log_request('GET', 'occurrences_list', 100, 1); "
                "breach.stats.log_request('POST', 'photo_upload', 100, 1); "
                "module.enforce_latency_budgets(breach); "
                "assert breach.process_exit_code == 1; "
                "passing = Environment(); "
                "passing.stats.log_request('POST', 'clock_in', 100, 1); "
                "passing.stats.log_request('GET', 'occurrences_list', 100, 1); "
                "passing.stats.log_request('POST', 'photo_upload', 100, 1); "
                "module.enforce_latency_budgets(passing); "
                "assert passing.process_exit_code is None"
            ),
        ],
        check=False,
        cwd=".",
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_stable_load_id_is_deterministic_and_short() -> None:
    first = stable_load_id("worker", "load", 7)
    second = stable_load_id("worker", "load", 7)

    assert first == second
    assert len(first) == 26
    assert first != stable_load_id("worker", "load", 8)


def test_ensure_load_seed_is_idempotent(session: Session) -> None:
    now = datetime(2026, 4, 30, 8, 0, tzinfo=UTC)
    workspace_id = "LDWORKSPACE00000000000000"
    session.add(
        Workspace(
            id=workspace_id,
            slug="load",
            name="load",
            plan="free",
            quota_json={},
            settings_json={},
            default_timezone="UTC",
            default_locale="en",
            default_currency="USD",
            created_at=now,
        )
    )
    session.flush()
    config = LoadSeedConfig(worker_count=3, occurrence_count=7, turnover_count=2)

    first = ensure_load_seed(session, config, now=now)
    second = ensure_load_seed(session, config, now=now)

    assert first == second
    assert first == deterministic_ids(config, workspace_id=workspace_id)
    assert len(first.worker_ids) == 3
    assert len(first.occurrence_ids) == 7
    assert len(first.turnover_occurrence_ids) == 2
    assert session.scalar(select(Workspace).where(Workspace.slug == "load")) is not None
    assert session.scalar(select(func.count()).select_from(User)) == 3
    assert session.scalar(select(func.count()).select_from(UserWorkspace)) == 3
    assert session.scalar(select(func.count()).select_from(RoleGrant)) == 3
    assert session.scalar(select(func.count()).select_from(Occurrence)) == 7


def test_ensure_load_seed_chunks_existing_occurrence_lookup(session: Session) -> None:
    now = datetime(2026, 4, 30, 8, 0, tzinfo=UTC)
    session.add(
        Workspace(
            id="LDWORKSPACECHUNK000000000",
            slug="load",
            name="load",
            plan="free",
            quota_json={},
            settings_json={},
            default_timezone="UTC",
            default_locale="en",
            default_currency="USD",
            created_at=now,
        )
    )
    session.flush()
    config = LoadSeedConfig(worker_count=1, occurrence_count=1_001, turnover_count=1)

    first = ensure_load_seed(session, config, now=now)
    second = ensure_load_seed(session, config, now=now)

    assert first == second
    assert session.scalar(select(func.count()).select_from(Occurrence)) == 1_001
