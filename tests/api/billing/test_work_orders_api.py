"""HTTP tests for billing work-order routes."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.billing.models import Organization, RateCard
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import current_workspace_context, db_session
from app.api.errors import add_exception_handlers
from app.api.v1.billing import build_billing_router
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
_STARTS = datetime(2026, 4, 29, 8, 0, 0, tzinfo=UTC)


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


def _bootstrap(
    s: Session, *, grant_role: Literal["manager", "worker"] = "manager"
) -> tuple[str, str, str, str, str]:
    workspace_id = new_ulid()
    manager_id = new_ulid()
    org_id = new_ulid()
    property_id = new_ulid()
    rate_card_id = new_ulid()
    email = f"manager-{manager_id[-6:]}@example.com"
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"wo-api-{workspace_id[-6:].lower()}",
            name="Work Orders API",
            plan="free",
            quota_json={},
            settings_json={},
            default_currency="EUR",
            created_at=_PINNED,
        )
    )
    s.flush()
    s.add(
        User(
            id=manager_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name="manager",
            created_at=_PINNED,
        )
    )
    s.flush()
    s.add(
        UserWorkspace(
            user_id=manager_id,
            workspace_id=workspace_id,
            source="workspace_grant",
            added_at=_PINNED,
        )
    )
    s.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=manager_id,
            grant_role=grant_role,
            scope_kind="workspace",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    s.add(
        Organization(
            id=org_id,
            workspace_id=workspace_id,
            kind="client",
            display_name="Dupont Family",
            billing_address={},
            tax_id=None,
            default_currency="EUR",
            contact_email=None,
            contact_phone=None,
            notes_md=None,
            created_at=_PINNED,
        )
    )
    s.add(
        Property(
            id=property_id,
            name="Billing Villa",
            kind="vacation",
            address="1 Billing Way",
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
            property_notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
            deleted_at=None,
        )
    )
    s.add(
        PropertyWorkspace(
            property_id=property_id,
            workspace_id=workspace_id,
            label="Billing Villa",
            membership_role="owner_workspace",
            share_guest_identity=False,
            status="active",
            created_at=_PINNED,
        )
    )
    s.add(
        RateCard(
            id=rate_card_id,
            workspace_id=workspace_id,
            organization_id=org_id,
            label="Hourly",
            currency="EUR",
            rates_json={"hourly": 4000},
            active_from=_STARTS.date(),
            active_to=None,
        )
    )
    s.flush()
    return workspace_id, manager_id, org_id, property_id, rate_card_id


def _ctx(
    workspace_id: str,
    manager_id: str,
    *,
    role: Literal["manager", "worker"] = "manager",
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="work-orders-api",
        actor_id=manager_id,
        actor_kind="user",
        actor_grant_role=role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _build_app(factory: sessionmaker[Session], ctx: WorkspaceContext) -> FastAPI:
    app = FastAPI()
    add_exception_handlers(app)
    app.include_router(build_billing_router(), prefix="/billing")

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    app.dependency_overrides[current_workspace_context] = lambda: ctx
    app.dependency_overrides[db_session] = _override_db
    return app


def test_work_order_crud_and_transitions(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        workspace_id, manager_id, org_id, property_id, rate_card_id = _bootstrap(s)
        s.commit()
    client = TestClient(
        _build_app(factory, _ctx(workspace_id, manager_id)),
        raise_server_exceptions=False,
    )

    created = client.post(
        "/billing/work-orders",
        json={
            "organization_id": org_id,
            "property_id": property_id,
            "title": "Replace pump seal",
            "starts_at": _STARTS.isoformat(),
            "rate_card_id": rate_card_id,
        },
    )
    assert created.status_code == 201
    work_order = created.json()
    assert work_order["status"] == "draft"
    assert work_order["total_hours_decimal"] == "0.00"

    patched = client.patch(
        f"/billing/work-orders/{work_order['id']}",
        json={"title": "Replace pool pump seal"},
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Replace pool pump seal"

    bad_complete = client.post(f"/billing/work-orders/{work_order['id']}/complete")
    assert bad_complete.status_code == 422
    assert bad_complete.json()["error"] == "work_order_invalid"

    in_progress = client.post(f"/billing/work-orders/{work_order['id']}/in-progress")
    assert in_progress.status_code == 200
    assert in_progress.json()["status"] == "in_progress"

    completed = client.post(f"/billing/work-orders/{work_order['id']}/complete")
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"

    invoiced = client.post(f"/billing/work-orders/{work_order['id']}/invoice")
    assert invoiced.status_code == 200
    assert invoiced.json()["status"] == "invoiced"

    listed = client.get("/billing/work-orders", params={"organization_id": org_id})
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]] == [work_order["id"]]


def test_worker_cannot_create_work_order(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        workspace_id, manager_id, org_id, property_id, rate_card_id = _bootstrap(
            s,
            grant_role="worker",
        )
        s.commit()
    client = TestClient(
        _build_app(factory, _ctx(workspace_id, manager_id, role="worker")),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/billing/work-orders",
        json={
            "organization_id": org_id,
            "property_id": property_id,
            "title": "Worker attempt",
            "starts_at": _STARTS.isoformat(),
            "rate_card_id": rate_card_id,
        },
    )

    assert response.status_code == 403
