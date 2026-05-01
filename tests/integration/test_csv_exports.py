"""Integration tests for payroll CSV export endpoints."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.expenses.models import ExpenseClaim
from app.adapters.db.payroll.models import PayPeriod, PayRule, Payslip
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.time.models import Shift
from app.adapters.db.workspace.models import WorkEngagement
from app.api.deps import current_workspace_context
from app.api.deps import db_session as _db_session_dep
from app.api.v1.payroll import build_payroll_router
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration

_SINCE = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
_UNTIL = datetime(2026, 4, 8, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SeededCsvExport:
    ctx: WorkspaceContext
    period_id: str
    worker_id: str


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> Iterator[SeededCsvExport]:
    tag = new_ulid()[-8:].lower()
    with session_factory() as s:
        owner = bootstrap_user(
            s, email=f"owner-{tag}@example.com", display_name="Owner"
        )
        worker = bootstrap_user(
            s, email=f"worker-{tag}@example.com", display_name="Worker"
        )
        ws = bootstrap_workspace(
            s,
            slug=f"csv-{tag}",
            name="CSV Exports",
            owner_user_id=owner.id,
        )
        property_ = Property(
            id=new_ulid(),
            name="Villa Export",
            kind="residence",
            address="1 Export Lane",
            address_json={"country": "FR"},
            country="FR",
            timezone="Europe/Paris",
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_SINCE,
            updated_at=_SINCE,
        )
        unlinked_property = Property(
            id=new_ulid(),
            name="Foreign Villa",
            kind="residence",
            address="99 Other Lane",
            address_json={"country": "FR"},
            country="FR",
            timezone="Europe/Paris",
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_SINCE,
            updated_at=_SINCE,
        )
        s.add_all([property_, unlinked_property])
        engagement = WorkEngagement(
            id=new_ulid(),
            user_id=worker.id,
            workspace_id=ws.id,
            engagement_kind="payroll",
            started_on=date(2026, 1, 1),
            notes_md="",
            created_at=_SINCE,
            updated_at=_SINCE,
        )
        period = PayPeriod(
            id=new_ulid(),
            workspace_id=ws.id,
            starts_at=_SINCE,
            ends_at=_UNTIL,
            state="paid",
            locked_at=_UNTIL,
            locked_by=owner.id,
            created_at=_SINCE,
        )
        s.add_all([engagement, period])
        s.flush()
        s.add_all(
            [
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws.id,
                    user_id=worker.id,
                    grant_role="worker",
                    scope_kind="workspace",
                    created_at=_SINCE,
                    created_by_user_id=owner.id,
                ),
                PropertyWorkspace(
                    property_id=property_.id,
                    workspace_id=ws.id,
                    label="Villa Export Ledger",
                    membership_role="owner_workspace",
                    status="active",
                    created_at=_SINCE,
                ),
                Shift(
                    id="shift_csv_1",
                    workspace_id=ws.id,
                    user_id=worker.id,
                    starts_at=datetime(2026, 4, 2, 8, 0, tzinfo=UTC),
                    ends_at=datetime(2026, 4, 2, 12, 30, tzinfo=UTC),
                    property_id=property_.id,
                    source="manual",
                    notes_md="covered breakfast",
                    approved_by=owner.id,
                    approved_at=datetime(2026, 4, 2, 13, 0, tzinfo=UTC),
                ),
                Shift(
                    id="shift_csv_unlinked_property",
                    workspace_id=ws.id,
                    user_id=worker.id,
                    starts_at=datetime(2026, 4, 2, 13, 0, tzinfo=UTC),
                    ends_at=datetime(2026, 4, 2, 14, 0, tzinfo=UTC),
                    property_id=unlinked_property.id,
                    source="manual",
                    notes_md="unlinked property should not leak label",
                    approved_by=owner.id,
                    approved_at=datetime(2026, 4, 2, 14, 5, tzinfo=UTC),
                ),
                PayRule(
                    id=new_ulid(),
                    workspace_id=ws.id,
                    user_id=worker.id,
                    currency="EUR",
                    base_cents_per_hour=2000,
                    overtime_multiplier=Decimal("1.50"),
                    night_multiplier=Decimal("1.25"),
                    weekend_multiplier=Decimal("1.50"),
                    effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                    effective_to=None,
                    created_by=owner.id,
                    created_at=_SINCE,
                ),
                Payslip(
                    id="payslip_csv_1",
                    workspace_id=ws.id,
                    pay_period_id=period.id,
                    user_id=worker.id,
                    shift_hours_decimal=Decimal("40.00"),
                    overtime_hours_decimal=Decimal("2.50"),
                    gross_cents=85_000,
                    deductions_cents={"tax": 10_000, "meal": 500},
                    net_cents=74_500,
                    components_json={"currency": "EUR"},
                    status="paid",
                    paid_at=datetime(2026, 4, 9, 9, 0, tzinfo=UTC),
                    created_at=_UNTIL,
                ),
                ExpenseClaim(
                    id="expense_csv_1",
                    workspace_id=ws.id,
                    work_engagement_id=engagement.id,
                    submitted_at=datetime(2026, 4, 3, 10, 0, tzinfo=UTC),
                    vendor="Market",
                    purchased_at=datetime(2026, 4, 3, 9, 30, tzinfo=UTC),
                    currency="EUR",
                    total_amount_cents=1234,
                    category="supplies",
                    property_id=property_.id,
                    note_md="",
                    state="approved",
                    decided_by=owner.id,
                    decided_at=datetime(2026, 4, 3, 11, 0, tzinfo=UTC),
                    reimbursement_destination_id=None,
                    reimbursed_via=None,
                    created_at=datetime(2026, 4, 3, 10, 0, tzinfo=UTC),
                ),
            ]
        )
        s.commit()
        owner_id = owner.id
        worker_id = worker.id
        workspace_id = ws.id
        workspace_slug = ws.slug
        period_id = period.id

    ctx = build_workspace_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=owner_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    try:
        yield SeededCsvExport(
            ctx=ctx,
            period_id=period_id,
            worker_id=worker_id,
        )
    finally:
        with session_factory() as s, tenant_agnostic():
            for row in s.scalars(
                select(AuditLog).where(AuditLog.workspace_id == workspace_id)
            ):
                s.delete(row)
            s.flush()
            # Drop cross-table state in dependency order so SQLite's
            # eager FK checks don't trip on a delete chain that needs
            # several cascades to complete in lockstep (workspace →
            # engagement → org/payout RESTRICT). DELETE statements via
            # ``s.execute`` issue raw DML so the unit-of-work isn't
            # involved in ordering.
            #
            # TODO(test-cleanup): hand-maintained list. Any future
            # workspace-child table that lands without ON DELETE CASCADE
            # to ``workspace`` must be appended here, or the final
            # ``s.delete(workspace)`` below will trip a RESTRICT FK on
            # PG. Replace with a topological sweep driven by FK
            # introspection (``inspect(engine).get_foreign_keys`` over
            # every table referencing ``workspace.id``) once a shared
            # ``cascade_workspace_rows`` test helper exists.
            s.execute(
                text("DELETE FROM expense_claim WHERE workspace_id = :w").bindparams(
                    w=workspace_id
                )
            )
            s.execute(
                text("DELETE FROM payslip WHERE workspace_id = :w").bindparams(
                    w=workspace_id
                )
            )
            s.execute(
                text("DELETE FROM pay_rule WHERE workspace_id = :w").bindparams(
                    w=workspace_id
                )
            )
            s.execute(
                text("DELETE FROM pay_period WHERE workspace_id = :w").bindparams(
                    w=workspace_id
                )
            )
            s.execute(
                text("DELETE FROM shift WHERE workspace_id = :w").bindparams(
                    w=workspace_id
                )
            )
            s.execute(
                text("DELETE FROM work_engagement WHERE workspace_id = :w").bindparams(
                    w=workspace_id
                )
            )
            s.execute(
                text(
                    "DELETE FROM property_workspace WHERE workspace_id = :w"
                ).bindparams(w=workspace_id)
            )
            s.execute(
                text("DELETE FROM role_grant WHERE workspace_id = :w").bindparams(
                    w=workspace_id
                )
            )
            s.flush()
            workspace = s.get(type(ws), workspace_id)
            if workspace is not None:
                s.delete(workspace)
            for property_id in (property_.id, unlinked_property.id):
                property_row = s.get(Property, property_id)
                if property_row is not None:
                    s.delete(property_row)
            for user_id in (owner_id, worker_id):
                user = s.get(type(owner), user_id)
                if user is not None:
                    s.delete(user)
            s.commit()


@pytest.fixture
def client(
    session_factory: sessionmaker[Session],
    seeded: SeededCsvExport,
) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(
        build_payroll_router(),
        prefix=f"/w/{seeded.ctx.workspace_slug}/api/v1/payroll",
    )

    def _session() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def _ctx() -> WorkspaceContext:
        return seeded.ctx

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = _ctx

    with TestClient(app) as c:
        yield c


def _export_url(ctx: WorkspaceContext, kind: str) -> str:
    return f"/w/{ctx.workspace_slug}/api/v1/payroll/exports/{kind}.csv"


def _audit_diff(
    session_factory: sessionmaker[Session], workspace_id: str
) -> dict[str, object]:
    with session_factory() as s, tenant_agnostic():
        row = s.scalars(
            select(AuditLog)
            .where(
                AuditLog.workspace_id == workspace_id,
                AuditLog.action == "payroll.exported",
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        ).first()
        assert row is not None
        assert isinstance(row.diff, dict)
        return row.diff


def test_timesheets_csv_streams_header_bom_rows_and_audit(
    client: TestClient,
    seeded: SeededCsvExport,
    session_factory: sessionmaker[Session],
) -> None:
    response = client.get(
        _export_url(seeded.ctx, "timesheets"),
        params={"since": _SINCE.isoformat(), "until": _UNTIL.isoformat(), "bom": True},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.content.startswith(b"\xef\xbb\xbf")
    lines = response.text.removeprefix("\ufeff").splitlines()
    assert lines[0] == (
        "shift_id,user_email,property_label,starts_at_utc,ends_at_utc,"
        "hours_decimal,source,notes"
    )
    assert "shift_csv_1" in lines[1]
    assert (
        ",Villa Export Ledger,2026-04-02T08:00:00Z,2026-04-02T12:30:00Z,4.50,"
    ) in lines[1]
    assert "shift_csv_unlinked_property" in lines[2]
    assert (
        "shift_csv_unlinked_property,"
        f"worker-{seeded.ctx.workspace_slug.removeprefix('csv-')}@example.com,,"
    ) in lines[2]
    assert "Foreign Villa" not in response.text

    assert _audit_diff(session_factory, seeded.ctx.workspace_id) == {
        "kind": "timesheets",
        "since": "2026-04-01T00:00:00Z",
        "until": "2026-04-08T00:00:00Z",
        "row_count": 2,
    }


def test_payslips_csv_streams_for_period(
    client: TestClient,
    seeded: SeededCsvExport,
    session_factory: sessionmaker[Session],
) -> None:
    response = client.get(
        _export_url(seeded.ctx, "payslips"),
        params={"period_id": seeded.period_id},
    )

    assert response.status_code == 200
    lines = response.text.splitlines()
    assert lines[0] == (
        "payslip_id,user_email,period_starts_at,period_ends_at,hours,"
        "overtime_hours,gross_cents,deductions_cents,net_cents,currency,paid_at"
    )
    assert "payslip_csv_1" in lines[1]
    assert ",40.00,2.50,85000,10500,74500,EUR,2026-04-09T09:00:00Z" in lines[1]

    diff = _audit_diff(session_factory, seeded.ctx.workspace_id)
    assert diff["kind"] == "payslips"
    assert diff["row_count"] == 1
    assert diff["since"] == "2026-04-01T00:00:00Z"
    assert diff["until"] == "2026-04-08T00:00:00Z"


def test_expense_ledger_csv_streams_approved_claims(
    client: TestClient,
    seeded: SeededCsvExport,
    session_factory: sessionmaker[Session],
) -> None:
    response = client.get(
        _export_url(seeded.ctx, "expense-ledger"),
        params={"since": _SINCE.isoformat(), "until": _UNTIL.isoformat()},
    )

    assert response.status_code == 200
    lines = response.text.splitlines()
    assert lines[0] == (
        "expense_id,claimant_email,vendor,spent_at,category,amount_cents,"
        "currency,property_label,decided_at,reimbursed_via"
    )
    assert "expense_csv_1" in lines[1]
    assert (
        ",Market,2026-04-03T09:30:00Z,supplies,1234,EUR,Villa Export Ledger,"
    ) in lines[1]

    diff = _audit_diff(session_factory, seeded.ctx.workspace_id)
    assert diff["kind"] == "expense-ledger"
    assert diff["row_count"] == 1


def test_expense_ledger_rejects_unknown_status_filter(
    client: TestClient,
    seeded: SeededCsvExport,
) -> None:
    response = client.get(
        _export_url(seeded.ctx, "expense-ledger"),
        params={
            "since": _SINCE.isoformat(),
            "until": _UNTIL.isoformat(),
            "status_filter": "approve",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_expense_status"


def test_empty_window_returns_header_only_and_audits_zero_rows(
    client: TestClient,
    seeded: SeededCsvExport,
    session_factory: sessionmaker[Session],
) -> None:
    response = client.get(
        _export_url(seeded.ctx, "timesheets"),
        params={
            "since": datetime(2026, 5, 1, tzinfo=UTC).isoformat(),
            "until": datetime(2026, 5, 2, tzinfo=UTC).isoformat(),
        },
    )

    assert response.status_code == 200
    assert response.text == (
        "shift_id,user_email,property_label,starts_at_utc,ends_at_utc,"
        "hours_decimal,source,notes\n"
    )
    diff = _audit_diff(session_factory, seeded.ctx.workspace_id)
    assert diff["row_count"] == 0


def test_payroll_export_permission_gate_is_enforced(
    session_factory: sessionmaker[Session],
    seeded: SeededCsvExport,
) -> None:
    worker_ctx = build_workspace_context(
        workspace_id=seeded.ctx.workspace_id,
        workspace_slug=seeded.ctx.workspace_slug,
        actor_id=seeded.worker_id,
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
    )
    app = FastAPI()
    app.include_router(
        build_payroll_router(),
        prefix=f"/w/{seeded.ctx.workspace_slug}/api/v1/payroll",
    )

    def _session() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = lambda: worker_ctx

    with TestClient(app) as worker_client:
        response = worker_client.get(
            _export_url(seeded.ctx, "timesheets"),
            params={"since": _SINCE.isoformat(), "until": _UNTIL.isoformat()},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "error": "permission_denied",
        "action_key": "payroll.export",
    }
