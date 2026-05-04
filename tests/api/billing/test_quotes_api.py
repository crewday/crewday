"""HTTP tests for billing quote routes."""

from __future__ import annotations

import importlib
import pkgutil
import re
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.billing.models import Organization
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.billing.quotes import build_quotes_public_router
from app.api.deps import current_workspace_context, db_session
from app.api.v1.billing import build_billing_router
from app.config import Settings, get_settings
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests._fakes.mailer import InMemoryMailer

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
_LINES_JSON = {
    "schema_version": 1,
    "lines": [
        {
            "kind": "labor",
            "description": "Diagnosis and repair",
            "quantity": 3,
            "unit": "hour",
            "unit_price_cents": 6000,
            "total_cents": 18000,
        },
        {
            "kind": "travel",
            "description": "Call-out fee",
            "quantity": 1,
            "unit": "unit",
            "unit_price_cents": 3500,
            "total_cents": 3500,
        },
    ],
}


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


@pytest.fixture
def mailer() -> InMemoryMailer:
    return InMemoryMailer()


def _settings() -> Settings:
    return Settings(
        database_url="sqlite:///:memory:",
        public_url="https://crew.day",
        root_key=SecretStr("test-root-key-for-quote-api"),
    )


def _bootstrap(s: Session) -> tuple[str, str, str, str]:
    workspace_id = new_ulid()
    manager_id = new_ulid()
    org_id = new_ulid()
    property_id = new_ulid()
    email = f"manager-{manager_id[-6:]}@example.com"
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
            grant_role="manager",
            scope_kind="workspace",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    s.flush()
    s.add(
        Organization(
            id=org_id,
            workspace_id=workspace_id,
            kind="client",
            display_name="Dupont Family",
            billing_address={},
            tax_id=None,
            default_currency="EUR",
            contact_email="client@example.com",
            contact_phone=None,
            notes_md=None,
            created_at=_PINNED,
        )
    )
    s.flush()
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
    s.flush()
    return workspace_id, manager_id, org_id, property_id


