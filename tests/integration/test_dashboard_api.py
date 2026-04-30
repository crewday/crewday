"""Integration coverage for the manager dashboard aggregate API."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.issues.models import IssueReport
from app.adapters.db.llm.models import ApprovalRequest
from app.adapters.db.places.models import Area, Property, PropertyWorkspace
from app.adapters.db.stays.models import Reservation
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.time.models import Leave, Shift
from app.adapters.db.workspace.models import UserWorkspace, WorkEngagement
from app.api import deps as api_deps
from app.api.v1.dashboard import build_dashboard_router
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

_NOW = datetime.now(tz=UTC).replace(hour=12, minute=0, second=0, microsecond=0)


def _ctx(
    workspace_id: str,
    actor_id: str,
    *,
    slug: str,
    grant_role: Literal["manager", "worker", "client", "guest"] = "manager",
    owner_member: bool = True,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=owner_member,
        audit_correlation_id="corr_dashboard_api",
    )


def _client(session: Session, ctx: WorkspaceContext) -> TestClient:
    app = FastAPI()
    app.include_router(build_dashboard_router())

    def override_ctx() -> WorkspaceContext:
        return ctx

    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[api_deps.current_workspace_context] = override_ctx
    app.dependency_overrides[api_deps.db_session] = override_db
    return TestClient(app)


def _seed_dashboard(
    session: Session,
) -> tuple[WorkspaceContext, TestClient, str, str, str]:
    suffix = new_ulid().lower()
    leave_id = f"leave_dashboard_api_{suffix}"
    manager = bootstrap_user(
        session,
        email=f"dashboard-manager-{suffix}@example.com",
        display_name="Dashboard Manager",
    )
    worker = bootstrap_user(
        session,
        email=f"dashboard-worker-{suffix}@example.com",
        display_name="Dashboard Worker",
    )
    workspace = bootstrap_workspace(
        session,
        slug=f"dashboard-api-{suffix}",
        name="Dashboard API",
        owner_user_id=manager.id,
    )
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace.id,
            user_id=worker.id,
            grant_role="worker",
            scope_kind="workspace",
            scope_property_id=None,
            created_at=_NOW,
            created_by_user_id=manager.id,
        )
    )
    session.add(
        UserWorkspace(
            user_id=worker.id,
            workspace_id=workspace.id,
            source="workspace_grant",
            added_at=_NOW,
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
            started_on=_NOW.date(),
            archived_on=None,
            notes_md="",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    property_id = f"prop_dashboard_api_{suffix}"
    area_id = f"area_dashboard_api_kitchen_{suffix}"
    session.add(
        Property(
            id=property_id,
            name="Dashboard Villa",
            kind="str",
            address="1 Dashboard Lane",
            address_json={"line1": "1 Dashboard Lane", "city": "Nice", "country": "FR"},
            country="FR",
            timezone="Europe/Paris",
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_NOW,
            updated_at=_NOW,
            deleted_at=None,
        )
    )
    session.add(
        PropertyWorkspace(
            property_id=property_id,
            workspace_id=workspace.id,
            label="Dashboard Villa",
            membership_role="owner_workspace",
            share_guest_identity=True,
            status="active",
            created_at=_NOW,
        )
    )
    session.flush()
    session.add(
        Area(
            id=area_id,
            property_id=property_id,
            unit_id=None,
            name="Kitchen",
            label="Kitchen",
            kind="indoor_room",
            icon=None,
            ordering=1,
            parent_area_id=None,
            notes_md="",
            created_at=_NOW,
            updated_at=_NOW,
            deleted_at=None,
        )
    )
    session.flush()
    session.add(
        Shift(
            id=new_ulid(),
            workspace_id=workspace.id,
            user_id=worker.id,
            starts_at=_NOW - timedelta(hours=1),
            ends_at=None,
            property_id=property_id,
            source="manual",
            notes_md=None,
            approved_by=None,
            approved_at=None,
        )
    )
    session.add(
        Occurrence(
            id=new_ulid(),
            workspace_id=workspace.id,
            schedule_id=None,
            template_id=None,
            property_id=property_id,
            assignee_user_id=worker.id,
            starts_at=_NOW + timedelta(hours=2),
            ends_at=_NOW + timedelta(hours=3),
            scheduled_for_local=f"{_NOW.date().isoformat()}T16:00",
            originally_scheduled_for=f"{_NOW.date().isoformat()}T16:00",
            state="in_progress",
            cancellation_reason=None,
            title="Turnover clean",
            description_md="",
            priority="high",
            photo_evidence="required",
            duration_minutes=45,
            area_id=area_id,
            unit_id=None,
            expected_role_id=None,
            linked_instruction_ids=["inst_dashboard"],
            inventory_consumption_json={},
            is_personal=False,
            created_by_user_id=manager.id,
            created_at=_NOW,
        )
    )
    session.add(
        ApprovalRequest(
            id=f"approval_dashboard_api_{suffix}",
            workspace_id=workspace.id,
            requester_actor_id=manager.id,
            action_json={
                "tool_name": "tasks.create",
                "tool_input": {"property_id": property_id},
                "card_summary": "Create turnover task",
                "card_risk": "medium",
                "inline_channel": "web_owner_sidebar",
            },
            status="pending",
            decided_by=None,
            decided_at=None,
            rationale_md=None,
            created_at=_NOW,
            expires_at=_NOW + timedelta(minutes=15),
            result_json=None,
            decision_note_md=None,
            inline_channel="web_owner_sidebar",
            for_user_id=manager.id,
            resolved_user_mode="strict",
        )
    )
    session.add(
        Leave(
            id=leave_id,
            workspace_id=workspace.id,
            user_id=worker.id,
            kind="vacation",
            starts_at=_NOW + timedelta(days=3),
            ends_at=_NOW + timedelta(days=5),
            status="pending",
            reason_md="Family trip",
            decided_by=None,
            decided_at=None,
            created_at=_NOW,
        )
    )
    session.add(
        IssueReport(
            id=f"issue_dashboard_api_{suffix}",
            workspace_id=workspace.id,
            reported_by_user_id=worker.id,
            property_id=property_id,
            area_id=area_id,
            area_label="Kitchen",
            task_id=None,
            title="Loose handle",
            description_md="Cupboard handle is loose",
            severity="high",
            category="broken",
            state="open",
            attachment_file_ids_json=[],
            converted_to_task_id=None,
            resolution_note=None,
            resolved_at=None,
            resolved_by=None,
            created_at=_NOW,
            updated_at=_NOW,
            deleted_at=None,
        )
    )
    session.add(
        Reservation(
            id=f"stay_dashboard_api_{suffix}",
            workspace_id=workspace.id,
            property_id=property_id,
            ical_feed_id=None,
            external_uid=f"stay-dashboard-api-{suffix}",
            check_in=_NOW - timedelta(days=1),
            check_out=_NOW + timedelta(days=2),
            guest_name="Ada Guest",
            guest_count=2,
            status="checked_in",
            source="manual",
            raw_summary=None,
            raw_description=None,
            guest_link_id=None,
            created_at=_NOW,
        )
    )
    session.commit()
    ctx = _ctx(workspace.id, manager.id, slug=workspace.slug)
    return ctx, _client(session, ctx), worker.id, property_id, leave_id


def test_dashboard_aggregates_manager_home_payload(db_session: Session) -> None:
    _ctx, client, worker_id, property_id, _leave_id = _seed_dashboard(db_session)

    response = client.get("/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["employees"][1]["id"] == worker_id
    assert payload["on_booking"][0]["id"] == worker_id
    assert payload["properties"][0]["id"] == property_id
    assert payload["properties"][0]["areas"] == ["Kitchen"]
    assert payload["by_status"]["in_progress"][0]["title"] == "Turnover clean"
    assert payload["by_status"]["in_progress"][0]["area"] == "Kitchen"
    assert payload["pending_approvals"][0]["id"]
    assert payload["pending_approvals"][0]["risk"] == "medium"
    assert payload["pending_approvals"][0]["gate_destination"] == "inline_chat"
    assert payload["pending_leaves"][0]["id"] == _leave_id
    assert payload["pending_leaves"][0]["note"] == "Family trip"
    assert payload["open_issues"][0]["title"] == "Loose handle"
    assert payload["stays_today"][0]["guest_name"] == "Ada Guest"
    assert payload["stays_today"][0]["status"] == "in_house"


def test_dashboard_leave_decision_alias_approves_pending_leave(
    db_session: Session,
) -> None:
    _ctx, client, worker_id, _property_id, leave_id = _seed_dashboard(db_session)

    response = client.post(f"/leaves/{leave_id}/approve")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == leave_id
    assert payload["employee_id"] == worker_id
    assert payload["approved_at"] is not None
    row = db_session.scalar(select(Leave).where(Leave.id == leave_id))
    assert row is not None
    assert row.status == "approved"


def test_dashboard_requires_employee_read_permission(db_session: Session) -> None:
    ctx, _manager_client, worker_id, _property_id, _leave_id = _seed_dashboard(
        db_session
    )
    worker_ctx = _ctx(
        ctx.workspace_id,
        worker_id,
        slug=ctx.workspace_slug,
        grant_role="worker",
        owner_member=False,
    )
    client = _client(db_session, worker_ctx)

    response = client.get("/dashboard")

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "error": "permission_denied",
        "action_key": "employees.read",
    }
