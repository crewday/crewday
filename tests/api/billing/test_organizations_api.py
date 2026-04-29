"""HTTP tests for billing organization routes."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.billing.models import RateCard
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import current_workspace_context, db_session
from app.api.v1.billing import build_billing_router
from app.tenancy.context import WorkspaceContext
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
_TODAY = date(2026, 4, 29)


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
            slug=f"billing-{workspace_id[-6:].lower()}",
            name="Billing API",
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
    email = f"{role}-{user_id[-6:]}@example.com"
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
        workspace_slug="billing",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _build_app(factory: sessionmaker[Session], ctx: WorkspaceContext) -> FastAPI:
    app = FastAPI()
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


def test_create_list_get_patch_archive_and_no_delete(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, _worker_id = seeded
    client = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
        ),
        raise_server_exceptions=False,
    )

    created = client.post(
        "/billing/organizations",
        json={
            "kind": "client",
            "display_name": "Dupont Family",
            "billing_address": {"country": "FR"},
        },
    )
    assert created.status_code == 201
    org = created.json()
    assert org["default_currency"] == "EUR"
    assert org["archived_at"] is None

    patched = client.patch(
        f"/billing/organizations/{org['id']}",
        json={"display_name": "Dupont Household", "default_currency": "gbp"},
    )
    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Dupont Household"
    assert patched.json()["default_currency"] == "GBP"

    listed = client.get(
        "/billing/organizations",
        params={"kind": "client", "q": "house"},
    )
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]] == [org["id"]]

    fetched = client.get(f"/billing/organizations/{org['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == org["id"]

    archived = client.post(f"/billing/organizations/{org['id']}/archive")
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None

    hidden = client.get(f"/billing/organizations/{org['id']}")
    assert hidden.status_code == 404
    fetched_archived = client.get(
        f"/billing/organizations/{org['id']}",
        params={"include_archived": True},
    )
    assert fetched_archived.status_code == 200
    assert fetched_archived.json()["id"] == org["id"]

    patch_archived = client.patch(
        f"/billing/organizations/{org['id']}",
        json={"display_name": "Archived Rename"},
    )
    assert patch_archived.status_code == 404

    assert client.get("/billing/organizations").json()["data"] == []
    with_archived = client.get(
        "/billing/organizations",
        params={"include_archived": True},
    )
    assert [row["id"] for row in with_archived.json()["data"]] == [org["id"]]

    assert client.delete(f"/billing/organizations/{org['id']}").status_code == 405


def test_duplicate_names_return_422(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, _worker_id = seeded
    client = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
        ),
        raise_server_exceptions=False,
    )
    first = client.post(
        "/billing/organizations",
        json={"kind": "client", "display_name": "Dupont Family"},
    )
    assert first.status_code == 201

    duplicate = client.post(
        "/billing/organizations",
        json={"kind": "vendor", "display_name": " Dupont Family "},
    )

    assert duplicate.status_code == 422
    assert duplicate.json()["detail"]["error"] == "organization_invalid"
    assert "already exists" in duplicate.json()["detail"]["message"]
    listed = client.get("/billing/organizations")
    assert [row["display_name"] for row in listed.json()["data"]] == ["Dupont Family"]


def test_patch_duplicate_name_returns_422(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, _worker_id = seeded
    client = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
        ),
        raise_server_exceptions=False,
    )
    first = client.post(
        "/billing/organizations",
        json={"kind": "client", "display_name": "Alpha Family"},
    ).json()
    second = client.post(
        "/billing/organizations",
        json={"kind": "vendor", "display_name": "Beta Co"},
    ).json()

    duplicate = client.patch(
        f"/billing/organizations/{second['id']}",
        json={"display_name": first["display_name"]},
    )

    assert duplicate.status_code == 422
    assert duplicate.json()["detail"]["error"] == "organization_invalid"
    assert "already exists" in duplicate.json()["detail"]["message"]


def test_kind_transition_conflict_maps_to_422(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, _worker_id = seeded
    client = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
        ),
        raise_server_exceptions=False,
    )
    org = client.post(
        "/billing/organizations",
        json={"kind": "mixed", "display_name": "CleanCo"},
    ).json()
    with factory() as s:
        s.add(
            RateCard(
                id=new_ulid(),
                workspace_id=workspace_id,
                organization_id=org["id"],
                label="Standard",
                currency="EUR",
                rates_json={"maid": 2500},
                active_from=_TODAY,
            )
        )
        s.commit()

    resp = client.patch(
        f"/billing/organizations/{org['id']}",
        json={"kind": "vendor"},
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "organization_invalid"
    assert "cannot become vendor" in resp.json()["detail"]["message"]


def test_worker_can_view_but_cannot_mutate(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, worker_id = seeded
    manager = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
        ),
        raise_server_exceptions=False,
    )
    org = manager.post(
        "/billing/organizations",
        json={"kind": "client", "display_name": "Visible Client"},
    ).json()
    worker = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=worker_id, role="worker"),
        ),
        raise_server_exceptions=False,
    )

    assert worker.get("/billing/organizations").status_code == 200
    assert worker.get(f"/billing/organizations/{org['id']}").status_code == 200
    denied = worker.patch(
        f"/billing/organizations/{org['id']}",
        json={"display_name": "Nope"},
    )
    assert denied.status_code == 403


def test_openapi_operation_ids_are_stable(
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
        "billing.organizations.list",
        "billing.organizations.create",
        "billing.organizations.get",
        "billing.organizations.update",
        "billing.organizations.archive",
    } <= operations