def _ctx(workspace_id: str, manager_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="billing",
        actor_id=manager_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _db_override(factory: sessionmaker[Session]) -> Iterator[Session]:
    uow = UnitOfWorkImpl(session_factory=factory)
    with uow as s:
        assert isinstance(s, Session)
        yield s


def _authed_app(
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
    mailer: InMemoryMailer,
) -> FastAPI:
    app = FastAPI()
    app.state.mailer = mailer
    app.include_router(build_billing_router(), prefix="/billing")

    def _override_db() -> Iterator[Session]:
        yield from _db_override(factory)

    app.dependency_overrides[current_workspace_context] = lambda: ctx
    app.dependency_overrides[db_session] = _override_db
    app.dependency_overrides[get_settings] = _settings
    return app


def _public_app(factory: sessionmaker[Session]) -> FastAPI:
    app = FastAPI()
    app.include_router(build_quotes_public_router())

    def _override_db() -> Iterator[Session]:
        yield from _db_override(factory)

    app.dependency_overrides[db_session] = _override_db
    app.dependency_overrides[get_settings] = _settings
    return app


def _extract_token(body_text: str) -> str:
    match = re.search(
        r"/q/[A-Z0-9]+\?token=([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)",
        body_text,
    )
    assert match is not None
    return match.group(1)


def test_quote_create_send_lock_and_public_accept_without_session(
    factory: sessionmaker[Session],
    mailer: InMemoryMailer,
) -> None:
    with factory() as s:
        workspace_id, manager_id, org_id, property_id = _bootstrap(s)
        s.commit()
    authed = TestClient(
        _authed_app(factory, _ctx(workspace_id, manager_id), mailer),
        raise_server_exceptions=False,
    )
    created = authed.post(
        "/billing/quotes",
        json={
            "organization_id": org_id,
            "property_id": property_id,
            "title": "Pool repair quote",
            "body_md": "Parts and labour.",
            "lines_json": _LINES_JSON,
            "subtotal_cents": 21500,
            "tax_cents": 500,
            "total_cents": 22000,
        },
    )
    assert created.status_code == 201
    created_body = created.json()
    quote_id = created_body["id"]
    assert created_body["lines_json"] == _LINES_JSON
    assert created_body["subtotal_cents"] == 21500
    assert created_body["tax_cents"] == 500
    assert created_body["total_cents"] == 22000
    assert created_body["superseded_by_quote_id"] is None

    sent = authed.post(f"/billing/quotes/{quote_id}/send", json={})
    assert sent.status_code == 200
    assert sent.json()["status"] == "sent"
    assert len(mailer.sent) == 1

    locked = authed.patch(f"/billing/quotes/{quote_id}", json={"title": "New title"})
    assert locked.status_code == 422
    assert "supersede" in locked.json()["detail"]["message"]

    token = _extract_token(mailer.sent[0].body_text)
    public = TestClient(_public_app(factory), raise_server_exceptions=False)
    opened = public.get(f"/q/{quote_id}", params={"token": token})
    assert opened.status_code == 200
    assert opened.json()["status"] == "sent"

    accepted = public.post(f"/q/{quote_id}/accept", params={"token": token})
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"


def test_quote_api_rejects_line_total_mismatch_and_exposes_supersession(
    factory: sessionmaker[Session],
    mailer: InMemoryMailer,
) -> None:
    with factory() as s:
        workspace_id, manager_id, org_id, property_id = _bootstrap(s)
        s.commit()
    authed = TestClient(
        _authed_app(factory, _ctx(workspace_id, manager_id), mailer),
        raise_server_exceptions=False,
    )
    mismatch = {
        **_LINES_JSON,
        "lines": [{**_LINES_JSON["lines"][0], "total_cents": 17999}],
    }
    rejected = authed.post(
        "/billing/quotes",
        json={
            "organization_id": org_id,
            "property_id": property_id,
            "title": "Bad quote",
            "lines_json": mismatch,
            "total_cents": 18000,
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["error"] == "quote_invalid"

    non_finite = {
        **_LINES_JSON,
        "lines": [{**_LINES_JSON["lines"][0], "quantity": "NaN"}],
    }
    rejected_non_finite = authed.post(
        "/billing/quotes",
        json={
            "organization_id": org_id,
            "property_id": property_id,
            "title": "Bad quote",
            "lines_json": non_finite,
            "total_cents": 18000,
        },
    )
    assert rejected_non_finite.status_code == 422
    assert rejected_non_finite.json()["detail"]["error"] == "quote_invalid"

    created = authed.post(
        "/billing/quotes",
        json={
            "organization_id": org_id,
            "property_id": property_id,
            "title": "Pool repair quote",
            "total_cents": 12500,
        },
    )
    assert created.status_code == 201
    quote_id = created.json()["id"]
    authed.post(f"/billing/quotes/{quote_id}/send", json={})

    clone = authed.post(
        f"/billing/quotes/{quote_id}/supersede",
        json={"lines_json": _LINES_JSON, "subtotal_cents": 21500, "total_cents": 21500},
    )
    assert clone.status_code == 201
    clone_body = clone.json()
    assert clone_body["status"] == "draft"
    assert clone_body["lines_json"] == _LINES_JSON

    original = authed.get(f"/billing/quotes/{quote_id}")
    assert original.status_code == 200
    assert original.json()["status"] == "expired"
    assert original.json()["superseded_by_quote_id"] == clone_body["id"]


def test_public_quote_rejects_tampered_token(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        workspace_id, manager_id, org_id, property_id = _bootstrap(s)
        s.commit()
    mailer = InMemoryMailer()
    authed = TestClient(
        _authed_app(factory, _ctx(workspace_id, manager_id), mailer),
        raise_server_exceptions=False,
    )
    quote_id = authed.post(
        "/billing/quotes",
        json={
            "organization_id": org_id,
            "property_id": property_id,
            "title": "Pool repair quote",
            "total_cents": 12500,
        },
    ).json()["id"]
    authed.post(f"/billing/quotes/{quote_id}/send", json={})
    token = _extract_token(mailer.sent[0].body_text)

    public = TestClient(_public_app(factory), raise_server_exceptions=False)
    resp = public.post(f"/q/{quote_id}/accept", params={"token": token[:-1] + "x"})

    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "quote_token_invalid"


def test_quote_decision_route_rejects_sent_quote(
    factory: sessionmaker[Session],
    mailer: InMemoryMailer,
) -> None:
    with factory() as s:
        workspace_id, manager_id, org_id, property_id = _bootstrap(s)
        s.commit()
    authed = TestClient(
        _authed_app(factory, _ctx(workspace_id, manager_id), mailer),
        raise_server_exceptions=False,
    )
    quote_id = authed.post(
        "/billing/quotes",
        json={
            "organization_id": org_id,
            "property_id": property_id,
            "title": "Pool repair quote",
            "total_cents": 12500,
        },
    ).json()["id"]
    authed.post(f"/billing/quotes/{quote_id}/send", json={})

    rejected = authed.post(
        f"/billing/quotes/{quote_id}/decision",
        json={"decision": "rejected", "decision_note_md": "Too expensive"},
    )

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
