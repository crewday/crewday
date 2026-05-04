"""Authz boundary tests for the client portal API."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.api.client.conftest import build_app, ctx, seed_dataset


def test_client_cannot_see_other_client_data(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        data = seed_dataset(session)
        session.commit()
    client = TestClient(
        build_app(factory, ctx(data["workspace_id"], data["client_a"])),
        raise_server_exceptions=False,
    )

    response = client.get(
        "/client/portfolio",
        params={"organization_id": data["org_b"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert [row["id"] for row in body["data"]] == [data["prop_a"]]
    assert data["prop_b"] not in {row["id"] for row in body["data"]}


def test_manager_without_client_grant_is_denied(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        data = seed_dataset(session)
        session.commit()
    client = TestClient(
        build_app(factory, ctx(data["workspace_id"], data["manager"], role="manager")),
        raise_server_exceptions=False,
    )

    response = client.get("/client/portfolio")

    assert response.status_code == 403
    assert response.json()["error"] == "client_portal_forbidden"


def test_property_scoped_client_sees_only_that_property(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        data = seed_dataset(session)
        session.commit()
    client = TestClient(
        build_app(factory, ctx(data["workspace_id"], data["property_client"])),
        raise_server_exceptions=False,
    )

    portfolio = client.get("/client/portfolio")
    invoices = client.get("/client/invoices")

    assert portfolio.status_code == 200
    assert [row["id"] for row in portfolio.json()["data"]] == [data["prop_a"]]
    assert invoices.status_code == 200
    assert invoices.json()["data"] == []
