"""HTTP tests for billing rate-card routes."""

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
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import current_workspace_context, db_session
from app.api.errors import add_exception_handlers
from app.api.v1.billing import build_billing_router
from app.tenancy.context import WorkspaceContext
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


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


def _bootstrap_workspace(s: Session) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"rate-api-{workspace_id[-6:].lower()}",
            name="Rate API",
            plan="free",
            quota_json={},
            settings_json={},
            default_currency="EUR",
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _bootstrap_user(
    s: Session,
    *,
    workspace_id: str,
    role: Literal["manager", "worker"],
) -> str:
    user_id = new_ulid()
    email = f"rate-{role}-{user_id[-6:]}@example.com"
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=role,
            created_at=_PINNED,
        )
    )
    s.flush()
    s.add(
        UserWorkspace(
            user_id=user_id,
            workspace_id=workspace_id,
            source="workspace_grant",
            added_at=_PINNED,
        )
    )
    s.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=role,
            scope_kind="workspace",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    s.flush()
    return user_id


def _ctx(
    *, workspace_id: str, actor_id: str, role: Literal["manager", "worker"]
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="rate-api",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _build_app(factory: sessionmaker[Session], ctx: WorkspaceContext) -> FastAPI:
    app = FastAPI()
    add_exception_handlers(app)
    app.include_router(build_billing_router(), prefix="/billing")

    def _override_ctx() -> WorkspaceContext:
        return ctx

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db
    return app


@pytest.fixture
def seeded(factory: sessionmaker[Session]) -> tuple[str, str, str]:
    with factory() as s:
        workspace_id = _bootstrap_workspace(s)
        manager_id = _bootstrap_user(s, workspace_id=workspace_id, role="manager")
        worker_id = _bootstrap_user(s, workspace_id=workspace_id, role="worker")
        s.commit()
    return workspace_id, manager_id, worker_id


def _client(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    actor_id: str,
    role: Literal["manager", "worker"] = "manager",
) -> TestClient:
    return TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=actor_id, role=role),
        ),
        raise_server_exceptions=False,
    )


def _create_org(client: TestClient, *, kind: str = "client") -> dict[str, object]:
    response = client.post(
        "/billing/organizations",
        json={"kind": kind, "display_name": f"{kind.title()} {new_ulid()[-6:]}"},
    )
    assert response.status_code == 201
    return response.json()


def test_create_list_get_and_patch_rate_cards(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, _worker_id = seeded
    client = _client(factory, workspace_id=workspace_id, actor_id=manager_id)
    org = _create_org(client)

    created = client.post(
        f"/billing/organizations/{org['id']}/rate-cards",
        json={
            "label": "Standard",
            "rates": {"maid": 2500},
            "active_from": "2026-01-01",
            "active_to": "2026-02-01",
        },
    )
    assert created.status_code == 201
    rate_card = created.json()
    assert rate_card["currency"] == "EUR"
    assert rate_card["rates"] == {"maid": 2500}

    patched = client.patch(
        f"/billing/organizations/{org['id']}/rate-cards/{rate_card['id']}",
        json={"currency": "usd", "rates": {"maid": 2600, "driver": 4100}},
    )
    assert patched.status_code == 200
    assert patched.json()["currency"] == "USD"
    assert patched.json()["rates"] == {"maid": 2600, "driver": 4100}

    listed = client.get(f"/billing/organizations/{org['id']}/rate-cards")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]] == [rate_card["id"]]

    fetched = client.get(
        f"/billing/organizations/{org['id']}/rate-cards/{rate_card['id']}"
    )
    assert fetched.status_code == 200
    assert fetched.json()["id"] == rate_card["id"]

    open_ended = client.patch(
        f"/billing/organizations/{org['id']}/rate-cards/{rate_card['id']}",
        json={"active_to": None},
    )
    assert open_ended.status_code == 200
    assert open_ended.json()["active_to"] is None


