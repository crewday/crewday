"""HTTP tests for workspace-scoped ``GET /bookings``."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.payroll.models import Booking
from app.adapters.db.workspace.models import WorkEngagement
from app.api.v1.bookings import build_bookings_router
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import (
    build_client,
    ctx_for,
    seed_worker_user,
)

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", build_bookings_router())], factory, ctx)


def _seed_engagement(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
) -> str:
    with factory() as s:
        engagement_id = new_ulid()
        s.add(
            WorkEngagement(
                id=engagement_id,
                user_id=user_id,
                workspace_id=workspace_id,
                engagement_kind="payroll",
                supplier_org_id=None,
                pay_destination_id=None,
                reimbursement_destination_id=None,
                started_on=date(2026, 1, 1),
                archived_on=None,
                notes_md=None,
                created_at=_PINNED,
                updated_at=_PINNED,
            )
        )
        s.commit()
        return engagement_id


def _seed_booking(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
    work_engagement_id: str,
    property_id: str | None = "prop-main",
    scheduled_start: datetime = datetime(2026, 5, 6, 9, 0, tzinfo=UTC),
    status: str = "scheduled",
    deleted: bool = False,
    pending_amend_minutes: int | None = None,
) -> str:
    with factory() as s:
        booking_id = new_ulid()
        s.add(
            Booking(
                id=booking_id,
                workspace_id=workspace_id,
                work_engagement_id=work_engagement_id,
                user_id=user_id,
                property_id=property_id,
                client_org_id=None,
                status=status,
                kind="work",
                pay_basis="scheduled",
                scheduled_start=scheduled_start,
                scheduled_end=scheduled_start + timedelta(hours=2),
                actual_minutes=None,
                actual_minutes_paid=120,
                break_seconds=0,
                notes_md="Window notes",
                adjusted=False,
                adjustment_reason=None,
                pending_amend_minutes=pending_amend_minutes,
                pending_amend_reason=(
                    "Stayed late" if pending_amend_minutes is not None else None
                ),
                declined_at=None,
                declined_reason=None,
                cancelled_at=None,
                cancellation_window_hours=24,
                cancellation_pay_to_worker=True,
                created_by_actor_kind=None,
                created_by_actor_id=None,
                created_at=_PINNED,
                updated_at=_PINNED,
                deleted_at=_PINNED if deleted else None,
            )
        )
        s.commit()
        return booking_id


def test_manager_lists_workspace_bookings_shape(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    engagement_id = _seed_engagement(
        factory, workspace_id=workspace_id, user_id=ctx.actor_id
    )
    booking_id = _seed_booking(
        factory,
        workspace_id=workspace_id,
        user_id=ctx.actor_id,
        work_engagement_id=engagement_id,
    )
    _seed_booking(
        factory,
        workspace_id=workspace_id,
        user_id=ctx.actor_id,
        work_engagement_id=engagement_id,
        deleted=True,
        scheduled_start=datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
    )

    resp = _client(ctx, factory).get("/bookings")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["id"] for row in body] == [booking_id]
    booking = body[0]
    assert booking == {
        "id": booking_id,
        "employee_id": ctx.actor_id,
        "user_id": ctx.actor_id,
        "work_engagement_id": engagement_id,
        "property_id": "prop-main",
        "client_org_id": None,
        "status": "scheduled",
        "kind": "work",
        "scheduled_start": "2026-05-06T09:00:00Z",
        "scheduled_end": "2026-05-06T11:00:00Z",
        "actual_minutes": None,
        "actual_minutes_paid": 120,
        "break_seconds": 0,
        "pending_amend_minutes": None,
        "pending_amend_reason": None,
        "declined_at": None,
        "declined_reason": None,
        "notes_md": "Window notes",
        "adjusted": False,
        "adjustment_reason": None,
    }


def test_worker_lists_only_own_bookings(
    worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
) -> None:
    ctx, factory, workspace_id, worker_id = worker_ctx
    own_engagement_id = _seed_engagement(
        factory, workspace_id=workspace_id, user_id=worker_id
    )
    other_user_id = _seed_other_worker(
        factory, workspace_id=workspace_id, email="other-worker@example.com"
    )
    other_engagement_id = _seed_engagement(
        factory, workspace_id=workspace_id, user_id=other_user_id
    )
    own_booking_id = _seed_booking(
        factory,
        workspace_id=workspace_id,
        user_id=worker_id,
        work_engagement_id=own_engagement_id,
    )
    _seed_booking(
        factory,
        workspace_id=workspace_id,
        user_id=other_user_id,
        work_engagement_id=other_engagement_id,
        scheduled_start=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
    )

    resp = _client(ctx, factory).get("/bookings")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["id"] for row in body] == [own_booking_id]
    assert body[0]["employee_id"] == worker_id


def test_worker_cannot_query_another_users_bookings(
    worker_ctx: tuple[WorkspaceContext, sessionmaker[Session], str, str],
) -> None:
    ctx, factory, workspace_id, _worker_id = worker_ctx
    other_user_id = _seed_other_worker(
        factory, workspace_id=workspace_id, email="blocked-worker@example.com"
    )

    resp = _client(ctx, factory).get("/bookings", params={"user_id": other_user_id})

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"] == "permission_denied"
    assert resp.json()["action_key"] == "bookings.view_other"


def test_client_cannot_list_workspace_bookings(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    owner_ctx_value, factory, workspace_id = owner_ctx
    client_user_id = _seed_client_user(
        factory, workspace_id=workspace_id, email="client@example.com"
    )
    engagement_id = _seed_engagement(
        factory, workspace_id=workspace_id, user_id=owner_ctx_value.actor_id
    )
    _seed_booking(
        factory,
        workspace_id=workspace_id,
        user_id=owner_ctx_value.actor_id,
        work_engagement_id=engagement_id,
    )
    ctx = ctx_for(
        workspace_id=workspace_id,
        workspace_slug=owner_ctx_value.workspace_slug,
        actor_id=client_user_id,
        grant_role="client",
        actor_was_owner_member=False,
    )

    resp = _client(ctx, factory).get("/bookings")

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"] == "permission_denied"
    assert resp.json()["action_key"] == "bookings.view_other"


def test_manager_can_filter_by_user_and_property(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    worker_id = _seed_other_worker(
        factory, workspace_id=workspace_id, email="filtered-worker@example.com"
    )
    owner_engagement_id = _seed_engagement(
        factory, workspace_id=workspace_id, user_id=ctx.actor_id
    )
    worker_engagement_id = _seed_engagement(
        factory, workspace_id=workspace_id, user_id=worker_id
    )
    expected_id = _seed_booking(
        factory,
        workspace_id=workspace_id,
        user_id=worker_id,
        work_engagement_id=worker_engagement_id,
        property_id="prop-filtered",
    )
    _seed_booking(
        factory,
        workspace_id=workspace_id,
        user_id=worker_id,
        work_engagement_id=worker_engagement_id,
        property_id="prop-other",
    )
    _seed_booking(
        factory,
        workspace_id=workspace_id,
        user_id=ctx.actor_id,
        work_engagement_id=owner_engagement_id,
        property_id="prop-filtered",
    )

    resp = _client(ctx, factory).get(
        "/bookings",
        params={"user_id": worker_id, "property_id": "prop-filtered"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["id"] for row in body] == [expected_id]
    assert body[0]["employee_id"] == worker_id
    assert body[0]["property_id"] == "prop-filtered"


def test_list_excludes_other_workspaces_bookings(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    engagement_id = _seed_engagement(
        factory, workspace_id=workspace_id, user_id=ctx.actor_id
    )
    expected_id = _seed_booking(
        factory,
        workspace_id=workspace_id,
        user_id=ctx.actor_id,
        work_engagement_id=engagement_id,
    )
    other_workspace_id = _seed_other_workspace(
        factory,
        owner_user_id=ctx.actor_id,
        slug="other-bookings-ws",
    )
    other_engagement_id = _seed_engagement(
        factory, workspace_id=other_workspace_id, user_id=ctx.actor_id
    )
    _seed_booking(
        factory,
        workspace_id=other_workspace_id,
        user_id=ctx.actor_id,
        work_engagement_id=other_engagement_id,
    )

    resp = _client(ctx, factory).get("/bookings")

    assert resp.status_code == 200, resp.text
    assert [row["id"] for row in resp.json()] == [expected_id]


def test_list_filters_status_window_and_pending_amend(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    engagement_id = _seed_engagement(
        factory, workspace_id=workspace_id, user_id=ctx.actor_id
    )
    expected_id = _seed_booking(
        factory,
        workspace_id=workspace_id,
        user_id=ctx.actor_id,
        work_engagement_id=engagement_id,
        scheduled_start=datetime(2026, 5, 6, 9, 0, tzinfo=UTC),
        pending_amend_minutes=150,
    )
    _seed_booking(
        factory,
        workspace_id=workspace_id,
        user_id=ctx.actor_id,
        work_engagement_id=engagement_id,
        scheduled_start=datetime(2026, 5, 8, 9, 0, tzinfo=UTC),
        status="completed",
        pending_amend_minutes=150,
    )
    _seed_booking(
        factory,
        workspace_id=workspace_id,
        user_id=ctx.actor_id,
        work_engagement_id=engagement_id,
        scheduled_start=datetime(2026, 5, 9, 9, 0, tzinfo=UTC),
        pending_amend_minutes=None,
    )

    resp = _client(ctx, factory).get(
        "/bookings",
        params={
            "from": "2026-05-06T00:00:00Z",
            "to": "2026-05-07T00:00:00Z",
            "status": "scheduled",
            "pending_amend": "true",
        },
    )

    assert resp.status_code == 200, resp.text
    assert [row["id"] for row in resp.json()] == [expected_id]


def test_backwards_window_returns_422(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx

    resp = _client(ctx, factory).get(
        "/bookings",
        params={"from": "2026-05-08T00:00:00Z", "to": "2026-05-07T00:00:00Z"},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "invalid_field"


def _seed_other_worker(
    factory: sessionmaker[Session], *, workspace_id: str, email: str
) -> str:
    with factory() as s:
        user_id = seed_worker_user(
            s, workspace_id=workspace_id, email=email, display_name="Other Worker"
        )
        s.commit()
        return user_id


def _seed_client_user(
    factory: sessionmaker[Session], *, workspace_id: str, email: str
) -> str:
    with factory() as s:
        user = bootstrap_user(s, email=email, display_name="Client")
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user.id,
                grant_role="client",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        s.commit()
        return user.id


def _seed_other_workspace(
    factory: sessionmaker[Session], *, owner_user_id: str, slug: str
) -> str:
    with factory() as s:
        workspace = bootstrap_workspace(
            s,
            slug=slug,
            name="Other Bookings WS",
            owner_user_id=owner_user_id,
        )
        s.commit()
        return workspace.id
