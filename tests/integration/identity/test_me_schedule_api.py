"""Integration tests for ``/me/{schedule,leaves,availability_overrides}`` (cd-6uij).

Exercises the router through :class:`TestClient` against a real DB
engine with the same model surface the production schema ships. Each
test asserts on:

* HTTP boundary: status code, response shape, error envelope.
* Persistence: rows land in ``user_leave`` /
  ``user_availability_override`` tables; the aggregator's reads see
  them through the live ORM tenant filter.
* Cross-workspace isolation through the production tenant filter.

Pattern matches :mod:`tests.integration.identity.test_user_leaves_api`:
a per-test in-memory SQLite engine + ``Base.metadata.create_all``
keeps the fixture cost low without sacrificing the integration-tier
guarantee that the **real** ORM seam fires (tenant filter, FK checks).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.availability.models import (
    UserLeave,
    UserWeeklyAvailability,
)
from app.adapters.db.base import Base
from app.adapters.db.payroll.models import Booking
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import WorkEngagement
from app.api.deps import current_workspace_context, db_session
from app.api.v1.me_schedule import build_me_schedule_router
from app.tenancy import WorkspaceContext, registry
from app.tenancy.context import ActorGrantRole
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<ctx>.models`` module."""
    import importlib
    import pkgutil

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
    """Per-test in-memory SQLite engine."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def api_factory(api_engine: Engine) -> sessionmaker[Session]:
    """``sessionmaker`` with the tenant filter installed."""
    factory = sessionmaker(bind=api_engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    """Register the workspace-scoped tables this module touches.

    Most scoped tables auto-register on package import (workspace +
    places + payroll). The list below is the residual set used by
    the schedule aggregator that is **not** auto-registered (the
    historical tests pinned them defensively in case sibling tests
    in the same xdist worker had cleared the registry).
    """
    registry.register("user_leave")
    registry.register("user_availability_override")
    registry.register("user_weekly_availability")
    registry.register("public_holiday")
    registry.register("occurrence")
    registry.register("audit_log")
    registry.register("role_grant")
    registry.register("permission_group")
    registry.register("permission_group_member")
    registry.register("booking")


def _ctx(
    *,
    workspace_id: str,
    workspace_slug: str,
    actor_id: str,
    grant_role: ActorGrantRole = "manager",
    actor_was_owner_member: bool = True,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=actor_was_owner_member,
        audit_correlation_id=new_ulid(),
    )


def _build_app(factory: sessionmaker[Session], ctx: WorkspaceContext) -> FastAPI:
    """Mount the me-schedule router behind pinned ctx + UoW overrides.

    Adds a :class:`~starlette.middleware.base.BaseHTTPMiddleware`
    that mirrors :class:`WorkspaceContextMiddleware`: it calls
    :func:`set_current` on the way in and :func:`reset_current` on
    the way out, so the tenant filter sees a live
    :class:`WorkspaceContext` at SELECT compile time.
    """
    from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response as StarletteResponse

    from app.tenancy.current import reset_current, set_current

    app = FastAPI()

    class _PinCtxMiddleware(BaseHTTPMiddleware):
        async def dispatch(
            self, request: Request, call_next: RequestResponseEndpoint
        ) -> StarletteResponse:
            token = set_current(ctx)
            try:
                response = await call_next(request)
                assert isinstance(response, StarletteResponse)
                return response
            finally:
                reset_current(token)

    app.add_middleware(_PinCtxMiddleware)
    app.include_router(build_me_schedule_router())

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


def _seed_workspace_with_owner(
    factory: sessionmaker[Session], *, slug: str
) -> tuple[str, str, str]:
    """Seed a workspace + an owner user. Returns (ws_id, ws_slug, owner_id)."""
    with factory() as s:
        user = bootstrap_user(
            s, email=f"{slug}-owner@example.com", display_name=f"Owner {slug}"
        )
        ws = bootstrap_workspace(s, slug=slug, name=f"WS {slug}", owner_user_id=user.id)
        s.commit()
        return ws.id, ws.slug, user.id


def _seed_worker(factory: sessionmaker[Session], *, ws_id: str, email: str) -> str:
    with factory() as s:
        user = bootstrap_user(s, email=email, display_name=email.split("@")[0])
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=ws_id,
                user_id=user.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        s.commit()
        return user.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_worker_creates_leave_and_sees_it_in_schedule(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """Worker POSTs ``/me/leaves`` and the row lands in ``/me/schedule.leaves``.

        End-to-end through the production tenant filter: persist the
        row via the POST, then read it back via the schedule
        aggregator to confirm both writes and reads honour the
        ``ctx.actor_id`` predicate. Approved + pending leaves now
        live in a single ``leaves`` list — the SPA branches on
        ``approved_at``.
        """
        ws_id, ws_slug, _owner_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-1"
        )
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="me-int-1-w@example.com"
        )
        worker_ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        client = TestClient(
            _build_app(api_factory, worker_ctx),
            raise_server_exceptions=False,
        )

        post = client.post(
            "/me/leaves",
            json={
                "starts_on": "2026-05-04",
                "ends_on": "2026-05-06",
                "category": "vacation",
                "note_md": "Family trip",
            },
        )
        assert post.status_code == 201, post.text
        leave_id = post.json()["id"]
        assert post.json()["user_id"] == worker_id
        assert post.json()["approved_at"] is None

        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Approved + pending merged — pending self-submit lives in the
        # main ``leaves`` list and the SPA reads ``approved_at`` to
        # decide pending state.
        assert [lv["id"] for lv in body["leaves"]] == [leave_id]
        assert body["leaves"][0]["approved_at"] is None

    def test_worker_creates_override_and_sees_it_in_schedule(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """Worker POSTs ``/me/availability_overrides`` and the resolved state lands.

        Adding hours on an off pattern auto-approves per §06's hybrid
        approval matrix; the aggregator returns the row in the
        ``overrides`` list with ``approved_at`` set.
        """
        ws_id, ws_slug, _owner_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-2"
        )
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="me-int-2-w@example.com"
        )
        # Seed an off-pattern row for Monday so the aggregator can
        # walk the §06 approval matrix deterministically.
        with api_factory() as s:
            s.add(
                UserWeeklyAvailability(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=worker_id,
                    weekday=0,
                    starts_local=None,
                    ends_local=None,
                    updated_at=_PINNED,
                )
            )
            s.commit()
        worker_ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        client = TestClient(
            _build_app(api_factory, worker_ctx),
            raise_server_exceptions=False,
        )

        post = client.post(
            "/me/availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
                "ends_local": "13:00:00",
            },
        )
        assert post.status_code == 201, post.text
        override_id = post.json()["id"]
        assert post.json()["user_id"] == worker_id
        assert post.json()["approval_required"] is False
        assert post.json()["approved_at"] is not None

        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        body = resp.json()
        assert [ov["id"] for ov in body["overrides"]] == [override_id]
        assert body["overrides"][0]["approved_at"] is not None

    def test_worker_lists_self_overrides_through_listing_endpoint(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """``GET /me/availability_overrides`` returns rows the worker just POSTed.

        End-to-end pin on the §12 "Self-service shortcuts" listing
        contract: persist via ``POST``, list via ``GET``, confirm
        the ``user_id`` is forced to ``ctx.actor_id`` and the
        cursor envelope (``data``/``next_cursor``/``has_more``) is
        populated. A sibling worker's row in the same workspace is
        seeded to confirm the listing does not widen.
        """
        ws_id, ws_slug, _owner_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-list-1"
        )
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="me-int-list-1-w@example.com"
        )
        sibling_id = _seed_worker(
            api_factory, ws_id=ws_id, email="me-int-list-1-other@example.com"
        )
        # Seed an off-pattern Monday so the worker's POST auto-approves
        # (same shape as the existing override-create test).
        with api_factory() as s:
            s.add(
                UserWeeklyAvailability(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=worker_id,
                    weekday=0,
                    starts_local=None,
                    ends_local=None,
                    updated_at=_PINNED,
                )
            )
            s.commit()
        worker_ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        client = TestClient(
            _build_app(api_factory, worker_ctx),
            raise_server_exceptions=False,
        )

        post = client.post(
            "/me/availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
                "ends_local": "13:00:00",
            },
        )
        assert post.status_code == 201, post.text
        own_override_id = post.json()["id"]

        # Seed a sibling override directly through the ORM — it must
        # not surface in the worker's self-listing.
        from app.adapters.db.availability.models import UserAvailabilityOverride

        with api_factory() as s:
            s.add(
                UserAvailabilityOverride(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=sibling_id,
                    date=date(2026, 5, 4),
                    available=True,
                    starts_local=None,
                    ends_local=None,
                    reason=None,
                    approval_required=False,
                    approved_at=_PINNED,
                    approved_by=sibling_id,
                    created_at=_PINNED,
                    updated_at=_PINNED,
                    deleted_at=None,
                )
            )
            s.commit()

        resp = client.get("/me/availability_overrides")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [ov["id"] for ov in body["data"]] == [own_override_id]
        assert all(ov["user_id"] == worker_id for ov in body["data"])
        assert body["next_cursor"] is None
        assert body["has_more"] is False

    def test_default_window_omitted_params(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """Calling ``/me/schedule`` with no params returns the 14-day default window."""
        ws_id, ws_slug, owner_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-5"
        )
        owner_ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=owner_id,
            grant_role="manager",
            actor_was_owner_member=True,
        )
        client = TestClient(
            _build_app(api_factory, owner_ctx),
            raise_server_exceptions=False,
        )
        resp = client.get("/me/schedule")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        from_d = date.fromisoformat(body["window"]["from"])
        to_d = date.fromisoformat(body["window"]["to"])
        # 14-day window per §12.
        assert (to_d - from_d) == timedelta(days=14)
        # Empty payload — no rows seeded.
        assert body["weekly_availability"] == []
        assert body["tasks"] == []
        assert body["leaves"] == []
        assert body["overrides"] == []
        assert body["bookings"] == []
        assert body["assignments"] == []
        assert body["properties"] == []

    def test_cross_workspace_isolation_through_tenant_filter(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """The production tenant filter blocks cross-workspace leaks."""
        ws_a_id, ws_a_slug, owner_a_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-a"
        )
        ws_b_id, _ws_b_slug, _owner_b_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-b"
        )
        # Seed a leave for the A owner inside workspace B (same user
        # id, different workspace) so a missing ``workspace_id``
        # predicate would surface it.
        with api_factory() as s:
            s.add(
                UserLeave(
                    id=new_ulid(),
                    workspace_id=ws_b_id,
                    user_id=owner_a_id,
                    starts_on=date(2026, 5, 4),
                    ends_on=date(2026, 5, 5),
                    category="vacation",
                    approved_at=_PINNED,
                    approved_by=owner_a_id,
                    note_md=None,
                    created_at=_PINNED,
                    updated_at=_PINNED,
                    deleted_at=None,
                )
            )
            s.commit()

        ctx_a = _ctx(
            workspace_id=ws_a_id,
            workspace_slug=ws_a_slug,
            actor_id=owner_a_id,
            grant_role="manager",
            actor_was_owner_member=True,
        )
        client = TestClient(
            _build_app(api_factory, ctx_a),
            raise_server_exceptions=False,
        )
        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        body = resp.json()
        assert body["leaves"] == []

    def test_explicit_user_id_in_body_rejected(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """A worker passing ``user_id`` in the body collapses to 422."""
        ws_id, ws_slug, _owner_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-4"
        )
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="me-int-4-w@example.com"
        )
        other_id = _seed_worker(
            api_factory, ws_id=ws_id, email="me-int-4-other@example.com"
        )
        worker_ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        client = TestClient(
            _build_app(api_factory, worker_ctx),
            raise_server_exceptions=False,
        )

        resp = client.post(
            "/me/leaves",
            json={
                "user_id": other_id,
                "starts_on": "2026-07-01",
                "ends_on": "2026-07-02",
                "category": "personal",
            },
        )
        assert resp.status_code == 422

        # Confirm no row was persisted to the other user.
        with UnitOfWorkImpl(session_factory=api_factory) as s:
            from app.tenancy.current import reset_current, set_current

            assert isinstance(s, Session)
            t = set_current(worker_ctx)
            try:
                from sqlalchemy import select

                rows = s.scalars(
                    select(UserLeave).where(UserLeave.user_id == other_id)
                ).all()
            finally:
                reset_current(t)
        assert rows == []


class TestBookingsPropertyLocalWindow:
    """End-to-end pin: bookings are bucketed by property-local date.

    The aggregator (cd-ijte) projects each booking's
    ``scheduled_start`` into the property's IANA timezone before
    bucketing it into the worker's ``[from, to]`` window. This pins
    that two bookings the naïve UTC bound would mis-classify (one
    east of UTC, one west) round-trip correctly through the SA
    repository — i.e. the over-fetched UTC SELECT plus the
    aggregator post-filter agree with the §14 worker calendar's
    "today through to in property time" intent.
    """

    def _seed_property_and_engagement(
        self,
        factory: sessionmaker[Session],
        *,
        ws_id: str,
        user_id: str,
        prop_id: str,
        timezone: str,
    ) -> str:
        """Seed one property with the requested timezone + a worker engagement.

        Returns the ``work_engagement_id`` so the caller can attach
        bookings. The property is wired to the workspace via
        ``PropertyWorkspace`` so ``list_workspace_properties`` (the
        route's source of the timezone bag) sees it.
        """
        engagement_id = new_ulid()
        with factory() as s:
            s.add(
                Property(
                    id=prop_id,
                    name=f"{timezone} villa",
                    kind="str",
                    address="1 Calendar Lane",
                    address_json={"line1": "1 Calendar Lane"},
                    country="NZ",
                    timezone=timezone,
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
                    property_id=prop_id,
                    workspace_id=ws_id,
                    label=f"{timezone} villa",
                    membership_role="owner_workspace",
                    share_guest_identity=True,
                    status="active",
                    created_at=_PINNED,
                )
            )
            s.add(
                WorkEngagement(
                    id=engagement_id,
                    user_id=user_id,
                    workspace_id=ws_id,
                    engagement_kind="payroll",
                    supplier_org_id=None,
                    pay_destination_id=None,
                    reimbursement_destination_id=None,
                    started_on=_PINNED.date(),
                    archived_on=None,
                    notes_md="",
                    created_at=_PINNED,
                    updated_at=_PINNED,
                )
            )
            s.commit()
        return engagement_id

    def _seed_booking(
        self,
        factory: sessionmaker[Session],
        *,
        ws_id: str,
        user_id: str,
        engagement_id: str,
        prop_id: str,
        scheduled_start: datetime,
    ) -> str:
        booking_id = new_ulid()
        with factory() as s:
            s.add(
                Booking(
                    id=booking_id,
                    workspace_id=ws_id,
                    work_engagement_id=engagement_id,
                    user_id=user_id,
                    property_id=prop_id,
                    client_org_id=None,
                    status="scheduled",
                    kind="work",
                    pay_basis="scheduled",
                    scheduled_start=scheduled_start,
                    scheduled_end=scheduled_start + timedelta(hours=2),
                    actual_minutes=None,
                    actual_minutes_paid=0,
                    break_seconds=0,
                    notes_md=None,
                    adjusted=False,
                    adjustment_reason=None,
                    pending_amend_minutes=None,
                    pending_amend_reason=None,
                    declined_at=None,
                    declined_reason=None,
                    cancelled_at=None,
                    cancellation_window_hours=24,
                    cancellation_pay_to_worker=True,
                    created_by_actor_kind=None,
                    created_by_actor_id=None,
                    created_at=_PINNED,
                    updated_at=_PINNED,
                    deleted_at=None,
                )
            )
            s.commit()
        return booking_id

    def test_auckland_local_midnight_booking_lands_in_window(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """Local 2026-05-01 00:30 NZST → 2026-04-30 12:30 UTC is included.

        Without the property-local fix the booking falls outside the
        UTC bound ``[2026-05-01 00:00 UTC, 2026-05-15 23:59:59 UTC]``
        and silently vanishes from the worker calendar.
        """
        ws_id, ws_slug, _owner_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-tzakl"
        )
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="me-int-tzakl-w@example.com"
        )
        prop_id = "01HWPROP_AUCKLAND00000000"
        engagement_id = self._seed_property_and_engagement(
            api_factory,
            ws_id=ws_id,
            user_id=worker_id,
            prop_id=prop_id,
            timezone="Pacific/Auckland",
        )
        booking_id = self._seed_booking(
            api_factory,
            ws_id=ws_id,
            user_id=worker_id,
            engagement_id=engagement_id,
            prop_id=prop_id,
            scheduled_start=datetime(2026, 4, 30, 12, 30, tzinfo=UTC),
        )

        worker_ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        client = TestClient(
            _build_app(api_factory, worker_ctx),
            raise_server_exceptions=False,
        )
        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [b["id"] for b in body["bookings"]] == [booking_id]

    def test_la_late_evening_booking_on_to_date_lands_in_window(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """Local 2026-05-15 23:30 PDT → 2026-05-16 06:30 UTC is included."""
        ws_id, ws_slug, _owner_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-tzla"
        )
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="me-int-tzla-w@example.com"
        )
        prop_id = "01HWPROP_LOSANGELES000000"
        engagement_id = self._seed_property_and_engagement(
            api_factory,
            ws_id=ws_id,
            user_id=worker_id,
            prop_id=prop_id,
            timezone="America/Los_Angeles",
        )
        booking_id = self._seed_booking(
            api_factory,
            ws_id=ws_id,
            user_id=worker_id,
            engagement_id=engagement_id,
            prop_id=prop_id,
            scheduled_start=datetime(2026, 5, 16, 6, 30, tzinfo=UTC),
        )

        worker_ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        client = TestClient(
            _build_app(api_factory, worker_ctx),
            raise_server_exceptions=False,
        )
        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [b["id"] for b in body["bookings"]] == [booking_id]

    def test_la_evening_booking_one_day_before_window_is_excluded(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """Local 2026-04-30 23:30 PDT → 2026-05-01 06:30 UTC is excluded.

        The naïve UTC bound would have **kept** this row (it falls
        inside ``[2026-05-01 00:00 UTC, 2026-05-15 23:59:59 UTC]``);
        the property-local filter correctly drops it because in the
        property's timezone the booking is on the day **before** the
        window.
        """
        ws_id, ws_slug, _owner_id = _seed_workspace_with_owner(
            api_factory, slug="me-int-tzla-out"
        )
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="me-int-tzla-out-w@example.com"
        )
        prop_id = "01HWPROP_LAOUT0000000000"
        engagement_id = self._seed_property_and_engagement(
            api_factory,
            ws_id=ws_id,
            user_id=worker_id,
            prop_id=prop_id,
            timezone="America/Los_Angeles",
        )
        self._seed_booking(
            api_factory,
            ws_id=ws_id,
            user_id=worker_id,
            engagement_id=engagement_id,
            prop_id=prop_id,
            scheduled_start=datetime(2026, 5, 1, 6, 30, tzinfo=UTC),
        )

        worker_ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        client = TestClient(
            _build_app(api_factory, worker_ctx),
            raise_server_exceptions=False,
        )
        resp = client.get(
            "/me/schedule",
            params={"from": "2026-05-01", "to": "2026-05-15"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["bookings"] == []
