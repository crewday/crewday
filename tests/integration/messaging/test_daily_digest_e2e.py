"""Integration coverage for the daily digest worker."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.messaging.models import DigestRecord, Notification
from app.adapters.db.tasks.models import Occurrence
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.tasks.daily_digest import send_daily_digest
from tests._fakes.mailer import InMemoryMailer
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


def _system_ctx(workspace_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="digest",
        actor_id="00000000000000000000000000",
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id="00000000000000000000000000",
        principal_kind="system",
    )


def _grant_worker(session: Session, *, workspace_id: str, user_id: str) -> None:
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role="worker",
            scope_kind="workspace",
            created_at=_NOW,
        )
    )


def _task(
    session: Session,
    *,
    workspace_id: str,
    assignee_user_id: str,
    hour: int,
    title: str,
) -> None:
    session.add(
        Occurrence(
            id=new_ulid(),
            workspace_id=workspace_id,
            assignee_user_id=assignee_user_id,
            starts_at=_NOW.replace(hour=hour),
            ends_at=_NOW.replace(hour=hour + 1),
            state="pending",
            title=title,
            created_at=_NOW,
        )
    )


def test_daily_digest_sends_one_email_each_for_three_user_workspace(
    db_session: Session,
) -> None:
    owner = bootstrap_user(
        db_session,
        email="owner-digest@example.test",
        display_name="Owner",
        clock=FrozenClock(_NOW),
    )
    workspace = bootstrap_workspace(
        db_session,
        slug="digest",
        name="Digest",
        owner_user_id=owner.id,
        clock=FrozenClock(_NOW),
    )
    worker_a = bootstrap_user(
        db_session,
        email="worker-a-digest@example.test",
        display_name="Worker A",
        clock=FrozenClock(_NOW),
    )
    worker_b = bootstrap_user(
        db_session,
        email="worker-b-digest@example.test",
        display_name="Worker B",
        clock=FrozenClock(_NOW),
    )
    _grant_worker(db_session, workspace_id=workspace.id, user_id=worker_a.id)
    _grant_worker(db_session, workspace_id=workspace.id, user_id=worker_b.id)
    _task(
        db_session,
        workspace_id=workspace.id,
        assignee_user_id=worker_a.id,
        hour=9,
        title="Restock linen",
    )
    _task(
        db_session,
        workspace_id=workspace.id,
        assignee_user_id=worker_b.id,
        hour=10,
        title="Inspect terrace",
    )
    db_session.flush()

    mailer = InMemoryMailer()
    report = send_daily_digest(
        _system_ctx(workspace.id),
        session=db_session,
        mailer=mailer,
        clock=FrozenClock(_NOW),
    )

    assert report.recipients_considered == 3
    assert report.sent == 3
    assert len(mailer.sent) == 3
    assert {message.to[0] for message in mailer.sent} == {
        owner.email,
        worker_a.email,
        worker_b.email,
    }
    assert db_session.scalar(select(func.count()).select_from(DigestRecord)) == 3
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.kind == "daily_digest")
        )
        == 3
    )

    rerun_report = send_daily_digest(
        _system_ctx(workspace.id),
        session=db_session,
        mailer=mailer,
        clock=FrozenClock(_NOW),
    )

    assert rerun_report.sent == 0
    assert rerun_report.skipped_existing == 3
    assert len(mailer.sent) == 3
    assert db_session.scalar(select(func.count()).select_from(DigestRecord)) == 3
