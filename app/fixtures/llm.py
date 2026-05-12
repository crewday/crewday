"""Deployment-scope LLM registry seeds (cd-4btd).

The cd-4btd registry trio (:class:`~app.adapters.db.llm.models.LlmProvider`,
:class:`~app.adapters.db.llm.models.LlmModel`,
:class:`~app.adapters.db.llm.models.LlmProviderModel`) is populated
through the ``/admin/llm`` graph editor in production (§11 "LLM
graph admin"), but a fresh deployment needs *some* trio in place
before the first :class:`~app.domain.llm.router.ModelPick` resolves.
This module supplies the minimum-viable trio: a single ``fake``
provider + a generic chat model + their join row, suitable for both
the dev-loop and the test harness.

The seed is intentionally **not** auto-installed at startup — a
deployment operator decides when to call :func:`seed_default_registry`
(or its CLI equivalent, which lands with the future ``/admin/llm``
slice). Calling it twice is safe; the function checks for the
canonical name first and returns the existing row.

`docs/specs/11-llm-and-agents.md` §"Provider / model / provider-model
registry" pins the column shape; this module is the seed-side
implementation.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import (
    LlmAssignment,
    LlmModel,
    LlmProvider,
    LlmProviderModel,
)
from app.config import Settings
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "DEFAULT_MODEL_CANONICAL_NAME",
    "DEFAULT_MODEL_CAPABILITIES",
    "DEFAULT_PROVIDER_NAME",
    "OPENROUTER_DEFAULT_MODEL_CANONICAL_NAME",
    "OPENROUTER_DEFAULT_PROVIDER_NAME",
    "seed_default_registry",
    "seed_default_registry_for_settings",
]


# Stable identifiers so a re-seed (idempotent retry) lands the same
# row. ``fake`` keeps the seed safe against accidental upstream
# traffic — operators flip the provider type via the admin graph
# once they wire a real key.
DEFAULT_PROVIDER_NAME: str = "default-fake"
DEFAULT_MODEL_CANONICAL_NAME: str = "default/chat-base"
DEFAULT_MODEL_CAPABILITIES: tuple[str, ...] = (
    "chat",
    "function_calling",
    "json_mode",
    "vision",
)
OPENROUTER_DEFAULT_PROVIDER_NAME: str = "openrouter-default"
OPENROUTER_DEFAULT_MODEL_CANONICAL_NAME: str = "google/gemma-4-31b-it"
_DEFAULT_CAPABILITY: str = "default"
_DEFAULT_REQUIRED_CAPABILITIES: list[str] = ["chat", "function_calling"]


def seed_default_registry(
    session: Session,
    *,
    clock: Clock | None = None,
    api_model_id: str = "default/chat-base",
    provider_name: str = DEFAULT_PROVIDER_NAME,
    provider_type: str = "fake",
    model_canonical_name: str = DEFAULT_MODEL_CANONICAL_NAME,
    model_display_name: str = "Default Chat Base",
    model_vendor: str = "other",
    model_capabilities: list[str] | None = None,
) -> LlmProviderModel:
    """Insert (or return) the default LLM registry trio.

    Idempotent on ``LlmProvider.name`` and
    ``LlmModel.canonical_name``: a re-seed (e.g. an operator running
    a deployment-bootstrap script twice) returns the existing
    :class:`LlmProviderModel` row instead of duplicating the trio.

    The default trio satisfies the §11 ``chat.manager`` capability —
    callers who need a workspace assignment thread the returned
    row's ``id`` into :attr:`LlmAssignment.model_id` (the cd-4btd
    FK target). Per-call tuning (``temperature``, ``max_tokens``,
    …) lives on the assignment, not the provider_model — the
    deployment seed is intentionally minimal.

    The :class:`Clock` defaults to :class:`SystemClock`; tests
    thread :class:`~app.util.clock.FrozenClock` through so seeded
    timestamps stay deterministic.

    Transaction-neutral: the caller's UoW owns the commit boundary;
    we ``session.flush()`` so subsequent reads in the transaction
    see the new rows.
    """
    # code-health: ignore[nloc,params] Idempotent fixture seeding keeps trio together.
    c = clock if clock is not None else SystemClock()
    now: datetime = c.now()

    provider = session.execute(
        select(LlmProvider).where(LlmProvider.name == provider_name)
    ).scalar_one_or_none()
    if provider is None:
        provider = LlmProvider(
            id=new_ulid(c),
            name=provider_name,
            provider_type=provider_type,
            timeout_s=60,
            requests_per_minute=60,
            is_enabled=True,
            created_at=now,
            updated_at=now,
        )
        session.add(provider)
        session.flush()

    model = session.execute(
        select(LlmModel).where(LlmModel.canonical_name == model_canonical_name)
    ).scalar_one_or_none()
    if model is None:
        model = LlmModel(
            id=new_ulid(c),
            canonical_name=model_canonical_name,
            display_name=model_display_name,
            vendor=model_vendor,
            capabilities=list(model_capabilities or DEFAULT_MODEL_CAPABILITIES),
            thinking_level="disabled",
            is_active=True,
            price_source="",
            created_at=now,
            updated_at=now,
        )
        session.add(model)
        session.flush()

    # Idempotency key on the join is the unique
    # ``(provider_id, model_id)`` index. A re-seed returns the
    # existing join; otherwise the second insert would collide.
    provider_model = session.execute(
        select(LlmProviderModel).where(
            LlmProviderModel.provider_id == provider.id,
            LlmProviderModel.model_id == model.id,
        )
    ).scalar_one_or_none()
    if provider_model is None:
        provider_model = LlmProviderModel(
            id=new_ulid(c),
            provider_id=provider.id,
            model_id=model.id,
            api_model_id=api_model_id,
            supports_system_prompt=True,
            supports_temperature=True,
            thinking_level_override=None,
            is_enabled=True,
            created_at=now,
            updated_at=now,
        )
        session.add(provider_model)
        session.flush()
    assignment = session.execute(
        select(LlmAssignment).where(
            LlmAssignment.workspace_id.is_(None),
            LlmAssignment.capability == _DEFAULT_CAPABILITY,
            LlmAssignment.priority == 0,
        )
    ).scalar_one_or_none()
    if assignment is None:
        session.add(
            LlmAssignment(
                id=new_ulid(c),
                workspace_id=None,
                capability=_DEFAULT_CAPABILITY,
                model_id=provider_model.id,
                provider=provider.name,
                priority=0,
                enabled=True,
                max_tokens=None,
                temperature=None,
                extra_api_params={},
                required_capabilities=_DEFAULT_REQUIRED_CAPABILITIES,
                created_at=now,
            )
        )
        session.flush()
    return provider_model


def seed_default_registry_for_settings(
    session: Session,
    *,
    settings: Settings,
    clock: Clock | None = None,
) -> LlmProviderModel:
    """Seed the default registry row matching the configured runtime client."""
    if settings.llm_provider == "openrouter" or (
        settings.llm_provider is None
        and (settings.openrouter_api_key is not None or settings.root_key is not None)
    ):
        return seed_default_registry(
            session,
            clock=clock,
            api_model_id=OPENROUTER_DEFAULT_MODEL_CANONICAL_NAME,
            provider_name=OPENROUTER_DEFAULT_PROVIDER_NAME,
            provider_type="openrouter",
            model_canonical_name=OPENROUTER_DEFAULT_MODEL_CANONICAL_NAME,
            model_display_name="Google Gemma 4 31B IT",
            model_vendor="google",
            model_capabilities=["chat", "function_calling", "json_mode", "vision"],
        )
    return seed_default_registry(session, clock=clock)
