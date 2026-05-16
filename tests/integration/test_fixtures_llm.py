"""Integration tests for :mod:`app.fixtures.llm` (cd-4btd).

The default-registry seed must:

* Land all three rows (:class:`LlmProvider`, :class:`LlmModel`,
  :class:`LlmProviderModel`) on first call.
* Be idempotent — a second call returns the same join row without
  duplicating the trio.
* Produce a row a workspace can assign ``chat.manager`` to (i.e. an
  ``LlmProviderModel.id`` the cd-4btd FK on
  ``llm_assignment.model_id`` accepts).

See ``docs/specs/11-llm-and-agents.md`` §"Provider / model /
provider-model registry".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import (
    LlmAssignment,
    LlmModel,
    LlmProvider,
    LlmProviderModel,
)
from app.config import Settings
from app.fixtures.llm import (
    DEFAULT_MODEL_CANONICAL_NAME,
    DEFAULT_MODEL_CAPABILITIES,
    DEFAULT_PROVIDER_NAME,
    FEEDBACK_EMBED_CAPABILITY,
    LOCAL_BGE_EMBEDDING_DIMENSIONS,
    LOCAL_BGE_MODEL_CANONICAL_NAME,
    LOCAL_BGE_MODEL_DISPLAY_NAME,
    LOCAL_EMBEDDING_PROVIDER_NAME,
    OPENROUTER_DEFAULT_MODEL_CANONICAL_NAME,
    OPENROUTER_DEFAULT_PROVIDER_NAME,
    seed_default_registry,
    seed_default_registry_for_settings,
    seed_local_embedding_registry,
)
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


class TestSeedDefaultRegistry:
    def test_first_call_lands_trio(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        pm = seed_default_registry(db_session, clock=clock)

        # Provider + Model + ProviderModel all exist with the
        # expected stable identifiers.
        provider = db_session.get(LlmProvider, pm.provider_id)
        model = db_session.get(LlmModel, pm.model_id)
        assert provider is not None
        assert model is not None
        assert provider.name == DEFAULT_PROVIDER_NAME
        assert model.canonical_name == DEFAULT_MODEL_CANONICAL_NAME
        assert model.capabilities == list(DEFAULT_MODEL_CAPABILITIES)
        assert pm.api_model_id == "default/chat-base"
        assert pm.is_enabled is True
        assignment = db_session.scalar(
            select(LlmAssignment).where(
                LlmAssignment.workspace_id.is_(None),
                LlmAssignment.capability == "default",
                LlmAssignment.priority == 0,
            )
        )
        assert assignment is not None
        assert assignment.model_id == pm.id
        assert assignment.required_capabilities == ["chat", "function_calling"]
        local = db_session.scalar(
            select(LlmProvider).where(LlmProvider.name == LOCAL_EMBEDDING_PROVIDER_NAME)
        )
        assert local is not None
        assert local.provider_type == "local_embedding"

    def test_idempotent_re_seed_returns_same_row(self, db_session: Session) -> None:
        """Calling the seed twice does not duplicate the trio."""
        clock = FrozenClock(_PINNED)
        first = seed_default_registry(db_session, clock=clock)
        second = seed_default_registry(db_session, clock=clock)

        assert first.id == second.id

        # Exactly one provider / model / provider_model row landed —
        # the upstream uniques would already block a duplicate, but
        # this assertion makes the idempotency guarantee explicit.
        providers = db_session.execute(
            select(LlmProvider).where(LlmProvider.name == DEFAULT_PROVIDER_NAME)
        ).all()
        assert len(providers) == 1
        models = db_session.execute(
            select(LlmModel).where(
                LlmModel.canonical_name == DEFAULT_MODEL_CANONICAL_NAME
            )
        ).all()
        assert len(models) == 1
        provider_models = db_session.execute(
            select(LlmProviderModel).where(LlmProviderModel.id == first.id)
        ).all()
        assert len(provider_models) == 1
        default_assignments = db_session.execute(
            select(LlmAssignment).where(
                LlmAssignment.workspace_id.is_(None),
                LlmAssignment.capability == "default",
                LlmAssignment.priority == 0,
            )
        ).all()
        assert len(default_assignments) == 1
        feedback_assignments = db_session.execute(
            select(LlmAssignment).where(
                LlmAssignment.workspace_id.is_(None),
                LlmAssignment.capability == FEEDBACK_EMBED_CAPABILITY,
            )
        ).all()
        assert len(feedback_assignments) == 1

    def test_local_embedding_seed_lands_bge_and_feedback_embed(
        self, db_session: Session
    ) -> None:
        clock = FrozenClock(_PINNED)
        first = seed_local_embedding_registry(db_session, clock=clock)
        second = seed_local_embedding_registry(db_session, clock=clock)

        assert first.id == second.id
        provider = db_session.get(LlmProvider, first.provider_id)
        model = db_session.get(LlmModel, first.model_id)
        assert provider is not None
        assert model is not None
        assert provider.name == LOCAL_EMBEDDING_PROVIDER_NAME
        assert provider.provider_type == "local_embedding"
        assert model.canonical_name == LOCAL_BGE_MODEL_CANONICAL_NAME
        assert model.display_name == LOCAL_BGE_MODEL_DISPLAY_NAME
        assert model.capabilities == ["embeddings"]
        assert model.embedding_dimensions == LOCAL_BGE_EMBEDDING_DIMENSIONS
        assert model.max_output_tokens is None
        assert model.price_source == "manual"
        assert first.api_model_id == LOCAL_BGE_MODEL_CANONICAL_NAME
        assert first.input_cost_per_million == 0
        assert first.output_cost_per_million == 0
        assert first.fixed_cost_per_call_usd == 0

        assignment = db_session.scalar(
            select(LlmAssignment).where(
                LlmAssignment.workspace_id.is_(None),
                LlmAssignment.capability == FEEDBACK_EMBED_CAPABILITY,
            )
        )
        assert assignment is not None
        assert assignment.model_id == first.id
        assert assignment.priority == 0
        assert assignment.required_capabilities == ["embeddings"]

    def test_local_embedding_seed_preserves_existing_feedback_assignment(
        self, db_session: Session
    ) -> None:
        clock = FrozenClock(_PINNED)
        for row in db_session.scalars(
            select(LlmAssignment).where(
                LlmAssignment.workspace_id.is_(None),
                LlmAssignment.capability == FEEDBACK_EMBED_CAPABILITY,
            )
        ):
            db_session.delete(row)
        db_session.flush()
        provider = LlmProvider(
            id="manual-provider",
            name="Manual Embeddings",
            provider_type="openai_compatible",
            timeout_s=60,
            requests_per_minute=60,
            is_enabled=True,
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        model = LlmModel(
            id="manual-model",
            canonical_name="manual/embed",
            display_name="Manual Embed",
            capabilities=["embeddings"],
            embedding_dimensions=768,
            thinking_level="disabled",
            thinking_strategy="none",
            is_active=True,
            price_source="manual",
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        provider_model = LlmProviderModel(
            id="manual-provider-model",
            provider_id=provider.id,
            model_id=model.id,
            api_model_id="manual/embed",
            input_cost_per_million=0,
            output_cost_per_million=0,
            fixed_cost_per_call_usd=0,
            supports_system_prompt=False,
            supports_temperature=False,
            is_enabled=True,
            created_at=_PINNED,
            updated_at=_PINNED,
        )
        db_session.add_all([provider, model, provider_model])
        db_session.flush()
        db_session.add(
            LlmAssignment(
                id="manual-feedback-embed",
                workspace_id=None,
                capability=FEEDBACK_EMBED_CAPABILITY,
                model_id=provider_model.id,
                provider=provider.name,
                priority=0,
                enabled=True,
                extra_api_params={},
                required_capabilities=["embeddings"],
                created_at=_PINNED,
            )
        )
        db_session.flush()

        seed_local_embedding_registry(db_session, clock=clock)

        assignments = db_session.scalars(
            select(LlmAssignment).where(
                LlmAssignment.workspace_id.is_(None),
                LlmAssignment.capability == FEEDBACK_EMBED_CAPABILITY,
            )
        ).all()
        assert [row.model_id for row in assignments] == [provider_model.id]

    def test_seed_satisfies_chat_manager_assignment(self, db_session: Session) -> None:
        """A workspace assignment can FK at the seeded provider_model.

        Proves the cd-4btd FK on ``llm_assignment.model_id`` accepts
        the seed's ULID — i.e. the seed is the smallest unit that
        unblocks a fresh deployment from creating a working
        ``chat.manager`` chain.
        """
        clock = FrozenClock(_PINNED)
        pm = seed_default_registry(db_session, clock=clock)

        user = bootstrap_user(
            db_session,
            email="seed@example.com",
            display_name="Seed",
            clock=clock,
        )
        workspace = bootstrap_workspace(
            db_session,
            slug="seed-ws",
            name="SeedWs",
            owner_user_id=user.id,
            clock=clock,
        )
        ctx = WorkspaceContext(
            workspace_id=workspace.id,
            workspace_slug=workspace.slug,
            actor_id=user.id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id="01HWA00000000000000000SEED",
        )
        token = set_current(ctx)
        try:
            row = LlmAssignment(
                id="01HWA00000000000000000SEDA",
                workspace_id=workspace.id,
                capability="chat.manager",
                model_id=pm.id,
                provider="openrouter",
                created_at=_PINNED,
            )
            db_session.add(row)
            # No FK violation — the seed's id is a real registry row.
            db_session.flush()

            loaded = db_session.get(LlmAssignment, row.id)
            assert loaded is not None
            assert loaded.model_id == pm.id
        finally:
            reset_current(token)

    def test_settings_seed_uses_openrouter_for_legacy_keyed_runtime(
        self, db_session: Session
    ) -> None:
        clock = FrozenClock(_PINNED)
        pm = seed_default_registry_for_settings(
            db_session,
            settings=Settings(openrouter_api_key=SecretStr("test-key")),
            clock=clock,
        )

        provider = db_session.get(LlmProvider, pm.provider_id)
        model = db_session.get(LlmModel, pm.model_id)
        assert provider is not None
        assert model is not None
        assert provider.name == OPENROUTER_DEFAULT_PROVIDER_NAME
        assert provider.provider_type == "openrouter"
        assert model.canonical_name == OPENROUTER_DEFAULT_MODEL_CANONICAL_NAME
        assert pm.api_model_id == OPENROUTER_DEFAULT_MODEL_CANONICAL_NAME
