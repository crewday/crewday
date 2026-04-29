"""Integration tests for payroll period and payslip HTTP routes."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.payroll.models import Booking, PayPeriod, PayRule, Payslip
from app.adapters.db.workspace.models import WorkEngagement
from app.api.deps import current_workspace_context
from app.api.deps import db_session as _db_session_dep
from app.api.v1.payroll import build_payroll_router
from app.events import bus
from app.events.types import PayrollExportReady
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_START = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
_END = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SeededPayroll:
    workspace_id: str
    slug: str
    owner_id: str
    manager_id: str
    worker_id: str
    peer_id: str
    worker_period_id: str
    manager_ctx: WorkspaceContext
    worker_ctx: WorkspaceContext


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def _grant(
    session: Session, *, workspace_id: str, user_id: str, grant_role: str
) -> None:
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_kind="workspace",
            created_at=_NOW,
            created_by_user_id=None,
        )
    )


def _engagement(session: Session, *, workspace_id: str, user_id: str) -> str:
    engagement = WorkEngagement(
        id=new_ulid(),
        user_id=user_id,
        workspace_id=workspace_id,
        engagement_kind="payroll",
        supplier_org_id=None,
        pay_destination_id=None,
        reimbursement_destination_id=None,
        started_on=date(2026, 1, 1),
        archived_on=None,
        notes_md="",
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(engagement)
    session.flush()
    return engagement.id


def _pay_rule(session: Session, *, workspace_id: str, user_id: str) -> None:
    session.add(
        PayRule(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            currency="USD",
            base_cents_per_hour=1000,
            overtime_multiplier=Decimal("1.50"),
            night_multiplier=Decimal("1.00"),
            weekend_multiplier=Decimal("1.00"),
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            effective_to=None,
            created_by=None,
            created_at=_NOW,
        )
    )


def _payslip(
    *,
    workspace_id: str,
    period_id: str,
    user_id: str,
    gross_cents: int = 4000,
) -> Payslip:
    return Payslip(
        id=new_ulid(),
        workspace_id=workspace_id,
        pay_period_id=period_id,
        user_id=user_id,
        shift_hours_decimal=Decimal("4.00"),
        overtime_hours_decimal=Decimal("0.00"),
        gross_cents=gross_cents,
        deductions_cents={},
        net_cents=gross_cents,
        components_json={"schema_version": 1, "currency": "USD"},
        status="draft",
        created_at=_NOW,
    )


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> SeededPayroll:
    tag = new_ulid()[-8:].lower()
    with session_factory() as session, tenant_agnostic():
        owner = bootstrap_user(
            session, email=f"owner-{tag}@example.com", display_name="Owner"
        )
        manager = bootstrap_user(
            session, email=f"manager-{tag}@example.com", display_name="Manager"
        )
        worker = bootstrap_user(
            session, email=f"worker-{tag}@example.com", display_name="Worker"
        )
        peer = bootstrap_user(
            session, email=f"peer-{tag}@example.com", display_name="Peer"
        )
        workspace = bootstrap_workspace(
            session,
            slug=f"payroll-{tag}",
            name="Payroll Routes",
            owner_user_id=owner.id,
        )
        _grant(
            session, workspace_id=workspace.id, user_id=manager.id, grant_role="manager"
        )
        _grant(
            session, workspace_id=workspace.id, user_id=worker.id, grant_role="worker"
        )
        _grant(session, workspace_id=workspace.id, user_id=peer.id, grant_role="worker")
        worker_engagement_id = _engagement(
            session, workspace_id=workspace.id, user_id=worker.id
        )
        _engagement(session, workspace_id=workspace.id, user_id=peer.id)
        _pay_rule(session, workspace_id=workspace.id, user_id=worker.id)
        _pay_rule(session, workspace_id=workspace.id, user_id=peer.id)
        period = PayPeriod(
            id=new_ulid(),
            workspace_id=workspace.id,
            starts_at=_START,
            ends_at=_END,
            state="open",
            locked_at=None,
            locked_by=None,
            created_at=_NOW,
        )
        session.add(period)
        session.flush()
        session.add(
            Booking(
                id=new_ulid(),
                workspace_id=workspace.id,
                work_engagement_id=worker_engagement_id,
                user_id=worker.id,
                property_id=None,
                client_org_id=None,
                status="completed",
                kind="work",
                pay_basis="scheduled",
                scheduled_start=datetime(2026, 5, 4, 8, 0, tzinfo=UTC),
                scheduled_end=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
                actual_minutes=None,
                actual_minutes_paid=240,
                break_seconds=0,
                adjusted=False,
                adjustment_reason=None,
                pending_amend_minutes=None,
                pending_amend_reason=None,
                cancelled_at=None,
                cancellation_window_hours=24,
                cancellation_pay_to_worker=True,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.commit()
        workspace_id = workspace.id
        slug = workspace.slug
        owner_id = owner.id
        manager_id = manager.id
        worker_id = worker.id
        peer_id = peer.id
        period_id = period.id

    return SeededPayroll(
        workspace_id=workspace_id,
        slug=slug,
        owner_id=owner_id,
        manager_id=manager_id,
        worker_id=worker_id,
        peer_id=peer_id,
        worker_period_id=period_id,
        manager_ctx=build_workspace_context(
            workspace_id=workspace_id,
            workspace_slug=slug,
            actor_id=manager_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
        ),
        worker_ctx=build_workspace_context(
            workspace_id=workspace_id,
            workspace_slug=slug,
            actor_id=worker_id,
            actor_kind="user",
            actor_grant_role="worker",
            actor_was_owner_member=False,
        ),
    )


def _client_for(
    session_factory: sessionmaker[Session],
    ctx: WorkspaceContext,
) -> TestClient:
    app = FastAPI()
    app.include_router(build_payroll_router(), prefix="/api/v1/payroll")

    def _session() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = lambda: ctx
    return TestClient(app)


def _audit_count(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    entity_id: str,
    action: str,
) -> int:
    with session_factory() as session, tenant_agnostic():
        return int(
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.workspace_id == workspace_id,
                    AuditLog.entity_id == entity_id,
                    AuditLog.action == action,
                )
            )
            or 0
        )


def test_pay_period_create_list_get_delete(
    session_factory: sessionmaker[Session],
    seeded: SeededPayroll,
) -> None:
    with _client_for(session_factory, seeded.manager_ctx) as client:
        created = client.post(
            "/api/v1/payroll/pay-periods",
            json={
                "starts_at": datetime(2026, 6, 1, tzinfo=UTC).isoformat(),
                "ends_at": datetime(2026, 7, 1, tzinfo=UTC).isoformat(),
            },
        )
        assert created.status_code == 201, created.text
        period_id = created.json()["id"]

        listed = client.get("/api/v1/payroll/pay-periods")
        assert listed.status_code == 200
        assert period_id in {item["id"] for item in listed.json()["data"]}

        fetched = client.get(f"/api/v1/payroll/pay-periods/{period_id}")
        assert fetched.status_code == 200
        assert fetched.json()["state"] == "open"

        updated = client.patch(
            f"/api/v1/payroll/pay-periods/{period_id}",
            json={
                "starts_at": datetime(2026, 6, 2, tzinfo=UTC).isoformat(),
                "ends_at": datetime(2026, 7, 2, tzinfo=UTC).isoformat(),
            },
        )
        assert updated.status_code == 200
        assert updated.json()["starts_at"] == "2026-06-02T00:00:00Z"

        deleted = client.delete(f"/api/v1/payroll/pay-periods/{period_id}")
        assert deleted.status_code == 204
        missing = client.get(f"/api/v1/payroll/pay-periods/{period_id}")
        assert missing.status_code == 404


def test_lock_pay_period_computes_payslip_and_second_lock_is_noop(
    session_factory: sessionmaker[Session],
    seeded: SeededPayroll,
) -> None:
    with _client_for(session_factory, seeded.manager_ctx) as client:
        first = client.post(
            f"/api/v1/payroll/pay-periods/{seeded.worker_period_id}/lock"
        )
        second = client.post(
            f"/api/v1/payroll/pay-periods/{seeded.worker_period_id}/lock"
        )

    assert first.status_code == 200, first.text
    assert first.json()["state"] == "locked"
    assert second.status_code == 200, second.text
    assert second.json()["state"] == "locked"
    assert (
        _audit_count(
            session_factory,
            workspace_id=seeded.workspace_id,
            entity_id=seeded.worker_period_id,
            action="pay_period.locked",
        )
        == 1
    )

    with session_factory() as session, tenant_agnostic():
        slips = session.scalars(
            select(Payslip).where(Payslip.pay_period_id == seeded.worker_period_id)
        ).all()
        assert len(slips) == 1
        assert slips[0].user_id == seeded.worker_id
        assert slips[0].gross_cents == 4000
        assert slips[0].components_json["currency"] == "USD"


def test_payslip_reads_are_worker_scoped(
    session_factory: sessionmaker[Session],
    seeded: SeededPayroll,
) -> None:
    with session_factory() as session, tenant_agnostic():
        period = session.get(PayPeriod, seeded.worker_period_id)
        assert period is not None
        period.state = "locked"
        worker_slip = _payslip(
            workspace_id=seeded.workspace_id,
            period_id=seeded.worker_period_id,
            user_id=seeded.worker_id,
        )
        peer_slip = _payslip(
            workspace_id=seeded.workspace_id,
            period_id=seeded.worker_period_id,
            user_id=seeded.peer_id,
            gross_cents=5000,
        )
        session.add_all([worker_slip, peer_slip])
        session.commit()
        worker_slip_id = worker_slip.id
        peer_slip_id = peer_slip.id

    with _client_for(session_factory, seeded.manager_ctx) as manager_client:
        manager_list = manager_client.get("/api/v1/payroll/payslips")
    assert manager_list.status_code == 200
    assert {item["id"] for item in manager_list.json()["data"]} == {
        worker_slip_id,
        peer_slip_id,
    }

    with _client_for(session_factory, seeded.worker_ctx) as worker_client:
        own = worker_client.get(f"/api/v1/payroll/payslips/{worker_slip_id}")
        peer = worker_client.get(f"/api/v1/payroll/payslips/{peer_slip_id}")
        own_list = worker_client.get("/api/v1/payroll/payslips")

    assert own.status_code == 200
    assert own.json()["gross"] == {"cents": 4000, "currency": "USD"}
    assert own.json()["net"] == {"cents": 4000, "currency": "USD"}
    assert peer.status_code == 403
    assert [item["id"] for item in own_list.json()["data"]] == [worker_slip_id]


def test_payslip_state_routes_issue_pay_and_void(
    session_factory: sessionmaker[Session],
    seeded: SeededPayroll,
) -> None:
    with session_factory() as session, tenant_agnostic():
        period = session.get(PayPeriod, seeded.worker_period_id)
        assert period is not None
        period.state = "locked"
        paid_slip = _payslip(
            workspace_id=seeded.workspace_id,
            period_id=seeded.worker_period_id,
            user_id=seeded.worker_id,
        )
        voided_slip = _payslip(
            workspace_id=seeded.workspace_id,
            period_id=seeded.worker_period_id,
            user_id=seeded.peer_id,
        )
        session.add_all([paid_slip, voided_slip])
        session.commit()
        paid_slip_id = paid_slip.id
        voided_slip_id = voided_slip.id

    with _client_for(session_factory, seeded.manager_ctx) as client:
        issue = client.post(f"/api/v1/payroll/payslips/{paid_slip_id}/issue")
        issue_replay = client.post(f"/api/v1/payroll/payslips/{paid_slip_id}/issue")
        paid = client.post(f"/api/v1/payroll/payslips/{paid_slip_id}/mark_paid")
        paid_void = client.post(f"/api/v1/payroll/payslips/{paid_slip_id}/void")
        voided = client.post(f"/api/v1/payroll/payslips/{voided_slip_id}/void")

    assert issue.status_code == 200, issue.text
    assert issue.json()["status"] == "issued"
    assert issue.json()["issued_at"] is not None
    assert issue_replay.status_code == 200, issue_replay.text
    assert issue_replay.json()["status"] == "issued"
    assert paid.status_code == 200, paid.text
    assert paid.json()["status"] == "paid"
    assert paid.json()["paid_at"] is not None
    assert paid_void.status_code == 409
    assert voided.status_code == 200, voided.text
    assert voided.json()["status"] == "voided"

    with session_factory() as session, tenant_agnostic():
        persisted = session.get(Payslip, paid_slip_id)
        assert persisted is not None
        assert persisted.status == "paid"
        assert persisted.payout_snapshot_json == {
            "schema_version": 1,
            "destinations": [],
            "reimbursements": [],
        }


def test_period_export_returns_ready_job_and_event(
    session_factory: sessionmaker[Session],
    seeded: SeededPayroll,
) -> None:
    bus._reset_for_tests()
    seen: list[PayrollExportReady] = []

    @bus.subscribe(PayrollExportReady)
    def _capture(event: PayrollExportReady) -> None:
        seen.append(event)

    try:
        with _client_for(session_factory, seeded.manager_ctx) as client:
            response = client.post(
                f"/api/v1/payroll/pay-periods/{seeded.worker_period_id}/exports"
            )
    finally:
        bus._reset_for_tests()

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["kind"] == "payslips"
    assert body["pay_period_id"] == seeded.worker_period_id
    assert len(body["job_id"]) > 8
    assert [(event.job_id, event.pay_period_id, event.kind) for event in seen] == [
        (body["job_id"], seeded.worker_period_id, "payslips")
    ]
    assert (
        _audit_count(
            session_factory,
            workspace_id=seeded.workspace_id,
            entity_id=body["job_id"],
            action="payroll_export.ready",
        )
        == 1
    )


def test_payroll_routes_are_present_in_openapi(
    session_factory: sessionmaker[Session],
    seeded: SeededPayroll,
) -> None:
    with _client_for(session_factory, seeded.manager_ctx) as client:
        schema: dict[str, Any] = cast("FastAPI", client.app).openapi()

    paths = schema["paths"]
    assert "/api/v1/payroll/pay-periods" in paths
    assert "/api/v1/payroll/pay-periods/{period_id}/lock" in paths
    assert "/api/v1/payroll/pay-periods/{period_id}/exports" in paths
    assert "/api/v1/payroll/payslips/{payslip_id}" in paths
    assert "/api/v1/payroll/payslips/{payslip_id}/issue" in paths
    assert "/api/v1/payroll/payslips/{payslip_id}/mark_paid" in paths
    assert "/api/v1/payroll/payslips/{payslip_id}/void" in paths
    assert (
        paths["/api/v1/payroll/pay-periods/{period_id}/lock"]["post"]["operationId"]
        == "payroll.pay_periods.lock"
    )
