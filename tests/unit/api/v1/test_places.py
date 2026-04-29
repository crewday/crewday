"""Focused HTTP tests for the places CRUD router (cd-75wp)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.places.models import PropertyWorkspace, Unit
from app.adapters.db.stays.models import Reservation
from app.api.transport.sse import _default_invalidates
from app.api.v1.places import build_properties_router
from app.events import bus
from app.events.types import PropertyWorkspaceChanged
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client

pytest_plugins = ["tests.unit.api.v1.places.conftest"]

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", build_properties_router())], factory, ctx)


def _property_body(name: str = "Villa API") -> dict[str, object]:
    return {
        "name": name,
        "kind": "vacation",
        "timezone": "Europe/Paris",
        "address_json": {
            "line1": "12 Chemin des Oliviers",
            "line2": None,
            "city": "Antibes",
            "state_province": "Alpes-Maritimes",
            "postal_code": "06600",
            "country": "FR",
        },
    }


def test_property_create_accepts_address_json_and_backfills_country(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ = owner_ctx
    client = _client(ctx, factory)

    response = client.post("/properties", json=_property_body())

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["country"] == "FR"
    assert body["address_json"]["country"] == "FR"
    assert body["address"] == (
        "12 Chemin des Oliviers, Antibes, Alpes-Maritimes, 06600, FR"
    )

    with factory() as session, tenant_agnostic():
        units = session.query(Unit).filter(Unit.property_id == body["id"]).all()
    assert [unit.name for unit in units] == ["Villa API"]


def test_units_and_areas_listings_are_cursor_paginated(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ = owner_ctx
    client = _client(ctx, factory)
    property_id = client.post("/properties", json=_property_body()).json()["id"]
    client.post(f"/properties/{property_id}/units", json={"name": "Guest house"})
    client.post(f"/properties/{property_id}/areas", json={"name": "Pool"})
    client.post(f"/properties/{property_id}/areas", json={"name": "Garden"})

    units = client.get(f"/properties/{property_id}/units", params={"limit": 1})
    assert units.status_code == 200, units.text
    assert len(units.json()["data"]) == 1
    assert units.json()["has_more"] is True
    assert units.json()["next_cursor"] is not None

    areas = client.get(f"/properties/{property_id}/areas", params={"limit": 1})
    assert areas.status_code == 200, areas.text
    assert len(areas.json()["data"]) == 1
    assert areas.json()["has_more"] is True
    assert areas.json()["next_cursor"] is not None


def test_closure_refuses_overlapping_stay_unless_forced(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    client = _client(ctx, factory)
    property_id = client.post("/properties", json=_property_body()).json()["id"]
    with factory() as session:
        session.add(
            Reservation(
                id=new_ulid(),
                workspace_id=workspace_id,
                property_id=property_id,
                ical_feed_id=None,
                external_uid="manual-1",
                check_in=datetime(2026, 5, 1, 15, 0, tzinfo=UTC),
                check_out=datetime(2026, 5, 3, 10, 0, tzinfo=UTC),
                guest_name=None,
                guest_count=None,
                status="scheduled",
                source="manual",
                raw_summary=None,
                raw_description=None,
                guest_link_id=None,
                created_at=_PINNED,
            )
        )
        session.commit()

    payload = {
        "property_id": property_id,
        "starts_at": "2026-05-02T00:00:00+00:00",
        "ends_at": "2026-05-04T00:00:00+00:00",
        "reason": "renovation",
    }
    blocked = client.post("/property_closures", json=payload)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["error"] == "closure_stay_conflict"

    forced = client.post("/property_closures", params={"force": True}, json=payload)
    assert forced.status_code == 201, forced.text
    assert forced.json()["property_id"] == property_id


def test_closure_persists_and_returns_unit_id(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ = owner_ctx
    client = _client(ctx, factory)
    property_id = client.post("/properties", json=_property_body()).json()["id"]
    unit_id = client.get(f"/properties/{property_id}/units").json()["data"][0]["id"]

    response = client.post(
        "/property_closures",
        json={
            "property_id": property_id,
            "unit_id": unit_id,
            "starts_at": "2026-05-02T00:00:00+00:00",
            "ends_at": "2026-05-04T00:00:00+00:00",
            "reason": "renovation",
        },
    )

    assert response.status_code == 201, response.text
    created = response.json()
    assert created["unit_id"] == unit_id

    listed = client.get(
        "/property_closures", params={"property_id": property_id, "unit_id": unit_id}
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"][0]["unit_id"] == unit_id

    updated = client.patch(
        f"/property_closures/{created['id']}",
        json={
            "unit_id": None,
            "starts_at": "2026-05-03T00:00:00+00:00",
            "ends_at": "2026-05-05T00:00:00+00:00",
            "reason": "seasonal",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["unit_id"] is None


def test_closure_rejects_invalid_or_cross_property_unit_id(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ = owner_ctx
    client = _client(ctx, factory)
    property_id = client.post("/properties", json=_property_body("Villa A")).json()[
        "id"
    ]
    other_property_id = client.post(
        "/properties", json=_property_body("Villa B")
    ).json()["id"]
    other_unit_id = client.get(f"/properties/{other_property_id}/units").json()["data"][
        0
    ]["id"]

    for unit_id in ("01HWA00000000000000000NOPE", other_unit_id):
        response = client.post(
            "/property_closures",
            json={
                "property_id": property_id,
                "unit_id": unit_id,
                "starts_at": "2026-05-02T00:00:00+00:00",
                "ends_at": "2026-05-04T00:00:00+00:00",
                "reason": "renovation",
            },
        )
        assert response.status_code == 404, response.text
        assert response.json()["detail"]["error"] == "property_closure_not_found"

    valid = client.post(
        "/property_closures",
        json={
            "property_id": property_id,
            "starts_at": "2026-05-02T00:00:00+00:00",
            "ends_at": "2026-05-04T00:00:00+00:00",
            "reason": "renovation",
        },
    )
    assert valid.status_code == 201, valid.text
    update = client.patch(
        f"/property_closures/{valid.json()['id']}",
        json={
            "unit_id": other_unit_id,
            "starts_at": "2026-05-02T00:00:00+00:00",
            "ends_at": "2026-05-04T00:00:00+00:00",
            "reason": "renovation",
        },
    )
    assert update.status_code == 404, update.text
    assert update.json()["detail"]["error"] == "property_closure_not_found"


def test_share_creates_membership_and_publishes_event(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ = owner_ctx
    client = _client(ctx, factory)
    property_id = client.post("/properties", json=_property_body()).json()["id"]
    with factory() as session:
        target_owner = bootstrap_user(
            session, email="target-owner@example.com", display_name="Target Owner"
        )
        target = bootstrap_workspace(
            session,
            slug="target-share",
            name="Target Share",
            owner_user_id=target_owner.id,
        )
        session.commit()
        target_id = target.id

    seen: list[PropertyWorkspaceChanged] = []
    bus._reset_for_tests()
    bus.subscribe(PropertyWorkspaceChanged)(seen.append)
    try:
        response = client.post(
            f"/properties/{property_id}/share",
            json={
                "workspace_slug": "target-share",
                "membership_role": "managed_workspace",
                "share_guest_identity": True,
            },
        )
    finally:
        bus._reset_for_tests()

    assert response.status_code == 201, response.text
    assert response.json()["workspace_id"] == target_id
    assert response.json()["share_guest_identity"] is True
    assert [event.workspace_id for event in seen] == [ctx.workspace_id, target_id]
    assert [event.change_kind for event in seen] == ["invited", "invited"]
    assert {event.property_id for event in seen} == {property_id}
    assert {event.target_workspace_id for event in seen} == {target_id}

    with factory() as session, tenant_agnostic():
        row = session.get(
            PropertyWorkspace,
            {"property_id": property_id, "workspace_id": target_id},
        )
    assert row is not None
    assert row.membership_role == "managed_workspace"


def test_revoke_accepts_workspace_id_path_ref(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ = owner_ctx
    client = _client(ctx, factory)
    property_id = client.post("/properties", json=_property_body()).json()["id"]
    with factory() as session:
        target_owner = bootstrap_user(
            session, email="target-revoke@example.com", display_name="Target Revoke"
        )
        target = bootstrap_workspace(
            session,
            slug="target-revoke",
            name="Target Revoke",
            owner_user_id=target_owner.id,
        )
        session.commit()
        target_id = target.id

    response = client.post(
        f"/properties/{property_id}/share",
        json={"workspace_id": target_id, "membership_role": "managed_workspace"},
    )
    assert response.status_code == 201, response.text

    seen: list[PropertyWorkspaceChanged] = []
    bus._reset_for_tests()
    bus.subscribe(PropertyWorkspaceChanged)(seen.append)
    try:
        revoked = client.delete(f"/properties/{property_id}/share/{target_id}")
    finally:
        bus._reset_for_tests()

    assert revoked.status_code == 204, revoked.text
    assert [event.workspace_id for event in seen] == [ctx.workspace_id, target_id]
    assert [event.change_kind for event in seen] == ["revoked", "revoked"]

    with factory() as session, tenant_agnostic():
        row = session.get(
            PropertyWorkspace,
            {"property_id": property_id, "workspace_id": target_id},
        )
    assert row is None


def test_share_with_unknown_workspace_id_returns_not_found(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _ = owner_ctx
    client = _client(ctx, factory)
    property_id = client.post("/properties", json=_property_body()).json()["id"]

    response = client.post(
        f"/properties/{property_id}/share",
        json={"workspace_id": "01KUNKNOWNWORKSPACE000000000"},
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["error"] == "property_workspace_not_found"


def test_property_closure_events_carry_sse_invalidations() -> None:
    expected = [["stays"], ["scheduler-calendar"], ["my-schedule"]]

    assert _default_invalidates("property.closure.created") == expected
    assert _default_invalidates("property.closure.updated") == expected
