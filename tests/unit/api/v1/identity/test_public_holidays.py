"""HTTP-level tests for ``/public_holidays``."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.api.v1.public_holidays import build_public_holidays_router
from app.tenancy import WorkspaceContext
from tests.unit.api.v1.identity.conftest import build_client


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", build_public_holidays_router())], factory, ctx)


def _create(client: TestClient, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "Labour Day",
        "date": "2026-05-01",
        "country": "FR",
        "scheduling_effect": "block",
    }
    payload.update(overrides)
    resp = client.post("/public_holidays", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert isinstance(body, dict)
    return body


class TestPublicHolidayCrud:
    def test_create_get_patch_delete_soft_deletes(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, ws_id = owner_ctx
        client = _client(ctx, factory)

        created = _create(client)
        holiday_id = str(created["id"])
        assert created["workspace_id"] == ws_id
        assert created["country"] == "FR"

        fetched = client.get(f"/public_holidays/{holiday_id}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["id"] == holiday_id

        patched = client.patch(
            f"/public_holidays/{holiday_id}",
            json={
                "scheduling_effect": "reduced",
                "reduced_starts_local": "10:00:00",
                "reduced_ends_local": "14:00:00",
                "payroll_multiplier": "2.00",
            },
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["scheduling_effect"] == "reduced"
        assert patched.json()["reduced_starts_local"] == "10:00:00"
        assert patched.json()["payroll_multiplier"] == "2.00"

        deleted = client.delete(f"/public_holidays/{holiday_id}")
        assert deleted.status_code == 204, deleted.text
        assert client.get(f"/public_holidays/{holiday_id}").status_code == 404
        listed = client.get("/public_holidays")
        assert listed.status_code == 200, listed.text
        assert listed.json() == {"data": [], "next_cursor": None, "has_more": False}

    def test_unknown_update_and_delete_return_404(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        update = client.patch("/public_holidays/missing", json={"name": "X"})
        assert update.status_code == 404
        assert update.json()["detail"]["error"] == "public_holiday_not_found"
        delete = client.delete("/public_holidays/missing")
        assert delete.status_code == 404


class TestPublicHolidayList:
    def test_filters_by_range_and_country_including_workspace_wide(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        global_holiday = _create(
            client, name="New Year", date="2026-01-01", country=None
        )
        french_holiday = _create(client)
        _create(client, name="US Day", date="2026-05-01", country="US")
        _create(client, name="Out of range", date="2026-08-01", country="FR")

        resp = client.get(
            "/public_holidays",
            params={"from": "2026-01-01", "to": "2026-05-31", "country": "FR"},
        )
        assert resp.status_code == 200, resp.text
        ids = [row["id"] for row in resp.json()["data"]]
        assert ids == [global_holiday["id"], french_holiday["id"]]

    def test_cursor_paginates(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        h1 = _create(client, name="A", date="2026-01-01", country="FR")
        h2 = _create(client, name="B", date="2026-01-02", country="FR")
        h3 = _create(client, name="C", date="2026-01-03", country="FR")

        page1 = client.get("/public_holidays", params={"limit": 2})
        assert page1.status_code == 200, page1.text
        body1 = page1.json()
        assert [row["id"] for row in body1["data"]] == [h1["id"], h2["id"]]
        assert body1["has_more"] is True
        assert body1["next_cursor"] is not None

        page2 = client.get(
            "/public_holidays",
            params={"limit": 2, "cursor": body1["next_cursor"]},
        )
        assert page2.status_code == 200, page2.text
        assert page2.json()["data"][0]["id"] == h3["id"]
        assert page2.json()["has_more"] is False


class TestPublicHolidayValidation:
    def test_scheduling_effect_enum_and_reduced_pairing_validate(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        bad_effect = client.post(
            "/public_holidays",
            json={
                "name": "Bad",
                "date": "2026-05-01",
                "scheduling_effect": "closed",
            },
        )
        assert bad_effect.status_code == 422

        bad_reduced = client.post(
            "/public_holidays",
            json={
                "name": "Bad",
                "date": "2026-05-02",
                "scheduling_effect": "reduced",
                "reduced_starts_local": "09:00:00",
            },
        )
        assert bad_reduced.status_code == 422

    def test_annual_recurrence_matches_month_day_across_years(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        christmas = _create(
            client,
            name="Christmas",
            date="2026-12-25",
            country=None,
            recurrence="annual",
        )

        resp = client.get(
            "/public_holidays",
            params={"from": "2027-12-24", "to": "2027-12-26"},
        )
        assert resp.status_code == 200, resp.text
        assert [row["id"] for row in resp.json()["data"]] == [christmas["id"]]
        assert resp.json()["data"][0]["date"] == "2026-12-25"

    def test_duplicate_live_date_country_conflicts(
        self,
        owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
    ) -> None:
        ctx, factory, _ = owner_ctx
        client = _client(ctx, factory)
        _create(client, country=None)
        duplicate = client.post(
            "/public_holidays",
            json={
                "name": "Dup",
                "date": "2026-05-01",
                "country": None,
                "scheduling_effect": "allow",
            },
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["error"] == "public_holiday_conflict"


class TestPublicHolidayAuth:
    def test_worker_is_denied_by_manager_gate(
        self,
        worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
    ) -> None:
        ctx, factory, _, _ = worker_ctx
        client = _client(ctx, factory)
        resp = client.get("/public_holidays")
        assert resp.status_code == 403
        assert resp.json()["detail"] == {
            "error": "permission_denied",
            "action_key": "work_roles.manage",
        }
