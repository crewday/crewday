"""Endpoint tests for the client portal API."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.billing.models import Quote
from app.api.pagination import Cursor, encode_page_cursor
from app.util.ulid import new_ulid
from tests.api.client.conftest import PINNED, build_app, ctx, seed_dataset


def test_portal_endpoints_return_redacted_paginated_data_and_audit(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        data = seed_dataset(session)
        session.add(
            Quote(
                id=new_ulid(),
                workspace_id=data["workspace_id"],
                organization_id=data["org_a"],
                property_id=data["prop_a"],
                title="Internal draft quote",
                body_md="do not show",
                total_cents=90000,
                currency="EUR",
                status="draft",
                sent_at=None,
                decided_at=None,
            )
        )
        session.commit()
    client = TestClient(
        build_app(factory, ctx(data["workspace_id"], data["client_a"])),
        raise_server_exceptions=False,
    )

    portfolio = client.get("/client/portfolio", params={"limit": 1})
    hours = client.get("/client/billable-hours")
    invoices = client.get("/client/invoices")
    quotes = client.get("/client/quotes")

    assert portfolio.status_code == 200
    assert portfolio.headers["cache-control"] == "private, max-age=30"
    assert portfolio.json()["has_more"] is False
    assert portfolio.json()["next_cursor"] is None
    assert portfolio.json()["data"] == [
        {
            "id": data["prop_a"],
            "organization_id": data["org_a"],
            "organization_name": "A Client",
            "name": "Alpha Villa",
            "kind": "vacation",
            "address": "Alpha Villa Road",
            "country": "FR",
            "timezone": "Europe/Paris",
            "default_currency": "EUR",
        }
    ]

    assert hours.status_code == 200
    assert hours.json()["data"] == [
        {
            "work_order_id": data["work_order_a"],
            "property_id": data["prop_a"],
            "property_name": "Alpha Villa",
            "week_start": "2026-04-27",
            "hours_decimal": "3.50",
            "total_cents": 35000,
            "currency": "EUR",
        }
    ]
    assert "hourly_rate_cents" not in hours.text
    assert "pay_rule" not in hours.text
    assert "shift_id" not in hours.text
    assert "accrued_cents" not in hours.text

    assert invoices.status_code == 200
    assert invoices.json()["data"] == [
        {
            "id": data["invoice_a"],
            "organization_id": data["org_a"],
            "invoice_number": "A-001",
            "issued_at": "2026-04-29",
            "due_at": "2026-05-29",
            "total_cents": 42000,
            "currency": "EUR",
            "status": "approved",
            "proof_of_payment_file_ids": [],
            "pdf_url": None,
        }
    ]
    assert "secret-storage-hash" not in invoices.text
    assert "supplier internal note" not in invoices.text

    assert quotes.status_code == 200
    assert quotes.json()["data"] == [
        {
            "id": data["quote_a"],
            "organization_id": data["org_a"],
            "property_id": data["prop_a"],
            "title": "Repair quote",
            "total_cents": 42000,
            "currency": "EUR",
            "status": "sent",
            "sent_at": "2026-04-29T12:00:00Z",
            "decided_at": None,
            "accept_url": (
                f"/w/client-api/api/v1/billing/quotes/{data['quote_a']}/accept"
            ),
        }
    ]
    assert "internal cost" not in quotes.text
    assert "Internal draft quote" not in quotes.text

    with factory() as session:
        audit_rows = session.scalars(
            select(AuditLog)
            .where(
                AuditLog.workspace_id == data["workspace_id"],
                AuditLog.action == "client_portal.viewed",
            )
            .order_by(AuditLog.created_at.asc())
        ).all()
    assert [row.diff["slug"] for row in audit_rows] == [
        "portfolio",
        "billable-hours",
        "invoices",
        "quotes",
    ]


def test_portal_pagination_uses_forward_cursor(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        data = seed_dataset(session)
        older_quote_id = new_ulid()
        session.add(
            Quote(
                id=older_quote_id,
                workspace_id=data["workspace_id"],
                organization_id=data["org_a"],
                property_id=data["prop_a"],
                title="Older repair quote",
                body_md="internal",
                total_cents=1000,
                currency="EUR",
                status="sent",
                sent_at=PINNED - timedelta(days=1),
                decided_at=None,
            )
        )
        session.commit()
    client = TestClient(
        build_app(factory, ctx(data["workspace_id"], data["client_a"])),
        raise_server_exceptions=False,
    )

    first = client.get("/client/quotes", params={"limit": 1})

    assert first.status_code == 200
    assert first.json()["has_more"] is True
    assert first.json()["next_cursor"] is not None

    second = client.get(
        "/client/quotes",
        params={"limit": 1, "cursor": first.json()["next_cursor"]},
    )

    assert second.status_code == 200
    assert second.json()["has_more"] is False
    returned = {first.json()["data"][0]["id"], second.json()["data"][0]["id"]}
    assert returned == {data["quote_a"], older_quote_id}


def test_portal_rejects_cursor_from_different_sort_type(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        data = seed_dataset(session)
        session.commit()
    client = TestClient(
        build_app(factory, ctx(data["workspace_id"], data["client_a"])),
        raise_server_exceptions=False,
    )
    cursor = encode_page_cursor(Cursor(last_sort_value=1, last_id_ulid="not-a-quote"))

    response = client.get("/client/quotes", params={"cursor": cursor})

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_cursor"
