"""Integration tests for ``GET /scheduler/calendar``."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import JSONResponse

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.availability.models import UserWeeklyAvailability
from app.adapters.db.base import Base
from app.adapters.db.billing.models import Organization
from app.adapters.db.places.models import (
    Property,
    PropertyWorkRoleAssignment,
    PropertyWorkspace,
)
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import UserWorkRole, WorkRole
from app.api.deps import current_workspace_context, db_session
from app.api.errors import _handle_domain_error
from app.api.v1.scheduler import build_scheduler_router
from app.domain.errors import DomainError
from app.tenancy import WorkspaceContext
from app.tenancy.context import ActorGrantRole
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)


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
def scheduler_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def scheduler_factory(scheduler_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=scheduler_engine, expire_on_commit=False, class_=Session)


def _ctx(
    *,
    workspace_id: str,
    workspace_slug: str,
    actor_id: str,
    grant_role: ActorGrantRole = "manager",
    actor_was_owner_member: bool = True,
) -> WorkspaceContext:
    return build_workspace_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=actor_was_owner_member,
    )


def _client(
    scheduler_factory: sessionmaker[Session], ctx: WorkspaceContext
) -> TestClient:
    app = FastAPI()
    app.include_router(build_scheduler_router())

    async def _on_domain_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, DomainError)
        return _handle_domain_error(request, exc)

    app.add_exception_handler(DomainError, _on_domain_error)

    def _override_ctx() -> WorkspaceContext:
        return ctx

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=scheduler_factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db
    return TestClient(app, raise_server_exceptions=False)


def _grant(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
    role: str,
    scope_property_id: str | None = None,
    binding_org_id: str | None = None,
) -> None:
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=role,
            scope_kind="workspace",
            scope_property_id=scope_property_id,
            binding_org_id=binding_org_id,
            created_at=_NOW,
            created_by_user_id=None,
        )
    )


def _property(
    session: Session,
    *,
    workspace_id: str,
    name: str,
    timezone: str = "Europe/Paris",
    client_org_id: str | None = None,
) -> str:
    prop = Property(
        id=new_ulid(),
        name=name,
        kind="residence",
        address=f"{name} address",
        address_json={"city": "Antibes", "country": "FR"},
        country="FR",
        locale=None,
        default_currency=None,
        timezone=timezone,
        lat=None,
        lon=None,
        client_org_id=client_org_id,
        owner_user_id=None,
        tags_json=[],
        welcome_defaults_json={},
        property_notes_md="",
        created_at=_NOW,
        updated_at=_NOW,
        deleted_at=None,
    )
    session.add(prop)
    session.flush()
    session.add(
        PropertyWorkspace(
            property_id=prop.id,
            workspace_id=workspace_id,
            label=name,
            membership_role="owner_workspace",
            share_guest_identity=False,
            auto_shift_from_occurrence=False,
            status="active",
            created_at=_NOW,
        )
    )
    session.flush()
    return prop.id


def _organization(
    session: Session,
    *,
    workspace_id: str,
    display_name: str,
) -> str:
    org = Organization(
        id=new_ulid(),
        workspace_id=workspace_id,
        kind="client",
        display_name=display_name,
        billing_address={},
        tax_id=None,
        default_currency="EUR",
        contact_email=None,
        contact_phone=None,
        notes_md=None,
        created_at=_NOW,
        archived_at=None,
    )
    session.add(org)
    session.flush()
    return org.id


def _role_assignment(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
    property_id: str,
    role_name: str = "Cleaner",
) -> str:
    work_role = WorkRole(
        id=new_ulid(),
        workspace_id=workspace_id,
        key=f"{role_name.lower()}-{new_ulid()[-6:]}",
        name=role_name,
        description_md="",
        default_settings_json={},
        icon_name="BrushCleaning",
        created_at=_NOW,
        deleted_at=None,
    )
    session.add(work_role)
    session.flush()
    user_work_role = UserWorkRole(
        id=new_ulid(),
        user_id=user_id,
        workspace_id=workspace_id,
        work_role_id=work_role.id,
        started_on=date(2026, 1, 1),
        ended_on=None,
        pay_rule_id=None,
        created_at=_NOW,
        deleted_at=None,
    )
    session.add(user_work_role)
    session.flush()
    assignment = PropertyWorkRoleAssignment(
        id=new_ulid(),
        workspace_id=workspace_id,
        user_work_role_id=user_work_role.id,
        property_id=property_id,
        schedule_ruleset_id=None,
        property_pay_rule_id=None,
        created_at=_NOW,
        updated_at=_NOW,
        deleted_at=None,
    )
    session.add(assignment)
    session.flush()
    return assignment.id


def _weekly(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
    weekday: int = 0,
    starts: time = time(9, 0),
    ends: time = time(13, 0),
) -> None:
    session.add(
        UserWeeklyAvailability(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            weekday=weekday,
            starts_local=starts,
            ends_local=ends,
            updated_at=_NOW,
        )
    )


def _task(
    session: Session,
    *,
    workspace_id: str,
    property_id: str,
    user_id: str,
    title: str,
    starts_at: datetime = datetime(2026, 5, 4, 10, 0, tzinfo=UTC),
    scheduled_for_local: str | None = None,
) -> str:
    local = scheduled_for_local or starts_at.replace(tzinfo=None).isoformat()
    task = Occurrence(
        id=new_ulid(),
        workspace_id=workspace_id,
        schedule_id=None,
        template_id=None,
        property_id=property_id,
        assignee_user_id=user_id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        scheduled_for_local=local,
        originally_scheduled_for=local,
        state="pending",
        overdue_since=None,
        completed_at=None,
        completed_by_user_id=None,
        reviewer_user_id=None,
        reviewed_at=None,
        cancellation_reason=None,
        title=title,
        description_md=None,
        priority="normal",
        photo_evidence="disabled",
        duration_minutes=60,
        area_id=None,
        unit_id=None,
        expected_role_id=None,
        linked_instruction_ids=[],
        inventory_consumption_json={},
        is_personal=False,
        created_by_user_id=None,
        created_at=_NOW,
    )
    session.add(task)
    return task.id


def _seed_workspace(scheduler_factory: sessionmaker[Session]) -> tuple[str, str, str]:
    with scheduler_factory() as session:
        owner = bootstrap_user(
            session,
            email="scheduler-owner@example.com",
            display_name="Owner Manager",
        )
        workspace = bootstrap_workspace(
            session,
            slug="scheduler-api",
            name="Scheduler API",
            owner_user_id=owner.id,
        )
        session.commit()
        return workspace.id, workspace.slug, owner.id


class TestSchedulerCalendar:
    def test_manager_feed_returns_assignments_slots_tasks_properties_and_users(
        self, scheduler_factory: sessionmaker[Session]
    ) -> None:
        ws_id, slug, owner_id = _seed_workspace(scheduler_factory)
        with scheduler_factory() as session:
            worker = bootstrap_user(
                session,
                email="ada.worker@example.com",
                display_name="Ada Lovelace",
            )
            prop_id = _property(session, workspace_id=ws_id, name="Villa Sud")
            assignment_id = _role_assignment(
                session, workspace_id=ws_id, user_id=worker.id, property_id=prop_id
            )
            _weekly(session, workspace_id=ws_id, user_id=worker.id)
            task_id = _task(
                session,
                workspace_id=ws_id,
                property_id=prop_id,
                user_id=worker.id,
                title="Pool check",
            )
            session.commit()

        client = _client(
            scheduler_factory,
            _ctx(workspace_id=ws_id, workspace_slug=slug, actor_id=owner_id),
        )
        resp = client.get(
            "/scheduler/calendar", params={"from": "2026-05-04", "to": "2026-05-10"}
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["window"] == {"from": "2026-05-04", "to": "2026-05-10"}
        assert body["stay_bundles"] == []
        assert [row["id"] for row in body["properties"]] == [prop_id]
        assert body["assignments"][0]["id"] == assignment_id
        ruleset_id = body["assignments"][0]["schedule_ruleset_id"]
        assert body["rulesets"] == [
            {"id": ruleset_id, "workspace_id": ws_id, "name": "Cleaner rota"}
        ]
        assert body["slots"] == [
            {
                "id": f"{assignment_id}:0",
                "schedule_ruleset_id": ruleset_id,
                "weekday": 0,
                "starts_local": "09:00",
                "ends_local": "13:00",
            }
        ]
        assert body["tasks"][0]["id"] == task_id
        assert body["tasks"][0]["scheduled_start"] == "2026-05-04T10:00:00"
        assert body["users"] == [
            {
                "id": worker.id,
                "first_name": "Ada",
                "display_name": "Ada Lovelace",
                "work_role": "Cleaner",
            }
        ]

    def test_client_feed_scopes_properties_and_redacts_staff_identity(
        self, scheduler_factory: sessionmaker[Session]
    ) -> None:
        ws_id, slug, _owner_id = _seed_workspace(scheduler_factory)
        with scheduler_factory() as session:
            client_user = bootstrap_user(
                session,
                email="client@example.com",
                display_name="Client User",
            )
            worker_a = bootstrap_user(
                session,
                email="ada.worker@example.com",
                display_name="Ada Lovelace",
            )
            worker_b = bootstrap_user(
                session,
                email="grace.worker@example.com",
                display_name="Grace Hopper",
            )
            org_a = _organization(
                session, workspace_id=ws_id, display_name="Client Org"
            )
            org_b = _organization(session, workspace_id=ws_id, display_name="Other Org")
            prop_a = _property(
                session, workspace_id=ws_id, name="Client Villa", client_org_id=org_a
            )
            prop_b = _property(
                session, workspace_id=ws_id, name="Other Villa", client_org_id=org_b
            )
            _grant(
                session,
                workspace_id=ws_id,
                user_id=client_user.id,
                role="client",
                binding_org_id=org_a,
            )
            _role_assignment(
                session, workspace_id=ws_id, user_id=worker_a.id, property_id=prop_a
            )
            _role_assignment(
                session, workspace_id=ws_id, user_id=worker_b.id, property_id=prop_b
            )
            _weekly(session, workspace_id=ws_id, user_id=worker_a.id)
            _weekly(session, workspace_id=ws_id, user_id=worker_b.id)
            _task(
                session,
                workspace_id=ws_id,
                property_id=prop_a,
                user_id=worker_a.id,
                title="Visible turn",
            )
            _task(
                session,
                workspace_id=ws_id,
                property_id=prop_b,
                user_id=worker_b.id,
                title="Hidden turn",
            )
            session.commit()

        client = _client(
            scheduler_factory,
            _ctx(
                workspace_id=ws_id,
                workspace_slug=slug,
                actor_id=client_user.id,
                grant_role="client",
                actor_was_owner_member=False,
            ),
        )
        resp = client.get(
            "/scheduler/calendar", params={"from": "2026-05-04", "to": "2026-05-10"}
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [row["id"] for row in body["properties"]] == [prop_a]
        assert {row["property_id"] for row in body["assignments"]} == {prop_a}
        assert {row["property_id"] for row in body["tasks"]} == {prop_a}
        public_user_id = body["users"][0]["id"]
        assert public_user_id.startswith("staff:")
        assert body["users"] == [
            {
                "id": public_user_id,
                "first_name": "Ada",
                "display_name": None,
                "work_role": "Cleaner",
            }
        ]
        encoded = json.dumps(body)
        assert worker_a.id not in encoded
        assert worker_b.id not in encoded
        assert "Ada Lovelace" not in encoded
        assert "ada.worker@example.com" not in encoded
        assert prop_b not in encoded
        assert "Hidden turn" not in encoded

    def test_worker_feed_is_self_only(
        self, scheduler_factory: sessionmaker[Session]
    ) -> None:
        ws_id, slug, _owner_id = _seed_workspace(scheduler_factory)
        with scheduler_factory() as session:
            worker_a = bootstrap_user(
                session, email="self@example.com", display_name="Self Worker"
            )
            worker_b = bootstrap_user(
                session, email="other@example.com", display_name="Other Worker"
            )
            _grant(session, workspace_id=ws_id, user_id=worker_a.id, role="worker")
            prop_id = _property(session, workspace_id=ws_id, name="Shared Villa")
            _role_assignment(
                session, workspace_id=ws_id, user_id=worker_a.id, property_id=prop_id
            )
            _role_assignment(
                session, workspace_id=ws_id, user_id=worker_b.id, property_id=prop_id
            )
            _weekly(session, workspace_id=ws_id, user_id=worker_a.id)
            _weekly(session, workspace_id=ws_id, user_id=worker_b.id)
            visible_task = _task(
                session,
                workspace_id=ws_id,
                property_id=prop_id,
                user_id=worker_a.id,
                title="Mine",
            )
            _task(
                session,
                workspace_id=ws_id,
                property_id=prop_id,
                user_id=worker_b.id,
                title="Theirs",
            )
            session.commit()

        client = _client(
            scheduler_factory,
            _ctx(
                workspace_id=ws_id,
                workspace_slug=slug,
                actor_id=worker_a.id,
                grant_role="worker",
                actor_was_owner_member=False,
            ),
        )
        resp = client.get(
            "/scheduler/calendar", params={"from": "2026-05-04", "to": "2026-05-10"}
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [row["id"] for row in body["users"]] == [worker_a.id]
        assert [row["id"] for row in body["tasks"]] == [visible_task]
        assert {row["user_id"] for row in body["assignments"]} == {worker_a.id}

    def test_filters_narrow_properties_users_roles_and_tasks(
        self, scheduler_factory: sessionmaker[Session]
    ) -> None:
        ws_id, slug, owner_id = _seed_workspace(scheduler_factory)
        with scheduler_factory() as session:
            worker_a = bootstrap_user(
                session, email="filter-a@example.com", display_name="Filter A"
            )
            worker_b = bootstrap_user(
                session, email="filter-b@example.com", display_name="Filter B"
            )
            prop_a = _property(session, workspace_id=ws_id, name="Filter Villa A")
            prop_b = _property(session, workspace_id=ws_id, name="Filter Villa B")
            _role_assignment(
                session,
                workspace_id=ws_id,
                user_id=worker_a.id,
                property_id=prop_a,
                role_name="Cleaner",
            )
            _role_assignment(
                session,
                workspace_id=ws_id,
                user_id=worker_b.id,
                property_id=prop_b,
                role_name="Cook",
            )
            _weekly(session, workspace_id=ws_id, user_id=worker_a.id)
            _weekly(session, workspace_id=ws_id, user_id=worker_b.id)
            task_a = _task(
                session,
                workspace_id=ws_id,
                property_id=prop_a,
                user_id=worker_a.id,
                title="Visible filtered task",
            )
            _task(
                session,
                workspace_id=ws_id,
                property_id=prop_b,
                user_id=worker_b.id,
                title="Hidden filtered task",
            )
            session.commit()

        client = _client(
            scheduler_factory,
            _ctx(workspace_id=ws_id, workspace_slug=slug, actor_id=owner_id),
        )
        resp = client.get(
            "/scheduler/calendar",
            params={
                "from": "2026-05-04",
                "to": "2026-05-10",
                "property": prop_a,
                "user": worker_a.id,
                "role": "Cleaner",
            },
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [row["id"] for row in body["properties"]] == [prop_a]
        assert [row["id"] for row in body["users"]] == [worker_a.id]
        assert [row["id"] for row in body["tasks"]] == [task_a]
        assert {row["property_id"] for row in body["assignments"]} == {prop_a}

    def test_task_window_uses_property_local_scheduled_date(
        self, scheduler_factory: sessionmaker[Session]
    ) -> None:
        ws_id, slug, owner_id = _seed_workspace(scheduler_factory)
        with scheduler_factory() as session:
            worker = bootstrap_user(
                session,
                email="timezone-worker@example.com",
                display_name="Timezone Worker",
            )
            prop_id = _property(
                session,
                workspace_id=ws_id,
                name="Kiritimati Villa",
                timezone="Pacific/Kiritimati",
            )
            _role_assignment(
                session, workspace_id=ws_id, user_id=worker.id, property_id=prop_id
            )
            task_id = _task(
                session,
                workspace_id=ws_id,
                property_id=prop_id,
                user_id=worker.id,
                title="Local midnight task",
                starts_at=datetime(2026, 5, 3, 10, 30, tzinfo=UTC),
                scheduled_for_local="2026-05-04T00:30:00",
            )
            session.commit()

        client = _client(
            scheduler_factory,
            _ctx(workspace_id=ws_id, workspace_slug=slug, actor_id=owner_id),
        )
        resp = client.get(
            "/scheduler/calendar", params={"from": "2026-05-04", "to": "2026-05-04"}
        )

        assert resp.status_code == 200, resp.text
        assert [row["id"] for row in resp.json()["tasks"]] == [task_id]

    def test_backwards_window_is_rejected(
        self, scheduler_factory: sessionmaker[Session]
    ) -> None:
        ws_id, slug, owner_id = _seed_workspace(scheduler_factory)
        client = _client(
            scheduler_factory,
            _ctx(workspace_id=ws_id, workspace_slug=slug, actor_id=owner_id),
        )

        resp = client.get(
            "/scheduler/calendar", params={"from": "2026-05-10", "to": "2026-05-04"}
        )

        assert resp.status_code == 422
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["error"] == "invalid_field"
        assert body["field"] == "to"
        assert body["message"] == "to must be on or after from"

    def test_guest_role_is_forbidden(
        self, scheduler_factory: sessionmaker[Session]
    ) -> None:
        ws_id, slug, owner_id = _seed_workspace(scheduler_factory)
        client = _client(
            scheduler_factory,
            _ctx(
                workspace_id=ws_id,
                workspace_slug=slug,
                actor_id=owner_id,
                grant_role="guest",
                actor_was_owner_member=False,
            ),
        )

        resp = client.get(
            "/scheduler/calendar", params={"from": "2026-05-04", "to": "2026-05-10"}
        )

        assert resp.status_code == 403
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["error"] == "permission_denied"
        assert body["action_key"] == "scheduler.calendar"

    def test_openapi_lists_scheduler_calendar(
        self, scheduler_factory: sessionmaker[Session]
    ) -> None:
        ws_id, slug, owner_id = _seed_workspace(scheduler_factory)
        client = _client(
            scheduler_factory,
            _ctx(workspace_id=ws_id, workspace_slug=slug, actor_id=owner_id),
        )

        schema = client.get("/openapi.json").json()

        assert "/scheduler/calendar" in schema["paths"]
        params = schema["paths"]["/scheduler/calendar"]["get"]["parameters"]
        assert [param["name"] for param in params] == [
            "from",
            "to",
            "user",
            "property",
            "role",
        ]
