"""Seed and read deployment-scoped LLM prompt templates."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from threading import Lock

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import LlmPromptTemplate, LlmPromptTemplateRevision
from app.tenancy import tenant_agnostic
from app.util.clock import Clock
from app.util.ulid import new_ulid

__all__ = ["get_active_prompt"]

_log = logging.getLogger(__name__)
_SEED_LOCK = Lock()


def get_active_prompt(
    session: Session,
    capability: str,
    default: str,
    clock: Clock | Callable[[], datetime] | None = None,
) -> str:
    """Return the active prompt body, self-seeding the code default first."""
    timestamp = _now(clock)
    default_hash = _hash_body(default)

    with _SEED_LOCK, tenant_agnostic():
        row = session.scalar(
            select(LlmPromptTemplate).where(
                LlmPromptTemplate.capability == capability,
                LlmPromptTemplate.is_active.is_(True),
            )
        )
        if row is None:
            session.add(
                LlmPromptTemplate(
                    id=new_ulid(),
                    capability=capability,
                    name=_default_name(capability),
                    template=default,
                    version=1,
                    is_active=True,
                    default_hash=default_hash,
                    notes=None,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            session.flush()
            _log.info(
                "llm prompt template seeded",
                extra={
                    "event": "template.seeded",
                    "table": "llm_prompt_template",
                    "capability": capability,
                },
            )
            return default

        if row.default_hash == default_hash:
            return row.template

        row.updated_at = timestamp
        if _hash_body(row.template) == row.default_hash:
            session.add(
                LlmPromptTemplateRevision(
                    id=new_ulid(),
                    template_id=row.id,
                    version=row.version,
                    body=row.template,
                    notes="Code default auto-upgrade",
                    created_at=timestamp,
                    created_by_user_id=None,
                )
            )
            row.template = default
            row.version += 1
            row.default_hash = default_hash
            session.flush()
            _log.info(
                "llm prompt template auto-upgraded",
                extra={
                    "event": "template.auto_upgraded",
                    "table": "llm_prompt_template",
                    "capability": capability,
                    "version": row.version,
                },
            )
            return row.template

        row.default_hash = default_hash
        session.flush()
        _log.warning(
            "llm prompt template customised while code default changed",
            extra={
                "event": "template.customised_code_default_changed",
                "table": "llm_prompt_template",
                "capability": capability,
                "version": row.version,
            },
        )
        return row.template


def _now(clock: Clock | Callable[[], datetime] | None) -> datetime:
    if clock is None:
        return datetime.now(UTC)
    if isinstance(clock, Clock):
        return clock.now()
    return clock()


def _hash_body(body: str) -> str:
    return sha256(body.encode("utf-8")).hexdigest()[:16]


def _default_name(capability: str) -> str:
    return capability.replace(".", " ").replace("_", " ").title()
