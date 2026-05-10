"""HTTP tests for the workspace settings surface."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.api.v1.settings import VerifyOwnershipConsumeBody, build_settings_router
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client, ctx_for

pytest_plugins = ("tests.unit.api.v1.identity.conftest",)


def _assert_problem_json(response: Response) -> dict[str, object]:
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "https://crewday.dev/errors/validation"
    assert body["title"] == "Validation error"
    assert body["status"] == 422
    return body


def _seed(factory: sessionmaker[Session]) -> tuple[WorkspaceContext, str]:
    with factory() as session:
        owner = bootstrap_user(
            session,
            email="settings-owner@example.com",
            display_name="Settings Owner",
        )
        workspace = bootstrap_workspace(
            session,
            slug="settings",
            name="Settings House",
            owner_user_id=owner.id,
        )
        workspace.default_timezone = "Europe/Paris"
        workspace.default_locale = "fr-FR"
        workspace.default_currency = "EUR"
        workspace.settings_json = {
            "evidence.policy": "require",
            "workspace.default_country": "FR",
        }
        session.commit()
        ctx = ctx_for(
            workspace_id=workspace.id,
            workspace_slug=workspace.slug,
            actor_id=owner.id,
            grant_role="manager",
            actor_was_owner_member=True,
        )
        return ctx, workspace.id


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", build_settings_router())], factory, ctx)


class TestVerifyOwnershipConsumeBodyTokenLength:
    """Workspace ownership verify tokens use the same bounded body shape."""

    def test_max_length_4096_accepts_boundary(self) -> None:
        body = VerifyOwnershipConsumeBody(token="x" * 4096)
        assert len(body.token) == 4096

    def test_token_above_4096_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            VerifyOwnershipConsumeBody(token="x" * 4097)

        errors = excinfo.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("token",)
        assert errors[0]["type"] == "string_too_long"

    def test_empty_token_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            VerifyOwnershipConsumeBody(token="")

        errors = excinfo.value.errors()
        assert len(errors) == 1
        assert errors[0]["loc"] == ("token",)
        assert errors[0]["type"] == "string_too_short"


def test_settings_read_merges_workspace_values_with_catalog_defaults(
    factory: sessionmaker[Session],
) -> None:
    ctx, _workspace_id = _seed(factory)
    client = _client(ctx, factory)

    response = client.get("/settings")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"] == {
        "slug": "settings",
        "name": "Settings House",
        "display_name": "Settings House",
        "timezone": "Europe/Paris",
        "currency": "EUR",
        "country": "FR",
        "default_locale": "fr-FR",
    }
    assert body["defaults"]["evidence.policy"] == "require"
    assert body["defaults"]["tasks.checklist_required"] is False
    assert body["policy"]["approvals"]["always_gated"]


def test_settings_basics_patch_updates_display_name_and_audits(
    factory: sessionmaker[Session],
) -> None:
    ctx, workspace_id = _seed(factory)
    client = _client(ctx, factory)

    response = client.patch(
        "/settings/basics",
        json={"display_name": "Settings Cottage"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"]["slug"] == "settings"
    assert body["meta"]["display_name"] == "Settings Cottage"
    assert body["meta"]["name"] == "Settings Cottage"

    with factory() as session:
        audit = (
            session.query(AuditLog)
            .filter_by(
                workspace_id=workspace_id,
                action="workspace.basics_updated",
            )
            .one()
        )
        assert audit.diff["before"] == {"name": "Settings House"}
        assert audit.diff["after"]["name"] == "Settings Cottage"


def test_settings_basics_patch_rejects_invalid_display_name(
    factory: sessionmaker[Session],
) -> None:
    ctx, _workspace_id = _seed(factory)
    client = _client(ctx, factory)

    response = client.patch("/settings/basics", json={"display_name": "   "})

    assert response.status_code == 422
    body = _assert_problem_json(response)
    assert body["error"] == "workspace_basics_invalid"
    assert body["field"] == "name"


def test_settings_basics_patch_denies_non_owner_manager(
    factory: sessionmaker[Session],
) -> None:
    owner_ctx, workspace_id = _seed(factory)
    with factory() as session:
        manager = bootstrap_user(
            session,
            email="settings-manager@example.com",
            display_name="Settings Manager",
        )
        session.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=manager.id,
                grant_role="manager",
                scope_property_id=None,
                created_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
                created_by_user_id=owner_ctx.actor_id,
            )
        )
        session.commit()
        manager_id = manager.id
    manager_ctx = ctx_for(
        workspace_id=owner_ctx.workspace_id,
        workspace_slug=owner_ctx.workspace_slug,
        actor_id=manager_id,
        grant_role="manager",
        actor_was_owner_member=False,
    )
    client = _client(manager_ctx, factory)

    response = client.patch(
        "/settings/basics",
        json={"display_name": "Manager Rename"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "owners_only"


def test_settings_patch_updates_known_keys_and_audits(
    factory: sessionmaker[Session],
) -> None:
    ctx, workspace_id = _seed(factory)
    client = _client(ctx, factory)

    response = client.patch(
        "/settings",
        json={
            "evidence.policy": "forbid",
            "tasks.checklist_required": True,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["defaults"]["evidence.policy"] == "forbid"
    assert body["defaults"]["tasks.checklist_required"] is True

    with factory() as session:
        audit = (
            session.query(AuditLog)
            .filter_by(
                workspace_id=workspace_id,
                action="workspace.settings_updated",
            )
            .one()
        )
        assert audit.diff["after"] == {
            "evidence.policy": "forbid",
            "tasks.checklist_required": True,
        }


def test_settings_patch_rejects_unknown_keys(factory: sessionmaker[Session]) -> None:
    ctx, _workspace_id = _seed(factory)
    client = _client(ctx, factory)

    response = client.patch("/settings", json={"unknown.key": True})

    assert response.status_code == 422
    body = _assert_problem_json(response)
    assert body["error"] == "unknown_setting"
    assert body["key"] == "unknown.key"


def test_settings_patch_rejects_wrong_value_type(
    factory: sessionmaker[Session],
) -> None:
    ctx, _workspace_id = _seed(factory)
    client = _client(ctx, factory)

    response = client.patch("/settings", json={"tasks.checklist_required": "yes"})

    assert response.status_code == 422
    body = _assert_problem_json(response)
    assert body["error"] == "setting_type_invalid"
    assert body["key"] == "tasks.checklist_required"
    assert body["expected"] == "bool"
