"""Integration tests for the demo first-visit mint flow."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.demo.models import DemoWorkspace
from app.adapters.db.expenses.models import ExpenseClaim
from app.adapters.db.session import UnitOfWorkImpl
from app.adapters.db.stays.models import Reservation
from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.factory import create_app
from app.config import Settings
from app.demo import demo_cookie_name, mint_demo_cookie

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def demo_client(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setattr(
        "app.api.factory.make_uow", lambda: UnitOfWorkImpl(session_factory)
    )
    monkeypatch.setattr(
        "app.api.deps.make_uow", lambda: UnitOfWorkImpl(session_factory)
    )
    monkeypatch.setattr(
        "app.tenancy.middleware.make_uow", lambda: UnitOfWorkImpl(session_factory)
    )
    settings = _settings()
    monkeypatch.setattr("app.tenancy.middleware.get_settings", lambda: settings)
    app = create_app(settings=settings)
    with TestClient(
        app,
        base_url="https://demo.crew.day",
        raise_server_exceptions=False,
    ) as client:
        yield client


def test_cold_get_rental_manager_mints_cookie_and_seeds_rows(
    demo_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    before = _workspace_count(session_factory)

    response = demo_client.get(
        "/app?scenario=rental-manager",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/w/demo-rental-manager-")
    assert response.headers["location"].endswith("/tasks")
    set_cookie = response.headers["set-cookie"]
    assert f"{demo_cookie_name('rental-manager')}=" in set_cookie
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=None" in set_cookie
    assert "Path=/" in set_cookie
    assert "Partitioned" in set_cookie
    assert "Max-Age=2592000" in set_cookie

    slug = response.headers["location"].split("/")[2]
    with session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == slug))
        assert workspace is not None
        assert _count_all(session, Workspace) == before + 1
        assert _count_all(session, DemoWorkspace) >= 1
        assert _count_for_workspace(session, Reservation, workspace.id) >= 2
        assert _count_for_workspace(session, ExpenseClaim, workspace.id) >= 3
        titles = set(
            session.scalars(
                select(Occurrence.title).where(Occurrence.workspace_id == workspace.id)
            )
        )
        assert "Turnover - Apt 3B" in titles
        assert "Fix loose cupboard handle" in titles


def test_tampered_cookie_is_absent_and_remints(
    demo_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    first = demo_client.get("/app?scenario=rental-manager", follow_redirects=False)
    cookie_value = _cookie_value(first.headers["set-cookie"], "rental-manager")
    before = _workspace_count(session_factory)
    demo_client.cookies.set(
        demo_cookie_name("rental-manager"),
        f"{cookie_value}x",
        domain="demo.crew.day",
        path="/",
    )

    response = demo_client.get(
        "/app?scenario=rental-manager",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert f"{demo_cookie_name('rental-manager')}=" in response.headers["set-cookie"]
    assert _workspace_count(session_factory) == before + 1


def test_stale_key_cookie_is_absent_and_remints(
    demo_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    first = demo_client.get("/app?scenario=rental-manager", follow_redirects=False)
    slug = first.headers["location"].split("/")[2]
    with session_factory() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == slug))
        assert workspace is not None
        persona_user_id = session.scalar(
            select(UserWorkspace.user_id).where(
                UserWorkspace.workspace_id == workspace.id
            )
        )
        assert persona_user_id is not None
        old_key_cookie = mint_demo_cookie(
            SecretStr("previous-demo-cookie-key"),
            scenario_key="rental-manager",
            workspace_id=workspace.id,
            persona_user_id=persona_user_id,
        )
    before = _workspace_count(session_factory)
    demo_client.cookies.set(
        demo_cookie_name("rental-manager"),
        old_key_cookie,
        domain="demo.crew.day",
        path="/",
    )

    response = demo_client.get(
        "/app?scenario=rental-manager",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert f"{demo_cookie_name('rental-manager')}=" in response.headers["set-cookie"]
    assert _workspace_count(session_factory) == before + 1


def test_demo_cookie_authenticates_followup_workspace_requests(
    demo_client: TestClient,
) -> None:
    first = demo_client.get("/app?scenario=rental-manager", follow_redirects=False)
    slug = first.headers["location"].split("/")[2]
    demo_client.cookies.set(
        demo_cookie_name("rental-manager"),
        _cookie_value(first.headers["set-cookie"], "rental-manager"),
        domain="demo.crew.day",
        path="/",
    )

    response = demo_client.get(f"/w/{slug}/api/v1/tasks")

    assert response.status_code == 200
    titles = {item["title"] for item in response.json()["data"]}
    assert "Turnover - Apt 3B" in titles


def test_two_scenarios_create_distinct_cookies_and_workspaces(
    demo_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    before = _workspace_count(session_factory)

    rental = demo_client.get("/app?scenario=rental-manager", follow_redirects=False)
    villa = demo_client.get("/app?scenario=villa-owner", follow_redirects=False)

    assert rental.status_code == 303
    assert villa.status_code == 303
    assert demo_cookie_name("rental-manager") in rental.headers["set-cookie"]
    assert demo_cookie_name("villa-owner") in villa.headers["set-cookie"]
    assert (
        rental.headers["location"].split("/")[2]
        != villa.headers["location"].split("/")[2]
    )
    assert _workspace_count(session_factory) == before + 2


def _settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-demo-root-key"),
        demo_cookie_key=SecretStr("integration-demo-cookie-key"),
        demo_mode=True,
        demo_db_denylist=[],
        public_url="https://demo.crew.day",
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="external",
        smtp_host=None,
        smtp_port=587,
        smtp_from=None,
        smtp_use_tls=False,
        log_level="INFO",
        cors_allow_origins=[],
        profile="prod",
        vite_dev_url="http://127.0.0.1:5173",
        metrics_enabled=False,
    )


def _workspace_count(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return _count_all(session, Workspace)


def _count_all(
    session: Session,
    model: type[Workspace] | type[DemoWorkspace],
) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _count_for_workspace(
    session: Session,
    model: type[Reservation] | type[ExpenseClaim],
    workspace_id: str,
) -> int:
    stmt = (
        select(func.count())
        .select_from(model)
        .where(model.workspace_id == workspace_id)
    )
    return session.scalar(stmt) or 0


def _cookie_value(set_cookie: str, scenario_key: str) -> str:
    prefix = f"{demo_cookie_name(scenario_key)}="
    start = set_cookie.index(prefix) + len(prefix)
    end = set_cookie.index(";", start)
    return set_cookie[start:end]
