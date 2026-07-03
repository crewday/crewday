"""Integration tests for ``POST /bookings/{id}/{amend,decline}`` (cd-juy0i).

Exercises the §09 worker-facing amend / decline write path through
:class:`TestClient` against a live in-memory engine with the production
ORM tenant filter installed. Each test asserts the HTTP boundary
(status, envelope), the persisted ``booking`` row, and — for the
payroll-linkage case — that the amended ``actual_minutes_paid`` drives
the booking pay derivation.

Harness mirrors :mod:`tests.integration.identity.test_me_schedule_api`:
a per-test in-memory SQLite engine + ``Base.metadata.create_all`` with
a ctx-pinning middleware so the tenant filter sees a live
:class:`WorkspaceContext` at SELECT compile time.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.payroll.models import Booking
from app.adapters.db.payroll.repositories import SqlAlchemyBookingPayRepository
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import WorkEngagement
from app.api.deps import current_workspace_context, db_session
from app.api.errors import add_exception_handlers
from app.api.v1.bookings import build_bookings_router
from app.domain.payroll.bookings import derive_booking_pay_entry
from app.tenancy import WorkspaceContext, registry, tenant_agnostic
from app.tenancy.context import ActorGrantRole
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_START = datetime(2026, 5, 6, 9, 0, 0, tzinfo=UTC)


def _load_all_models() -> None:
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
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def api_factory(api_engine: Engine) -> sessionmaker[Session]:
    factory = sessionmaker(bind=api_engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    registry.register("booking")
    registry.register("work_engagement")
    registry.register("audit_log")
    registry.register("role_grant")
    registry.register("permission_group")
    registry.register("permission_group_member")


def _ctx(
    *,
    workspace_id: str,
    workspace_slug: str,
    actor_id: str,
    grant_role: ActorGrantRole = "worker",
    actor_was_owner_member: bool = False,
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
    add_exception_handlers(app)
    app.include_router(build_bookings_router())

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


def _seed_workspace(
    factory: sessionmaker[Session], *, slug: str
) -> tuple[str, str, str]:
    """Seed a workspace + owner. Returns (ws_id, ws_slug, owner_id)."""
    with factory() as s:
        user = bootstrap_user(
            s, email=f"{slug}-owner@example.com", display_name=f"Owner {slug}"
        )
        ws = bootstrap_workspace(s, slug=slug, name=f"WS {slug}", owner_user_id=user.id)
        s.commit()
        return ws.id, ws.slug, user.id


def _seed_worker(factory: sessionmaker[Session], *, ws_id: str, email: str) -> str:
    """Seed a worker user + ``worker`` grant + a payroll work engagement."""
    with factory() as s:
        user = bootstrap_user(s, email=email, display_name=email.split("@")[0])
        with tenant_agnostic():
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
            s.add(
                WorkEngagement(
                    id=new_ulid(),
                    user_id=user.id,
                    workspace_id=ws_id,
                    engagement_kind="payroll",
                    supplier_org_id=None,
                    started_on=date(2026, 1, 1),
                    archived_on=None,
                    notes_md="",
                    created_at=_PINNED,
                    updated_at=_PINNED,
                )
            )
        s.commit()
        return user.id


def _engagement_id(factory: sessionmaker[Session], *, ws_id: str, user_id: str) -> str:
    with factory() as s, tenant_agnostic():
        return s.scalars(
            select(WorkEngagement.id).where(
                WorkEngagement.workspace_id == ws_id,
                WorkEngagement.user_id == user_id,
            )
        ).one()


def _seed_booking(
    factory: sessionmaker[Session],
    *,
    ws_id: str,
    user_id: str,
    status: str = "scheduled",
    minutes: int = 120,
    actual_minutes_paid: int | None = None,
) -> str:
    engagement_id = _engagement_id(factory, ws_id=ws_id, user_id=user_id)
    booking_id = new_ulid()
    with factory() as s, tenant_agnostic():
        s.add(
            Booking(
                id=booking_id,
                workspace_id=ws_id,
                work_engagement_id=engagement_id,
                user_id=user_id,
                property_id=None,
                client_org_id=None,
                status=status,
                kind="work",
                pay_basis="scheduled",
                scheduled_start=_START,
                scheduled_end=_START + timedelta(minutes=minutes),
                actual_minutes=None,
                actual_minutes_paid=(
                    actual_minutes_paid if actual_minutes_paid is not None else minutes
                ),
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
                created_by_actor_kind="user",
                created_by_actor_id=user_id,
                created_at=_PINNED,
                updated_at=_PINNED,
            )
        )
        s.commit()
    return booking_id


def _load_booking(factory: sessionmaker[Session], booking_id: str) -> Booking:
    with factory() as s, tenant_agnostic():
        return s.scalars(select(Booking).where(Booking.id == booking_id)).one()


def _worker_client(
    factory: sessionmaker[Session], *, ws_id: str, ws_slug: str, worker_id: str
) -> TestClient:
    ctx = _ctx(
        workspace_id=ws_id,
        workspace_slug=ws_slug,
        actor_id=worker_id,
        grant_role="worker",
    )
    return TestClient(_build_app(factory, ctx), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Amend
# ---------------------------------------------------------------------------


class TestAmend:
    def test_self_amend_within_threshold_auto_approves(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        ws_id, ws_slug, _owner = _seed_workspace(api_factory, slug="amend-auto")
        worker_id = _seed_worker(api_factory, ws_id=ws_id, email="amend-auto-w@x.com")
        booking_id = _seed_booking(api_factory, ws_id=ws_id, user_id=worker_id)
        client = _worker_client(
            api_factory, ws_id=ws_id, ws_slug=ws_slug, worker_id=worker_id
        )

        # +20 min <= default 30 threshold → auto-approve.
        resp = client.post(
            f"/bookings/{booking_id}/amend",
            json={"actual_minutes": 140, "reason": "Ran a bit long"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["actual_minutes"] == 140
        assert body["actual_minutes_paid"] == 140
        assert body["adjusted"] is True
        assert body["adjustment_reason"] == "Ran a bit long"
        assert body["pending_amend_minutes"] is None

        row = _load_booking(api_factory, booking_id)
        assert row.actual_minutes == 140
        assert row.actual_minutes_paid == 140
        assert row.adjusted is True
        assert row.pending_amend_minutes is None

    def test_self_amend_decrease_auto_approves(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        ws_id, ws_slug, _owner = _seed_workspace(api_factory, slug="amend-down")
        worker_id = _seed_worker(api_factory, ws_id=ws_id, email="amend-down-w@x.com")
        booking_id = _seed_booking(api_factory, ws_id=ws_id, user_id=worker_id)
        client = _worker_client(
            api_factory, ws_id=ws_id, ws_slug=ws_slug, worker_id=worker_id
        )

        resp = client.post(
            f"/bookings/{booking_id}/amend",
            json={"actual_minutes": 90, "reason": "Finished early"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["actual_minutes_paid"] == 90
        assert resp.json()["adjusted"] is True

    def test_self_amend_over_threshold_lands_pending(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        ws_id, ws_slug, _owner = _seed_workspace(api_factory, slug="amend-pending")
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="amend-pending-w@x.com"
        )
        booking_id = _seed_booking(api_factory, ws_id=ws_id, user_id=worker_id)
        client = _worker_client(
            api_factory, ws_id=ws_id, ws_slug=ws_slug, worker_id=worker_id
        )

        # +80 min > 30 threshold → pending, pay held at scheduled value.
        resp = client.post(
            f"/bookings/{booking_id}/amend",
            json={"actual_minutes": 200, "reason": "Place was a disaster"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pending_amend_minutes"] == 200
        assert body["pending_amend_reason"] == "Place was a disaster"
        assert body["actual_minutes_paid"] == 120
        assert body["adjusted"] is False

        row = _load_booking(api_factory, booking_id)
        assert row.pending_amend_minutes == 200
        assert row.actual_minutes_paid == 120

    def test_manager_amend_other_is_unconditional(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        ws_id, ws_slug, owner_id = _seed_workspace(api_factory, slug="amend-mgr")
        worker_id = _seed_worker(api_factory, ws_id=ws_id, email="amend-mgr-w@x.com")
        booking_id = _seed_booking(api_factory, ws_id=ws_id, user_id=worker_id)
        owner_ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=owner_id,
            grant_role="manager",
            actor_was_owner_member=True,
        )
        client = TestClient(
            _build_app(api_factory, owner_ctx), raise_server_exceptions=False
        )

        # +200 min far exceeds the worker threshold but a manager amend
        # (bookings.amend_other) auto-approves with no pending queue.
        resp = client.post(
            f"/bookings/{booking_id}/amend",
            json={"actual_minutes": 320, "reason": "Corrected the record"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["actual_minutes_paid"] == 320
        assert body["pending_amend_minutes"] is None
        assert body["adjusted"] is True

    def test_worker_cannot_amend_another_workers_booking(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        ws_id, ws_slug, _owner = _seed_workspace(api_factory, slug="amend-foreign")
        worker_a = _seed_worker(api_factory, ws_id=ws_id, email="amend-a@x.com")
        worker_b = _seed_worker(api_factory, ws_id=ws_id, email="amend-b@x.com")
        booking_b = _seed_booking(api_factory, ws_id=ws_id, user_id=worker_b)
        client = _worker_client(
            api_factory, ws_id=ws_id, ws_slug=ws_slug, worker_id=worker_a
        )

        resp = client.post(
            f"/bookings/{booking_b}/amend",
            json={"actual_minutes": 130, "reason": "Not mine"},
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["action_key"] == "bookings.amend_other"

        # The foreign booking is untouched.
        row = _load_booking(api_factory, booking_b)
        assert row.actual_minutes_paid == 120
        assert row.adjusted is False

    def test_amend_nonexistent_booking_404(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        ws_id, ws_slug, _owner = _seed_workspace(api_factory, slug="amend-404")
        worker_id = _seed_worker(api_factory, ws_id=ws_id, email="amend-404-w@x.com")
        client = _worker_client(
            api_factory, ws_id=ws_id, ws_slug=ws_slug, worker_id=worker_id
        )

        resp = client.post(
            f"/bookings/{new_ulid()}/amend",
            json={"actual_minutes": 130, "reason": "Ghost"},
        )
        assert resp.status_code == 404, resp.text

    def test_amend_blank_reason_422(self, api_factory: sessionmaker[Session]) -> None:
        ws_id, ws_slug, _owner = _seed_workspace(api_factory, slug="amend-noreason")
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="amend-noreason-w@x.com"
        )
        booking_id = _seed_booking(api_factory, ws_id=ws_id, user_id=worker_id)
        client = _worker_client(
            api_factory, ws_id=ws_id, ws_slug=ws_slug, worker_id=worker_id
        )

        resp = client.post(
            f"/bookings/{booking_id}/amend",
            json={"actual_minutes": 130, "reason": "   "},
        )
        assert resp.status_code == 422, resp.text

    def test_amend_negative_minutes_422(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        ws_id, ws_slug, _owner = _seed_workspace(api_factory, slug="amend-neg")
        worker_id = _seed_worker(api_factory, ws_id=ws_id, email="amend-neg-w@x.com")
        booking_id = _seed_booking(api_factory, ws_id=ws_id, user_id=worker_id)
        client = _worker_client(
            api_factory, ws_id=ws_id, ws_slug=ws_slug, worker_id=worker_id
        )

        resp = client.post(
            f"/bookings/{booking_id}/amend",
            json={"actual_minutes": -5, "reason": "Impossible"},
        )
        assert resp.status_code == 422, resp.text

    def test_amend_drives_payroll_minutes(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """An auto-approved amend on a completed booking drives pay minutes."""
        ws_id, ws_slug, _owner = _seed_workspace(api_factory, slug="amend-pay")
        worker_id = _seed_worker(api_factory, ws_id=ws_id, email="amend-pay-w@x.com")
        booking_id = _seed_booking(
            api_factory, ws_id=ws_id, user_id=worker_id, status="completed"
        )
        client = _worker_client(
            api_factory, ws_id=ws_id, ws_slug=ws_slug, worker_id=worker_id
        )

        resp = client.post(
            f"/bookings/{booking_id}/amend",
            json={"actual_minutes": 145, "reason": "Stayed late"},
        )
        assert resp.status_code == 200, resp.text

        with api_factory() as s, tenant_agnostic():
            repo = SqlAlchemyBookingPayRepository(s)
            rows = repo.list_pay_bearing_bookings(
                workspace_id=ws_id,
                starts_at=_START - timedelta(days=1),
                ends_at=_START + timedelta(days=1),
            )
        matched = [r for r in rows if r.id == booking_id]
        assert len(matched) == 1
        entry = derive_booking_pay_entry(matched[0])
        assert entry.minutes == 145


# ---------------------------------------------------------------------------
# Decline
# ---------------------------------------------------------------------------


class TestDecline:
    def test_worker_declines_own_scheduled_booking(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        ws_id, ws_slug, _owner = _seed_workspace(api_factory, slug="decline-ok")
        worker_id = _seed_worker(api_factory, ws_id=ws_id, email="decline-ok-w@x.com")
        booking_id = _seed_booking(api_factory, ws_id=ws_id, user_id=worker_id)
        client = _worker_client(
            api_factory, ws_id=ws_id, ws_slug=ws_slug, worker_id=worker_id
        )

        resp = client.post(
            f"/bookings/{booking_id}/decline",
            json={"reason": "Off sick"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Returned to the manager queue for reassignment.
        assert body["status"] == "pending_approval"
        assert body["declined_reason"] == "Off sick"
        assert body["declined_at"] is not None

        row = _load_booking(api_factory, booking_id)
        assert row.status == "pending_approval"
        assert row.declined_at is not None
        assert row.declined_reason == "Off sick"

    def test_worker_cannot_decline_another_workers_booking(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        ws_id, ws_slug, _owner = _seed_workspace(api_factory, slug="decline-foreign")
        worker_a = _seed_worker(api_factory, ws_id=ws_id, email="decline-a@x.com")
        worker_b = _seed_worker(api_factory, ws_id=ws_id, email="decline-b@x.com")
        booking_b = _seed_booking(api_factory, ws_id=ws_id, user_id=worker_b)
        client = _worker_client(
            api_factory, ws_id=ws_id, ws_slug=ws_slug, worker_id=worker_a
        )

        resp = client.post(
            f"/bookings/{booking_b}/decline",
            json={"reason": "Not mine"},
        )
        assert resp.status_code == 403, resp.text

        row = _load_booking(api_factory, booking_b)
        assert row.status == "scheduled"
        assert row.declined_at is None

    def test_decline_non_scheduled_booking_409(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        ws_id, ws_slug, _owner = _seed_workspace(api_factory, slug="decline-done")
        worker_id = _seed_worker(api_factory, ws_id=ws_id, email="decline-done-w@x.com")
        booking_id = _seed_booking(
            api_factory, ws_id=ws_id, user_id=worker_id, status="completed"
        )
        client = _worker_client(
            api_factory, ws_id=ws_id, ws_slug=ws_slug, worker_id=worker_id
        )

        resp = client.post(
            f"/bookings/{booking_id}/decline",
            json={"reason": "Too late"},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["booking_status"] == "completed"

    def test_decline_nonexistent_booking_404(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        ws_id, ws_slug, _owner = _seed_workspace(api_factory, slug="decline-404")
        worker_id = _seed_worker(api_factory, ws_id=ws_id, email="decline-404-w@x.com")
        client = _worker_client(
            api_factory, ws_id=ws_id, ws_slug=ws_slug, worker_id=worker_id
        )

        resp = client.post(
            f"/bookings/{new_ulid()}/decline",
            json={"reason": "Ghost"},
        )
        assert resp.status_code == 404, resp.text