def test_overlap_validation_and_bad_rates_map_to_422(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, _worker_id = seeded
    client = _client(factory, workspace_id=workspace_id, actor_id=manager_id)
    org = _create_org(client)
    base = client.post(
        f"/billing/organizations/{org['id']}/rate-cards",
        json={
            "label": "January",
            "rates": {"maid": 2500},
            "active_from": "2026-01-01",
            "active_to": "2026-02-01",
        },
    )
    assert base.status_code == 201
    adjacent = client.post(
        f"/billing/organizations/{org['id']}/rate-cards",
        json={
            "label": "February",
            "rates": {"maid": 3000},
            "active_from": "2026-02-01",
            "active_to": "2026-03-01",
        },
    )
    assert adjacent.status_code == 201

    overlap = client.post(
        f"/billing/organizations/{org['id']}/rate-cards",
        json={
            "label": "Mid-month",
            "rates": {"maid": 2800},
            "active_from": "2026-01-15",
            "active_to": "2026-02-15",
        },
    )
    assert overlap.status_code == 422
    assert overlap.json()["error"] == "rate_card_invalid"
    assert "overlaps existing window" in overlap.json()["message"]

    bad_rate = client.post(
        f"/billing/organizations/{org['id']}/rate-cards",
        json={
            "label": "Bad",
            "rates": {"maid": 0},
            "active_from": "2026-03-01",
        },
    )
    assert bad_rate.status_code == 422
    assert bad_rate.json()["error"] == "rate_card_invalid"

    bool_rate = client.post(
        f"/billing/organizations/{org['id']}/rate-cards",
        json={
            "label": "Bool",
            "rates": {"maid": True},
            "active_from": "2026-03-01",
        },
    )
    assert bool_rate.status_code == 422

    patch_overlap = client.patch(
        f"/billing/organizations/{org['id']}/rate-cards/{adjacent.json()['id']}",
        json={"active_from": "2026-01-15"},
    )
    assert patch_overlap.status_code == 422
    assert "overlaps existing window" in patch_overlap.json()["message"]

    patch_bool_rate = client.patch(
        f"/billing/organizations/{org['id']}/rate-cards/{adjacent.json()['id']}",
        json={"rates": {"maid": False}},
    )
    assert patch_bool_rate.status_code == 422


def test_vendor_only_and_worker_writes_are_denied(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, worker_id = seeded
    manager = _client(factory, workspace_id=workspace_id, actor_id=manager_id)
    vendor = _create_org(manager, kind="vendor")
    client_org = _create_org(manager, kind="client")
    rate_card = manager.post(
        f"/billing/organizations/{client_org['id']}/rate-cards",
        json={
            "label": "Standard",
            "rates": {"maid": 2500},
            "active_from": "2026-01-01",
        },
    ).json()

    vendor_response = manager.post(
        f"/billing/organizations/{vendor['id']}/rate-cards",
        json={
            "label": "Vendor",
            "rates": {"maid": 2500},
            "active_from": "2026-01-01",
        },
    )
    assert vendor_response.status_code == 422

    worker = _client(
        factory,
        workspace_id=workspace_id,
        actor_id=worker_id,
        role="worker",
    )
    worker_list = worker.get(f"/billing/organizations/{client_org['id']}/rate-cards")
    assert worker_list.status_code == 200
    denied_create = worker.post(
        f"/billing/organizations/{client_org['id']}/rate-cards",
        json={
            "label": "Nope",
            "rates": {"maid": 2500},
            "active_from": "2026-02-01",
        },
    )
    assert denied_create.status_code == 403
    denied_patch = worker.patch(
        f"/billing/organizations/{client_org['id']}/rate-cards/{rate_card['id']}",
        json={"label": "Nope"},
    )
    assert denied_patch.status_code == 403


def test_openapi_operation_ids_include_rate_cards(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, _worker_id = seeded
    app = _build_app(
        factory,
        _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
    )
    operations = {
        operation["operationId"]
        for path_item in app.openapi()["paths"].values()
        for operation in path_item.values()
    }

    assert {
        "billing.rate_cards.list",
        "billing.rate_cards.create",
        "billing.rate_cards.get",
        "billing.rate_cards.update",
    } <= operations
