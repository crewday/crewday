"""Focused HTTP tests for the instructions KB router."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.instructions.repositories import SqlAlchemyInstructionsRepository
from app.adapters.db.workspace.models import Workspace
from app.api.v1.instructions import router as instructions_router
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.unit.api.v1.identity.conftest import build_client
from tests.unit.test_service_instructions import _seed_area, _seed_property

pytest_plugins = ["tests.unit.api.v1.identity.conftest"]

_PINNED = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("/instructions", instructions_router)], factory, ctx)


def _create_payload(
    *,
    slug: str = "house-rules",
    title: str = "House rules",
    body_md: str = "No shoes inside.",
) -> dict[str, object]:
    return {
        "slug": slug,
        "title": title,
        "body_md": body_md,
        "scope": "global",
        "tags": ["Safety", "safety", " general "],
        "change_note": "initial",
    }


def test_create_returns_instruction_and_current_revision(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ = owner_ctx
    client = _client(ctx, factory)

    response = client.post("/instructions", json=_create_payload())

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["instruction"]["slug"] == "house-rules"
    assert body["instruction"]["scope"] == "global"
    assert body["instruction"]["tags"] == ["safety", "general"]
    assert body["current_revision"]["version"] == 1
    assert body["current_revision"]["body_md"] == "No shoes inside."
    assert body["instruction"]["current_revision_id"] == body["current_revision"]["id"]


def test_patch_body_bumps_version_and_metadata_keeps_current_shape(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ = owner_ctx
    client = _client(ctx, factory)
    created = client.post("/instructions", json=_create_payload()).json()
    instruction_id = created["instruction"]["id"]

    patched = client.patch(
        f"/instructions/{instruction_id}",
        json={
            "title": "House rules updated",
            "body_md": "No shoes, no glass by the pool.",
            "change_note": "pool safety",
        },
    )

    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["instruction"]["title"] == "House rules updated"
    assert body["current_revision"]["version"] == 2
    assert body["current_revision"]["body_md"] == "No shoes, no glass by the pool."
    assert body["current_revision"]["id"] != created["current_revision"]["id"]

    noop = client.patch(
        f"/instructions/{instruction_id}",
        json={"body_md": "No shoes, no glass by the pool."},
    )
    assert noop.status_code == 200, noop.text
    assert noop.json()["current_revision"]["version"] == 2


def test_patch_rejects_null_for_non_nullable_update_fields(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ = owner_ctx
    client = _client(ctx, factory)
    created = client.post("/instructions", json=_create_payload()).json()
    instruction_id = created["instruction"]["id"]

    response = client.patch(
        f"/instructions/{instruction_id}",
        json={"body_md": None},
    )

    assert response.status_code == 422, response.text


def test_list_and_get_use_cursor_envelope(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ = owner_ctx
    client = _client(ctx, factory)
    first = client.post(
        "/instructions",
        json=_create_payload(slug="a", title="A", body_md="A"),
    ).json()
    client.post(
        "/instructions",
        json=_create_payload(slug="b", title="B", body_md="B"),
    )
    client.post(
        "/instructions",
        json=_create_payload(slug="c", title="C", body_md="C"),
    )

    listed = client.get("/instructions", params={"limit": 2})

    assert listed.status_code == 200, listed.text
    page1 = listed.json()
    assert [row["slug"] for row in page1["data"]] == ["a", "b"]
    assert page1["has_more"] is True
    assert page1["next_cursor"] is not None

    page2 = client.get(
        "/instructions",
        params={"limit": 2, "cursor": page1["next_cursor"]},
    )
    assert page2.status_code == 200, page2.text
    assert [row["slug"] for row in page2.json()["data"]] == ["c"]

    fetched = client.get(f"/instructions/{first['instruction']['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["current_revision"]["body_md"] == "A"


def test_versions_list_and_specific_version(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ = owner_ctx
    client = _client(ctx, factory)
    created = client.post("/instructions", json=_create_payload(body_md="v1")).json()
    instruction_id = created["instruction"]["id"]
    client.patch(f"/instructions/{instruction_id}", json={"body_md": "v2"})
    client.patch(f"/instructions/{instruction_id}", json={"body_md": "v3"})

    listed = client.get(f"/instructions/{instruction_id}/versions", params={"limit": 2})

    assert listed.status_code == 200, listed.text
    page1 = listed.json()
    assert [row["version"] for row in page1["data"]] == [3, 2]
    assert page1["has_more"] is True
    assert page1["next_cursor"] is not None

    page2 = client.get(
        f"/instructions/{instruction_id}/versions",
        params={"limit": 2, "cursor": page1["next_cursor"]},
    )
    assert page2.status_code == 200, page2.text
    assert [row["version"] for row in page2.json()["data"]] == [1]

    v2 = client.get(f"/instructions/{instruction_id}/versions/2")
    assert v2.status_code == 200, v2.text
    assert v2.json()["body_md"] == "v2"


def test_scope_resolver_returns_deduped_union_with_provenance(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    with factory() as session:
        ws = session.get(Workspace, workspace_id)
        assert ws is not None
        prop = _seed_property(session, ws=ws, label="Villa Sud")
        area = _seed_area(session, prop=prop, label="Pool")
        session.commit()
    client = _client(ctx, factory)
    global_row = client.post(
        "/instructions",
        json=_create_payload(slug="global", title="Global", body_md="global"),
    ).json()
    property_row = client.post(
        "/instructions",
        json={
            **_create_payload(slug="property", title="Property", body_md="property"),
            "scope": "property",
            "property_id": prop.id,
        },
    ).json()
    area_row = client.post(
        "/instructions",
        json={
            **_create_payload(slug="area", title="Area", body_md="area"),
            "scope": "area",
            "property_id": prop.id,
            "area_id": area.id,
        },
    ).json()
    template_id = new_ulid()
    with factory() as session:
        repo = SqlAlchemyInstructionsRepository(session)
        repo.insert_instruction_link(
            link_id=new_ulid(),
            workspace_id=ctx.workspace_id,
            instruction_id=global_row["instruction"]["id"],
            target_kind="task_template",
            target_id=template_id,
            added_by=ctx.actor_id,
            added_at=_PINNED,
        )
        session.commit()

    response = client.get(
        "/instructions:scope",
        params={"template": template_id, "area": area.id},
    )

    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert [row["instruction_id"] for row in rows] == [
        area_row["instruction"]["id"],
        property_row["instruction"]["id"],
        global_row["instruction"]["id"],
    ]
    assert [row["provenance"] for row in rows] == [
        "scope:area",
        "scope:property",
        "scope:global",
    ]
    assert [row["body_md"] for row in rows] == ["area", "property", "global"]
    assert response.json()["has_more"] is False


def test_slug_uniqueness_maps_to_409(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ = owner_ctx
    client = _client(ctx, factory)

    first = client.post("/instructions", json=_create_payload(slug="duplicate"))
    second = client.post("/instructions", json=_create_payload(slug="duplicate"))

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["error"] == "instruction_slug_conflict"


def test_create_rejects_unknown_fields(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ = owner_ctx
    client = _client(ctx, factory)

    response = client.post(
        "/instructions",
        json={**_create_payload(), "unsupported": True},
    )

    assert response.status_code == 422, response.text
