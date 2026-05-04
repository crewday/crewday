"""Integration tests for payslip PDF rendering, storage, and fetch auth."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import IO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.payroll.models import PayPeriod, PayRule, Payslip
from app.adapters.db.payroll.repositories import SqlAlchemyPayslipPdfRepository
from app.adapters.storage.ports import Blob
from app.api.deps import current_workspace_context, db_session, get_storage
from app.api.errors import add_exception_handlers
from app.api.v1.payroll import build_payroll_router
from app.domain.payroll import pdf as payslip_pdf
from app.domain.payroll.pdf import render_payslip
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests._fakes.storage import InMemoryStorage
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_START = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
_END = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


class CountingStorage(InMemoryStorage):
    """In-memory storage with a visible put counter."""

    def __init__(self) -> None:
        super().__init__()
        self.put_count = 0

    def put(
        self,
        content_hash: str,
        data: IO[bytes],
        *,
        content_type: str | None = None,
    ) -> Blob:
        self.put_count += 1
        return super().put(content_hash, data, content_type=content_type)


@dataclass(frozen=True, slots=True)
class SeededPayslipPdf:
    workspace_id: str
    slug: str
    manager_id: str
    worker_id: str
    peer_id: str
    payslip_id: str
    peer_payslip_id: str
    manager_ctx: WorkspaceContext
    worker_ctx: WorkspaceContext
    peer_ctx: WorkspaceContext


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


def _pay_rule(session: Session, *, workspace_id: str, user_id: str) -> None:
    session.add(
        PayRule(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            currency="USD",
            base_cents_per_hour=1429,
            overtime_multiplier=Decimal("1.50"),
            night_multiplier=Decimal("1.00"),
            weekend_multiplier=Decimal("1.00"),
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            effective_to=None,
            created_by=None,
            created_at=_NOW,
        )
    )


def _canonical_payslip(
    *,
    workspace_id: str,
    period_id: str,
    user_id: str,
) -> Payslip:
    return Payslip(
        id=new_ulid(),
        workspace_id=workspace_id,
        pay_period_id=period_id,
        user_id=user_id,
        shift_hours_decimal=Decimal("151.67"),
        overtime_hours_decimal=Decimal("12.00"),
        gross_cents=230000,
        deductions_cents={},
        net_cents=230000,
        components_json={
            "schema_version": 1,
            "currency": "USD",
            "gross_breakdown": [
                {"key": "base_pay", "cents": 200000},
                {"key": "overtime_150", "cents": 30000},
                {"key": "holiday_bonus", "cents": 0},
            ],
            "deductions": [{"key": "adjustment", "cents": 0, "reason": None}],
            "statutory": [],
            "metadata": {
                "hours_regular": 151.67,
                "hours_overtime_150": 12.0,
                "hourly_rate_cents": 1429,
            },
        },
        status="issued",
        issued_at=_NOW,
        payout_snapshot_json={
            "schema_version": 1,
            "destinations": [{"label": "Payout", "display_stub": "Bank ending 1234"}],
            "reimbursements": [],
        },
        created_at=_NOW,
    )


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> SeededPayslipPdf:
    tag = new_ulid()[-8:].lower()
    with session_factory() as session, tenant_agnostic():
        owner = bootstrap_user(
            session, email=f"pdf-owner-{tag}@example.com", display_name="Owner"
        )
        manager = bootstrap_user(
            session, email=f"pdf-manager-{tag}@example.com", display_name="Manager"
        )
        worker = bootstrap_user(
            session, email=f"pdf-worker-{tag}@example.com", display_name="Worker"
        )
        peer = bootstrap_user(
            session, email=f"pdf-peer-{tag}@example.com", display_name="Peer"
        )
        workspace = bootstrap_workspace(
            session,
            slug=f"payslip-pdf-{tag}",
            name="Canonical Payroll",
            owner_user_id=owner.id,
        )
        workspace.settings_json = {
            "payroll.payslip.registered_name": "Canonical Payroll LLC",
            "payroll.payslip.address": "1 Payroll Way",
        }
        _grant(
            session, workspace_id=workspace.id, user_id=manager.id, grant_role="manager"
        )
        _grant(
            session, workspace_id=workspace.id, user_id=worker.id, grant_role="worker"
        )
        _grant(session, workspace_id=workspace.id, user_id=peer.id, grant_role="worker")
        _pay_rule(session, workspace_id=workspace.id, user_id=worker.id)
        _pay_rule(session, workspace_id=workspace.id, user_id=peer.id)
        period = PayPeriod(
            id=new_ulid(),
            workspace_id=workspace.id,
            starts_at=_START,
            ends_at=_END,
            state="locked",
            locked_at=_NOW,
            locked_by=manager.id,
            created_at=_NOW,
        )
        session.add(period)
        session.flush()
        worker_payslip = _canonical_payslip(
            workspace_id=workspace.id,
            period_id=period.id,
            user_id=worker.id,
        )
        peer_payslip = _canonical_payslip(
            workspace_id=workspace.id,
            period_id=period.id,
            user_id=peer.id,
        )
        session.add_all([worker_payslip, peer_payslip])
        session.commit()

        workspace_id = workspace.id
        slug = workspace.slug
        manager_id = manager.id
        worker_id = worker.id
        peer_id = peer.id
        payslip_id = worker_payslip.id
        peer_payslip_id = peer_payslip.id

    return SeededPayslipPdf(
        workspace_id=workspace_id,
        slug=slug,
        manager_id=manager_id,
        worker_id=worker_id,
        peer_id=peer_id,
        payslip_id=payslip_id,
        peer_payslip_id=peer_payslip_id,
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
        peer_ctx=build_workspace_context(
            workspace_id=workspace_id,
            workspace_slug=slug,
            actor_id=peer_id,
            actor_kind="user",
            actor_grant_role="worker",
            actor_was_owner_member=False,
        ),
    )


def _client_for(
    session_factory: sessionmaker[Session],
    ctx: WorkspaceContext,
    storage: InMemoryStorage,
) -> TestClient:
    app = FastAPI()
    add_exception_handlers(app)
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

    app.dependency_overrides[db_session] = _session
    app.dependency_overrides[current_workspace_context] = lambda: ctx
    app.dependency_overrides[get_storage] = lambda: storage
    return TestClient(app)


def _audit_count(
    session_factory: sessionmaker[Session],
    *,
    workspace_id: str,
    entity_id: str,
) -> int:
    with session_factory() as session, tenant_agnostic():
        return int(
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.workspace_id == workspace_id,
                    AuditLog.entity_id == entity_id,
                    AuditLog.action == "payslip.pdf_rendered",
                )
            )
            or 0
        )


def test_render_payslip_pdf_stores_hash_and_is_idempotent(
    session_factory: sessionmaker[Session],
    seeded: SeededPayslipPdf,
) -> None:
    storage = CountingStorage()
    with session_factory() as session, tenant_agnostic():
        repo = SqlAlchemyPayslipPdfRepository(session)
        first = render_payslip(
            repo,
            seeded.manager_ctx,
            storage,
            payslip_id=seeded.payslip_id,
        )
        second = render_payslip(
            repo,
            seeded.manager_ctx,
            storage,
            payslip_id=seeded.payslip_id,
        )
        forced = render_payslip(
            repo,
            seeded.manager_ctx,
            storage,
            payslip_id=seeded.payslip_id,
            force=True,
        )
        session.commit()

    assert first.rendered is True
    assert second.rendered is False
    assert forced.rendered is True
    assert second.content_hash == first.content_hash
    assert storage.put_count == 2

    with storage.get(first.content_hash) as blob:
        pdf = blob.read()
    assert pdf.startswith(b"%PDF")

    with session_factory() as session, tenant_agnostic():
        persisted = session.get(Payslip, seeded.payslip_id)
        assert persisted is not None
        assert persisted.pdf_blob_hash == first.content_hash

    assert (
        _audit_count(
            session_factory,
            workspace_id=seeded.workspace_id,
            entity_id=seeded.payslip_id,
        )
        == 2
    )


def test_payslip_pdf_payout_lines_only_use_display_stub() -> None:
    assert payslip_pdf._payout_lines(
        {
            "destinations": [
                {
                    "label": "Primary bank",
                    "display_stub": "Bank ending 1234",
                    "destination": "FR7612345678901234567890185",
                }
            ]
        }
    ) == ["Primary bank: Bank ending 1234"]
    assert payslip_pdf._payout_lines(
        {
            "destinations": [
                {
                    "label": "Primary bank",
                    "destination": "FR7612345678901234567890185",
                }
            ]
        }
    ) == ["Primary bank"]


def test_payslip_pdf_fetch_permissions(
    session_factory: sessionmaker[Session],
    seeded: SeededPayslipPdf,
) -> None:
    storage = CountingStorage()

    with _client_for(session_factory, seeded.manager_ctx, storage) as client:
        manager_response = client.get(
            f"/api/v1/payroll/payslips/{seeded.payslip_id}/pdf"
        )
    assert manager_response.status_code == 200, manager_response.text
    assert manager_response.headers["content-type"] == "application/pdf"
    assert manager_response.content.startswith(b"%PDF")
    assert manager_response.headers["content-disposition"] == (
        'inline; filename="payslip.pdf"'
    )

    with _client_for(session_factory, seeded.manager_ctx, storage) as client:
        forced = client.post(
            f"/api/v1/payroll/payslips/{seeded.payslip_id}/pdf",
            params={"force": True},
        )
    assert forced.status_code == 200, forced.text
    assert forced.json()["rendered"] is True

    with _client_for(session_factory, seeded.worker_ctx, storage) as client:
        worker_response = client.get(
            f"/api/v1/payroll/payslips/{seeded.payslip_id}/pdf"
        )
        worker_forbidden_force = client.post(
            f"/api/v1/payroll/payslips/{seeded.payslip_id}/pdf",
            params={"force": True},
        )
    assert worker_response.status_code == 200, worker_response.text
    assert worker_response.content.startswith(b"%PDF")
    assert worker_forbidden_force.status_code == 403

    with _client_for(session_factory, seeded.peer_ctx, storage) as client:
        denied = client.get(f"/api/v1/payroll/payslips/{seeded.payslip_id}/pdf")
        own = client.get(f"/api/v1/payroll/payslips/{seeded.peer_payslip_id}/pdf")

    assert denied.status_code == 403
    assert own.status_code == 200, own.text


def test_payslip_pdf_routes_are_present_in_openapi(
    session_factory: sessionmaker[Session],
    seeded: SeededPayslipPdf,
) -> None:
    storage = CountingStorage()
    with _client_for(session_factory, seeded.manager_ctx, storage) as client:
        schema = client.app.openapi()

    path = schema["paths"]["/api/v1/payroll/payslips/{payslip_id}/pdf"]
    assert path["get"]["operationId"] == "payroll.payslips.pdf"
    assert path["post"]["operationId"] == "payroll.payslips.pdf.render"
