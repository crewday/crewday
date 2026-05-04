"""Integration coverage for workspace issue reporting routes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.api.deps import current_workspace_context, db_session
from app.api.errors import _handle_domain_error
from app.api.v1.issues import router as issues_router
from app.domain.errors import DomainError
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

_NOW = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


def _ctx(
    workspace_id: str,
    actor_id: str,
    *,
    slug: str,
    role: ActorGrantRole = "manager",
    owner: bool = False,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=role,
        actor_was_owner_member=owner,
        audit_correlation_id="corr_issues_api",
    )


def _client(session: Session, ctx: WorkspaceContext) -> TestClient:
    app = FastAPI()
    app.include_router(issues_router, prefix="/issues")

    async def _on_domain_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DomainError)
        return _handle_domain_error(request, exc)

    app.add_exception_handler(DomainError, _on_domain_error)

    def override_ctx() -> WorkspaceContext:
        return ctx

    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[current_workspace_context] = override_ctx
    app.dependency_overrides[db_session] = override_db
    return TestClient(app)


def _seed_property(
    session: Session, *, workspace_id: str, property_id: str, label: str
) -> None:
    session.add(
        Property(
            id=property_id,
            name=label,
            kind="residence",
            address=f"{label} Road",
            address_json={"line1": f"{label} Road", "country": "US"},
            country="US",
            timezone="UTC",
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
            workspace_id=workspace_id,
            label=label,
            membership_role="owner_workspace",
            share_guest_identity=False,
            status="active",
            created_at=_NOW,
        )
    )
    session.flush()


def _seed_workspace(
    session: Session,
) -> tuple[WorkspaceContext, WorkspaceContext, str, str]:
    owner = bootstrap_user(
        session,
        email="issues-api-manager@example.com",
        display_name="Issue API Manager",
    )
    worker = bootstrap_user(
        session,
        email="issues-api-worker@example.com",
        display_name="Issue API Worker",
    )
    workspace = bootstrap_workspace(
        session,
        slug="issues-api",
        name="Issues API",
        owner_user_id=owner.id,
    )
    visible = "prop_issues_api_visible"
    hidden = "prop_issues_api_hidden"
    _seed_property(
        session,
        workspace_id=workspace.id,
        property_id=visible,
        label="Visible Issue Villa",
    )
    _seed_property(
        session,
        workspace_id=workspace.id,
        property_id=hidden,
        label="Hidden Issue Villa",
    )
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace.id,
            user_id=worker.id,
            grant_role="worker",
            scope_kind="workspace",
            scope_property_id=visible,
            created_at=_NOW,
            created_by_user_id=owner.id,
        )
    )
    session.flush()
    manager_ctx = _ctx(workspace.id, owner.id, slug=workspace.slug, owner=True)
    worker_ctx = _ctx(workspace.id, worker.id, slug=workspace.slug, role="worker")
    return manager_ctx, worker_ctx, visible, hidden


def test_worker_creates_issue_and_manager_lists_open_issue(db_session: Session) -> None:
    manager_ctx, worker_ctx, visible, _hidden = _seed_workspace(db_session)
    worker_client = _client(db_session, worker_ctx)

    created = worker_client.post(
        "/issues",
        json={
            "title": "Pool gate loose",
            "severity": "urgent",
            "category": "safety",
            "property_id": visible,
            "area": "Pool",
            "body": "Latch is not catching",
        },
    )

    assert created.status_code == 201
    body = created.json()
    assert body["title"] == "Pool gate loose"
    assert body["status"] == "open"
    assert body["reported_by"] == worker_ctx.actor_id

    manager_client = _client(db_session, manager_ctx)
    listed = manager_client.get("/issues", params={"state": "open"})

    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]] == [body["id"]]


def test_worker_lists_only_own_issues(db_session: Session) -> None:
    manager_ctx, worker_ctx, visible, _hidden = _seed_workspace(db_session)
    manager_client = _client(db_session, manager_ctx)
    worker_client = _client(db_session, worker_ctx)
    manager_created = manager_client.post(
        "/issues",
        json={
            "title": "Manager spotted cracked tile",
            "category": "damage",
            "property_id": visible,
        },
    )
    worker_created = worker_client.post(
        "/issues",
        json={
            "title": "Worker spotted missing towels",
            "category": "supplies",
            "property_id": visible,
        },
    )

    assert manager_created.status_code == 201
    assert worker_created.status_code == 201
    listed = worker_client.get("/issues")

    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]] == [worker_created.json()["id"]]


def test_worker_cannot_create_issue_for_hidden_property(db_session: Session) -> None:
    _manager_ctx, worker_ctx, _visible, hidden = _seed_workspace(db_session)
    client = _client(db_session, worker_ctx)

    response = client.post(
        "/issues",
        json={
            "title": "Hidden problem",
            "category": "other",
            "property_id": hidden,
        },
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["error"] == "not_visible"
    assert body["field"] == "property_id"


def test_invalid_severity_and_category_are_422(db_session: Session) -> None:
    _manager_ctx, worker_ctx, visible, _hidden = _seed_workspace(db_session)
    client = _client(db_session, worker_ctx)

    severity = client.post(
        "/issues",
        json={
            "title": "Bad severity",
            "severity": "critical",
            "category": "other",
            "property_id": visible,
        },
    )
    category = client.post(
        "/issues",
        json={
            "title": "Bad category",
            "severity": "normal",
            "category": "noise",
            "property_id": visible,
        },
    )

    assert severity.status_code == 422
    assert category.status_code == 422


def test_openapi_mounts_issue_paths_under_workspace_prefix(
    db_session: Session,
) -> None:
    manager_ctx, _worker_ctx, _visible, _hidden = _seed_workspace(db_session)
    app = FastAPI()
    app.include_router(issues_router, prefix="/w/{slug}/api/v1/issues")

    def override_ctx() -> WorkspaceContext:
        return manager_ctx

    def override_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[current_workspace_context] = override_ctx
    app.dependency_overrides[db_session] = override_db
    schema = TestClient(app).get("/openapi.json").json()

    assert "/w/{slug}/api/v1/issues" in schema["paths"]
    assert "/w/{slug}/api/v1/issues/{issue_id}" in schema["paths"]
