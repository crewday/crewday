"""HTTP tests for the workspace audit feed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.api.v1.audit import build_workspace_audit_router
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client, ctx_for

pytest_plugins = ("tests.unit.api.v1.identity.conftest",)

PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)
AFTER_BOOTSTRAP = PINNED + timedelta(days=1)


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", build_workspace_audit_router())], factory, ctx)


def _seed_audit(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    action: str,
    actor_id: str = "actor_1",
    actor_kind: str = "user",
    actor_grant_role: str = "manager",
    entity_kind: str = "task",
    entity_id: str | None = None,
    created_at: datetime = PINNED,
    scope_kind: str = "workspace",
) -> str:
    row_id = new_ulid()
    with factory() as session, tenant_agnostic():
        session.add(
            AuditLog(
                id=row_id,
                workspace_id=workspace_id if scope_kind == "workspace" else None,
                actor_id=actor_id,
                actor_kind=actor_kind,
                actor_grant_role=actor_grant_role,
                actor_was_owner_member=False,
                entity_kind=entity_kind,
                entity_id=entity_id or new_ulid(),
                action=action,
                diff={"reason": "because"},
                correlation_id=new_ulid(),
                scope_kind=scope_kind,
                via="web",
                created_at=created_at,
            )
        )
        session.commit()
    return row_id


def test_lists_workspace_rows_newest_first(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    _seed_audit(
        factory,
        workspace_id=workspace_id,
        action="task.created",
        created_at=AFTER_BOOTSTRAP,
    )
    _seed_audit(
        factory,
        workspace_id=workspace_id,
        action="task.completed",
        created_at=AFTER_BOOTSTRAP + timedelta(minutes=1),
    )
    _seed_audit(
        factory,
        workspace_id=workspace_id,
        action="admin.granted",
        created_at=AFTER_BOOTSTRAP + timedelta(minutes=2),
        scope_kind="deployment",
    )

    response = _client(ctx, factory).get("/audit")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["action"] for row in body["data"][:2]] == [
        "task.completed",
        "task.created",
    ]
    assert [row["target"] for row in body["data"]] == [
        body["data"][0]["entity_kind"] + ":" + body["data"][0]["entity_id"],
        body["data"][1]["entity_kind"] + ":" + body["data"][1]["entity_id"],
        body["data"][2]["entity_kind"] + ":" + body["data"][2]["entity_id"],
    ]
    assert {row["correlation_id"] for row in body["data"]}


def test_filters_by_actor_action_entity_and_time(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    wanted_entity = new_ulid()
    _seed_audit(
        factory,
        workspace_id=workspace_id,
        action="asset.updated",
        actor_id="actor_wanted",
        entity_kind="asset",
        entity_id=wanted_entity,
        created_at=PINNED,
    )
    _seed_audit(
        factory,
        workspace_id=workspace_id,
        action="asset.updated",
        actor_id="actor_other",
        entity_kind="asset",
        created_at=PINNED,
    )
    _seed_audit(
        factory,
        workspace_id=workspace_id,
        action="asset.updated",
        actor_id="actor_wanted",
        entity_kind="asset",
        entity_id=wanted_entity,
        created_at=PINNED - timedelta(days=1),
    )
    _seed_audit(
        factory,
        workspace_id=workspace_id,
        action="asset.updated",
        actor_id="actor_wanted",
        entity_kind="asset",
        entity_id=wanted_entity,
        created_at=PINNED + timedelta(days=1),
    )

    response = _client(ctx, factory).get(
        "/audit",
        params={
            "actor": "actor_wanted",
            "action": "asset.updated",
            "entity": f"asset:{wanted_entity}",
            "since": PINNED.isoformat(),
            "until": PINNED.isoformat(),
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["entity_id"] for row in body["data"]] == [wanted_entity]
    assert body["data"][0]["target"] == f"asset:{wanted_entity}"


def test_cursor_walks_to_next_page(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    _seed_audit(
        factory,
        workspace_id=workspace_id,
        action="newest",
        created_at=AFTER_BOOTSTRAP + timedelta(minutes=2),
    )
    _seed_audit(
        factory,
        workspace_id=workspace_id,
        action="middle",
        created_at=AFTER_BOOTSTRAP + timedelta(minutes=1),
    )
    _seed_audit(
        factory,
        workspace_id=workspace_id,
        action="oldest",
        created_at=AFTER_BOOTSTRAP,
    )

    client = _client(ctx, factory)
    page1 = client.get("/audit", params={"limit": 2}).json()
    page2 = client.get(
        "/audit",
        params={"limit": 2, "cursor": page1["next_cursor"]},
    ).json()

    assert [row["action"] for row in page1["data"]] == ["newest", "middle"]
    assert page1["has_more"] is True
    assert next(row["action"] for row in page2["data"]) == "oldest"
    assert page2["has_more"] is False


def test_worker_without_audit_log_view_is_denied(
    worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
) -> None:
    ctx, factory, workspace_id, _worker_id = worker_ctx
    _seed_audit(factory, workspace_id=workspace_id, action="task.created")

    response = _client(ctx, factory).get("/audit")

    assert response.status_code == 403
    assert response.json()["detail"]["action_key"] == "audit_log.view"


def test_invalid_since_returns_422(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx

    response = _client(ctx, factory).get("/audit", params={"since": "wat"})

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_iso8601"


def test_blank_filters_are_ignored(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    _seed_audit(factory, workspace_id=workspace_id, action="visible")

    response = _client(ctx, factory).get(
        "/audit",
        params={"actor": "  ", "action": "", "cursor": ""},
    )

    assert response.status_code == 200, response.text
    assert "visible" in {row["action"] for row in response.json()["data"]}


def test_workspace_rows_do_not_cross_tenant(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        owner_a = bootstrap_user(
            session,
            email="owner-a@example.com",
            display_name="Owner A",
        )
        owner_b = bootstrap_user(
            session,
            email="owner-b@example.com",
            display_name="Owner B",
        )
        ws_a = bootstrap_workspace(
            session,
            slug="audit-a",
            name="Audit A",
            owner_user_id=owner_a.id,
        )
        ws_b = bootstrap_workspace(
            session,
            slug="audit-b",
            name="Audit B",
            owner_user_id=owner_b.id,
        )
        session.commit()
        ctx = ctx_for(
            workspace_id=ws_a.id,
            workspace_slug=ws_a.slug,
            actor_id=owner_a.id,
        )
    _seed_audit(factory, workspace_id=ws_a.id, action="visible")
    _seed_audit(factory, workspace_id=ws_b.id, action="hidden")

    response = _client(ctx, factory).get("/audit", params={"action": "visible"})

    assert response.status_code == 200, response.text
    assert [row["action"] for row in response.json()["data"]] == ["visible"]
