"""Unit tests for :mod:`app.api.admin.signups` (cd-1h7k).

Signup abuse surfacing is deployment-scoped. Pre-workspace signals
such as burst-rate trips have no workspace to attach to, so the
placeholder lives under ``/admin/api/v1/signups`` and uses the same
surface-invisible 404 wall as the rest of the deployment admin API.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.unit.api.admin._helpers import (
    build_client,
    engine_fixture,
    install_admin_cookie,
    settings_fixture,
)

PINNED = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return settings_fixture("signups")


@pytest.fixture
def engine() -> Iterator[Engine]:
    yield from engine_fixture()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    yield from build_client(settings, session_factory, monkeypatch)


class TestAdminSignups:
    def test_admin_gets_200_empty_envelope(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/signups")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"data": [], "next_cursor": None, "has_more": False}

    def test_admin_gets_suspicious_signal_rows(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _seed_suspicious(
            session_factory,
            kind="distinct_emails_one_ip",
            email_hash="email-a",
            ip_hash="ip-a",
        )

        resp = client.get("/admin/api/v1/signups")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["has_more"] is False
        assert len(body["data"]) == 1
        row = body["data"][0]
        assert row["kind"] == "distinct_emails_one_ip"
        assert row["email_hash"] == "email-a"
        assert row["ip_hash"] == "ip-a"
        assert row["detail"]["scope"] == "ip"

    def test_kind_filter_applies_to_suspicious_rows(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _seed_suspicious(
            session_factory,
            kind="burst_rate",
            email_hash="email-a",
            ip_hash="ip-a",
            offset=1,
        )
        _seed_suspicious(
            session_factory,
            kind="repeat_email",
            email_hash="email-b",
            ip_hash="ip-b",
            offset=2,
        )

        resp = client.get("/admin/api/v1/signups?kind=repeat_email")

        assert resp.status_code == 200, resp.text
        rows = resp.json()["data"]
        assert [row["kind"] for row in rows] == ["repeat_email"]

    def test_unknown_signal_kind_is_not_projected_as_burst_rate(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _seed_suspicious(
            session_factory,
            kind="old_or_corrupt_kind",
            email_hash="email-a",
            ip_hash="ip-a",
        )

        resp = client.get("/admin/api/v1/signups")

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"] == []

    def test_repeat_email_rows_roll_up_by_email_hash(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        _seed_suspicious(
            session_factory,
            kind="repeat_email",
            email_hash="same-email",
            ip_hash="ip-a",
            offset=1,
        )
        _seed_suspicious(
            session_factory,
            kind="repeat_email",
            email_hash="same-email",
            ip_hash="ip-b",
            offset=2,
        )
        _seed_suspicious(
            session_factory,
            kind="repeat_email",
            email_hash="other-email",
            ip_hash="ip-c",
            offset=3,
        )

        resp = client.get("/admin/api/v1/signups?kind=repeat_email")

        assert resp.status_code == 200, resp.text
        rows = resp.json()["data"]
        assert len(rows) == 2
        same = next(row for row in rows if row["email_hash"] == "same-email")
        assert same["detail"]["count"] == 2

    def test_cursor_pages_projected_rollups_without_repeating_raw_rows(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)
        newest_repeat_id = _seed_suspicious(
            session_factory,
            kind="repeat_email",
            email_hash="same-email",
            ip_hash="ip-a",
            offset=10,
        )
        _seed_suspicious(
            session_factory,
            kind="repeat_email",
            email_hash="same-email",
            ip_hash="ip-b",
            offset=1,
        )
        _seed_suspicious(
            session_factory,
            kind="repeat_email",
            email_hash="other-email",
            ip_hash="ip-c",
            offset=5,
        )

        first = client.get("/admin/api/v1/signups?kind=repeat_email&limit=1")
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["has_more"] is True
        assert first_body["next_cursor"] == newest_repeat_id
        assert first_body["data"][0]["email_hash"] == "same-email"
        assert first_body["data"][0]["detail"]["count"] == 2

        second = client.get(
            f"/admin/api/v1/signups?kind=repeat_email&limit=1&cursor={newest_repeat_id}"
        )

        assert second.status_code == 200, second.text
        second_body = second.json()
        assert second_body["data"][0]["email_hash"] == "other-email"
        assert second_body["has_more"] is False

    def test_non_admin_gets_surface_invisible_404(self, client: TestClient) -> None:
        resp = client.get("/admin/api/v1/signups")

        assert resp.status_code == 404, resp.text
        assert resp.json().get("error") == "not_found"

    def test_accepts_kind_cursor_and_limit(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/signups?kind=burst_rate&cursor=abc&limit=25")

        assert resp.status_code == 200, resp.text

    def test_rejects_unknown_kind(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/signups?kind=nope")

        assert resp.status_code == 422

    def test_rejects_limit_above_ceiling(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/signups?limit=501")

        assert resp.status_code == 422

    def test_rejects_non_positive_limit(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        resp = client.get("/admin/api/v1/signups?limit=0")

        assert resp.status_code == 422


class TestAdminSignupsOpenApi:
    def test_operation_shape(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        install_admin_cookie(client, session_factory, settings)

        schema = client.get("/openapi.json").json()
        op = schema["paths"]["/admin/api/v1/signups"]["get"]

        assert op["operationId"] == "admin.signups.list"
        assert "admin" in op.get("tags", [])
        assert op.get("x-cli", {}).get("group") == "admin"
        assert op["x-cli"].get("verb") == "signups-list"
        assert op["x-cli"].get("mutates") is False
        assert op["parameters"][0]["name"] == "kind"
        assert op["parameters"][0]["schema"]["pattern"] == (
            "^(|burst_rate|distinct_emails_one_ip|repeat_email|quota_near_breach)$"
        )


def _seed_suspicious(
    session_factory: sessionmaker[Session],
    *,
    kind: str,
    email_hash: str,
    ip_hash: str,
    offset: int = 0,
) -> str:
    row_id = new_ulid()
    with session_factory() as s, tenant_agnostic():
        s.add(
            AuditLog(
                id=row_id,
                workspace_id=None,
                actor_id="00000000000000000000000000",
                actor_kind="system",
                actor_grant_role="manager",
                actor_was_owner_member=False,
                entity_kind="signup_attempt",
                entity_id=email_hash,
                action="audit.signup.suspicious",
                diff={
                    "kind": kind,
                    "scope": "email" if kind == "repeat_email" else "ip",
                    "reason": f"rate_limited:{kind}",
                    "email_hash": email_hash,
                    "ip_hash": ip_hash,
                },
                correlation_id=new_ulid(),
                scope_kind="deployment",
                via="web",
                created_at=PINNED + timedelta(seconds=offset),
            )
        )
        s.commit()
    return row_id
