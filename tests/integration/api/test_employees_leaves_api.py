"""Integration coverage for ``GET /employees/{employee_id}/leaves``."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.time.models import Leave
from app.adapters.db.workspace.models import UserWorkspace, WorkEngagement
from app.api.deps import current_workspace_context
from app.api.deps import db_session as db_session_dep
from app.api.v1.employees import build_employees_router
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


def _client(session: Session, ctx: WorkspaceContext) -> TestClient:
    app = FastAPI()
    app.include_router(build_employees_router())

    def override_ctx() -> WorkspaceContext:
        return ctx

    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[current_workspace_context] = override_ctx
    app.dependency_overrides[db_session_dep] = override_db
    return TestClient(app, raise_server_exceptions=False)


def _seed(session: Session) -> tuple[WorkspaceContext, str, str]:
    suffix = new_ulid().lower()
    owner = bootstrap_user(
        session,
        email=f"leaves-owner-{suffix}@example.com",
        display_name="Leaves Owner",
    )
    worker = bootstrap_user(
        session,
        email=f"leaves-worker-{suffix}@example.com",
        display_name="Maya Santos",
    )
    workspace = bootstrap_workspace(
        session,
        slug=f"emp-leaves-{suffix}",
        name="Employee Leaves",
        owner_user_id=owner.id,
    )
    session.add(
        UserWorkspace(
            user_id=worker.id,
            workspace_id=workspace.id,
            source="workspace_grant",
            added_at=_PINNED,
        )
    )
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace.id,
            user_id=worker.id,
            grant_role="worker",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=owner.id,
        )
    )
    session.add(
        WorkEngagement(
            id=new_ulid(),
            user_id=worker.id,
            workspace_id=workspace.id,
            engagement_kind="payroll",
            supplier_org_id=None,
            pay_destination_id=None,
            reimbursement_destination_id=None,
            started_on=_PINNED.date(),
            archived_on=None,
            notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
    )
    session.add(
        Leave(
            id=f"leave_pending_{suffix}",
            workspace_id=workspace.id,
            user_id=worker.id,
            kind="vacation",
            starts_at=_PINNED + timedelta(days=7),
            ends_at=_PINNED + timedelta(days=9),
            status="pending",
            reason_md="Family trip",
            decided_by=None,
            decided_at=None,
            created_at=_PINNED,
        )
    )
    session.add(
        Leave(
            id=f"leave_approved_{suffix}",
            workspace_id=workspace.id,
            user_id=worker.id,
            kind="comp",
            starts_at=_PINNED + timedelta(days=14),
            ends_at=_PINNED + timedelta(days=15),
            status="approved",
            reason_md=None,
            decided_by=owner.id,
            decided_at=_PINNED,
            created_at=_PINNED,
        )
    )
    session.flush()
    ctx = build_workspace_context(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=owner.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    return ctx, owner.id, worker.id


def test_employee_leaves_returns_subject_and_mock_compatible_rows(
    db_session: Session,
) -> None:
    ctx, _owner_id, worker_id = _seed(db_session)
    client = _client(db_session, ctx)

    response = client.get(f"/employees/{worker_id}/leaves")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["subject"]["id"] == worker_id
    assert body["subject"]["name"] == "Maya Santos"
    assert [row["employee_id"] for row in body["leaves"]] == [worker_id, worker_id]
    assert body["leaves"][0]["category"] == "vacation"
    assert body["leaves"][0]["note"] == "Family trip"
    assert body["leaves"][0]["approved_at"] is None
    assert body["leaves"][1]["category"] == "personal"
    assert body["leaves"][1]["approved_at"] == "2026-04-30T12:00:00Z"


def test_employee_leaves_requires_manager_roster_permission(
    db_session: Session,
) -> None:
    ctx, _owner_id, worker_id = _seed(db_session)
    worker_ctx = build_workspace_context(
        workspace_id=ctx.workspace_id,
        workspace_slug=ctx.workspace_slug,
        actor_id=worker_id,
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
    )
    client = _client(db_session, worker_ctx)

    response = client.get(f"/employees/{worker_id}/leaves")

    assert response.status_code == 403, response.text
