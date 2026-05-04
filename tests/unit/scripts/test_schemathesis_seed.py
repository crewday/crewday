"""Focused tests for the Schemathesis seed helper."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.llm.models import AgentDoc
from app.adapters.db.workspace.models import Workspace
from app.domain.messaging.push_tokens import SETTINGS_KEY_VAPID_PUBLIC
from tests.unit.api.admin._helpers import engine_fixture, seed_user, seed_workspace


def test_admin_contract_path_resources_are_live_and_idempotent() -> None:
    from scripts._schemathesis_seed import seed_admin_contract_path_resources

    engine_iter = engine_fixture()
    engine = next(engine_iter)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    try:
        with session_factory() as session:
            actor_id = seed_user(
                session,
                email="schemathesis-owner@dev.local",
                display_name="Schemathesis Owner",
            )
            workspace_id = seed_workspace(session, slug="schemathesis")

            first = seed_admin_contract_path_resources(
                session,
                actor_user_id=actor_id,
                workspace_id=workspace_id,
            )
            second = seed_admin_contract_path_resources(
                session,
                actor_user_id=actor_id,
                workspace_id=workspace_id,
            )

            grant = session.get(RoleGrant, first.admin_revoke_grant_id)
            doc = session.scalar(
                select(AgentDoc).where(AgentDoc.slug == first.agent_doc_slug)
            )
    finally:
        engine.dispose()

    assert first == second
    assert first.workspace_id == workspace_id
    assert grant is not None
    assert grant.scope_kind == "deployment"
    assert grant.revoked_at is None
    assert grant.user_id != actor_id
    assert doc is not None
    assert doc.is_active is True


def test_contract_vapid_public_key_seed_is_idempotent() -> None:
    from scripts._schemathesis_seed import _ensure_contract_vapid_public_key

    engine_iter = engine_fixture()
    engine = next(engine_iter)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    try:
        with session_factory() as session:
            workspace_id = seed_workspace(session, slug="schemathesis")
            workspace = session.get_one(Workspace, workspace_id)

            _ensure_contract_vapid_public_key(workspace)
            first_settings = dict(workspace.settings_json or {})

            _ensure_contract_vapid_public_key(workspace)
            second_settings = dict(workspace.settings_json or {})

            existing_workspace_id = seed_workspace(session, slug="schemathesis-custom")
            existing_workspace = session.get_one(Workspace, existing_workspace_id)
            existing_workspace.settings_json = {
                SETTINGS_KEY_VAPID_PUBLIC: "operator-key"
            }
            _ensure_contract_vapid_public_key(existing_workspace)
            existing_settings = dict(existing_workspace.settings_json or {})
    finally:
        engine.dispose()

    assert first_settings[SETTINGS_KEY_VAPID_PUBLIC] == "schemathesis-vapid-public-key"
    assert second_settings == first_settings
    assert existing_settings[SETTINGS_KEY_VAPID_PUBLIC] == "operator-key"
