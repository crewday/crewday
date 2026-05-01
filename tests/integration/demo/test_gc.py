"""Integration tests for demo workspace garbage collection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.demo.models import DemoWorkspace
from app.adapters.db.session import UnitOfWorkImpl
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.config import Settings
from app.util.clock import FrozenClock
from app.worker.jobs import demo as demo_jobs

pytestmark = pytest.mark.integration


def test_demo_gc_purges_expired_workspace_rows_and_upload_dir(
    engine: Engine,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    workspace_id = "01HWA00000000000000000WSP"
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    monkeypatch.setattr(
        demo_jobs,
        "make_uow",
        lambda: UnitOfWorkImpl(factory),
    )
    with factory() as session:
        session.add(
            Workspace(
                id=workspace_id,
                slug="expired-demo",
                name="Expired Demo",
                plan="free",
                quota_json={},
                settings_json={},
                default_timezone="UTC",
                default_locale="en",
                default_currency="USD",
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(days=2),
            )
        )
        session.flush()
        session.add(
            DemoWorkspace(
                id=workspace_id,
                scenario_key="rental-manager",
                seed_digest="digest",
                created_at=now - timedelta(days=2),
                last_activity_at=now - timedelta(days=2),
                expires_at=now - timedelta(seconds=1),
                cookie_binding_digest="binding",
            )
        )
        session.add(
            UserWorkspace(
                workspace_id=workspace_id,
                user_id="01HWA00000000000000000USR",
                source="workspace_grant",
                added_at=now - timedelta(days=2),
            )
        )
        session.commit()

    upload_dir = tmp_path / "demo" / workspace_id
    upload_dir.mkdir(parents=True)
    (upload_dir / "photo.jpg").write_bytes(b"demo")

    # Baseline orphans from any other tests sharing this DB (xdist
    # workers don't roll back; see the integration conftest). The purge
    # must not introduce *new* orphans regardless of whatever residue
    # earlier tests left behind.
    with factory() as session:
        baseline_orphans = demo_jobs.count_workspace_id_orphans(session)

    report = demo_jobs.purge_expired_demo_workspaces(
        settings=_settings(tmp_path),
        clock=FrozenClock(now),
    )

    assert report.purged == 1
    assert report.workspace_ids == (workspace_id,)
    assert not upload_dir.exists()
    with factory() as session:
        assert session.get(Workspace, workspace_id) is None
        assert session.get(DemoWorkspace, workspace_id) is None
        assert (
            session.scalar(
                select(UserWorkspace).where(UserWorkspace.workspace_id == workspace_id)
            )
            is None
        )
        assert demo_jobs.count_workspace_id_orphans(session) == baseline_orphans


def _settings(data_dir) -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        data_dir=data_dir,
        root_key=SecretStr("demo-root-key"),
        demo_cookie_key=SecretStr("demo-cookie-key"),
        demo_mode=True,
        public_url="https://demo.crew.day",
        bind_host="127.0.0.1",
        worker="external",
    )
