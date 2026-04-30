"""Fixtures for client portal API tests."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.billing.models import (
    Organization,
    Quote,
    VendorInvoice,
    WorkOrder,
    WorkOrderShiftAccrual,
)
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.client.portal import build_client_portal_router
from app.api.deps import current_workspace_context, db_session
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid

PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


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
def api_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(api_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=api_engine, expire_on_commit=False, class_=Session)


def ctx(
    workspace_id: str,
    user_id: str,
    *,
    role: Literal["manager", "worker", "client", "guest"] = "client",
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="client-api",
        actor_id=user_id,
        actor_kind="user",
        actor_grant_role=role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def build_app(factory: sessionmaker[Session], context: WorkspaceContext) -> FastAPI:
    app = FastAPI()
    app.include_router(build_client_portal_router())

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as session:
            assert isinstance(session, Session)
            yield session

    app.dependency_overrides[current_workspace_context] = lambda: context
    app.dependency_overrides[db_session] = _override_db
    return app


def seed_user(
    session: Session,
    *,
    workspace_id: str,
    role: Literal["manager", "worker", "client"] = "client",
    binding_org_id: str | None = None,
    scope_property_id: str | None = None,
) -> str:
    user_id = new_ulid()
    email = f"{role}-{user_id[-8:].lower()}@example.com"
    session.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=f"{role.title()} User",
            created_at=PINNED,
        )
    )
    session.add(
        UserWorkspace(
            user_id=user_id,
            workspace_id=workspace_id,
            source="workspace_grant",
            added_at=PINNED,
        )
    )
    session.flush()
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=role,
            scope_kind="workspace",
            scope_property_id=scope_property_id,
            binding_org_id=binding_org_id,
            created_at=PINNED,
            created_by_user_id=None,
        )
    )
    return user_id


def seed_dataset(session: Session) -> dict[str, str]:
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=f"client-{workspace_id[-6:].lower()}",
            name="Client Portal API",
            plan="free",
            quota_json={},
            settings_json={},
            default_currency="EUR",
            created_at=PINNED,
        )
    )
    session.flush()

    org_a = new_ulid()
    org_b = new_ulid()
    for org_id, name in ((org_a, "A Client"), (org_b, "B Client")):
        session.add(
            Organization(
                id=org_id,
                workspace_id=workspace_id,
                kind="client",
                display_name=name,
                billing_address={},
                tax_id=None,
                default_currency="EUR",
                contact_email=None,
                contact_phone=None,
                notes_md="internal note",
                created_at=PINNED,
                archived_at=None,
            )
        )
    session.flush()

    prop_a = new_ulid()
    prop_b = new_ulid()
    for prop_id, org_id, name in (
        (prop_a, org_a, "Alpha Villa"),
        (prop_b, org_b, "Beta Villa"),
    ):
        session.add(
            Property(
                id=prop_id,
                name=name,
                kind="vacation",
                address=f"{name} Road",
                address_json={"country": "FR"},
                country="FR",
                timezone="Europe/Paris",
                lat=None,
                lon=None,
                default_currency="EUR",
                client_org_id=org_id,
                owner_user_id=None,
                tags_json=[],
                welcome_defaults_json={},
                property_notes_md="staff-only note",
                created_at=PINNED,
                updated_at=PINNED,
                deleted_at=None,
            )
        )
        session.add(
            PropertyWorkspace(
                property_id=prop_id,
                workspace_id=workspace_id,
                label=name,
                membership_role="owner_workspace",
                share_guest_identity=False,
                status="active",
                created_at=PINNED,
            )
        )
    session.flush()

    work_order_a = new_ulid()
    work_order_b = new_ulid()
    for work_order_id, org_id, prop_id, total_hours, total_cents in (
        (work_order_a, org_a, prop_a, Decimal("3.50"), 35000),
        (work_order_b, org_b, prop_b, Decimal("1.00"), 10000),
    ):
        session.add(
            WorkOrder(
                id=work_order_id,
                workspace_id=workspace_id,
                organization_id=org_id,
                property_id=prop_id,
                title="Client work",
                status="in_progress",
                starts_at=PINNED,
                ends_at=None,
                rate_card_id=None,
                total_hours_decimal=total_hours,
                total_cents=total_cents,
            )
        )
    session.flush()

    session.add_all(
        [
            WorkOrderShiftAccrual(
                id=new_ulid(),
                workspace_id=workspace_id,
                work_order_id=work_order_a,
                shift_id=new_ulid(),
                hours_decimal=Decimal("2.00"),
                hourly_rate_cents=10000,
                accrued_cents=20000,
                created_at=PINNED,
            ),
            WorkOrderShiftAccrual(
                id=new_ulid(),
                workspace_id=workspace_id,
                work_order_id=work_order_a,
                shift_id=new_ulid(),
                hours_decimal=Decimal("1.50"),
                hourly_rate_cents=10000,
                accrued_cents=15000,
                created_at=PINNED,
            ),
            WorkOrderShiftAccrual(
                id=new_ulid(),
                workspace_id=workspace_id,
                work_order_id=work_order_b,
                shift_id=new_ulid(),
                hours_decimal=Decimal("1.00"),
                hourly_rate_cents=10000,
                accrued_cents=10000,
                created_at=PINNED,
            ),
        ]
    )

    quote_a = new_ulid()
    quote_b = new_ulid()
    for quote_id, org_id, prop_id in (
        (quote_a, org_a, prop_a),
        (quote_b, org_b, prop_b),
    ):
        session.add(
            Quote(
                id=quote_id,
                workspace_id=workspace_id,
                organization_id=org_id,
                property_id=prop_id,
                title="Repair quote",
                body_md="internal cost: 20 EUR",
                total_cents=42000,
                currency="EUR",
                status="sent",
                sent_at=PINNED,
                decided_at=None,
            )
        )

    invoice_a = new_ulid()
    invoice_b = new_ulid()
    for invoice_id, org_id, number in (
        (invoice_a, org_a, "A-001"),
        (invoice_b, org_b, "B-001"),
    ):
        session.add(
            VendorInvoice(
                id=invoice_id,
                workspace_id=workspace_id,
                vendor_org_id=org_id,
                invoice_number=number,
                issued_at=date(2026, 4, 29),
                due_at=date(2026, 5, 29),
                total_cents=42000,
                currency="EUR",
                status="approved",
                pdf_blob_hash="secret-storage-hash",
                notes_md="supplier internal note",
            )
        )

    client_a = seed_user(session, workspace_id=workspace_id, binding_org_id=org_a)
    client_b = seed_user(session, workspace_id=workspace_id, binding_org_id=org_b)
    manager = seed_user(session, workspace_id=workspace_id, role="manager")
    property_client = seed_user(
        session,
        workspace_id=workspace_id,
        scope_property_id=prop_a,
    )
    session.flush()
    return {
        "workspace_id": workspace_id,
        "org_a": org_a,
        "org_b": org_b,
        "prop_a": prop_a,
        "prop_b": prop_b,
        "work_order_a": work_order_a,
        "work_order_b": work_order_b,
        "quote_a": quote_a,
        "quote_b": quote_b,
        "invoice_a": invoice_a,
        "invoice_b": invoice_b,
        "client_a": client_a,
        "client_b": client_b,
        "manager": manager,
        "property_client": property_client,
    }
