"""Focused tests for the Schemathesis seed helper."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.llm.models import AgentDoc
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
