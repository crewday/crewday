"""Tests for the deployment-scoped LLM prompt-template reader."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import LlmPromptTemplate, LlmPromptTemplateRevision
from app.services.llm import get_active_prompt
from app.util.clock import FrozenClock


def test_get_active_prompt_self_seeds_default(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    body = get_active_prompt(
        db_session,
        "chat.manager",
        default="Default manager prompt.",
        clock=clock,
    )

    row = db_session.scalar(
        select(LlmPromptTemplate).where(LlmPromptTemplate.capability == "chat.manager")
    )
    assert body == "Default manager prompt."
    assert row is not None
    assert row.template == "Default manager prompt."
    assert row.version == 1
    assert len(row.default_hash) == 16


def test_get_active_prompt_auto_upgrades_unchanged_default(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    get_active_prompt(db_session, "chat.manager", default="v1", clock=clock)

    body = get_active_prompt(db_session, "chat.manager", default="v2", clock=clock)

    row = db_session.scalar(
        select(LlmPromptTemplate).where(LlmPromptTemplate.capability == "chat.manager")
    )
    assert body == "v2"
    assert row is not None
    assert row.template == "v2"
    assert row.version == 2
    revisions = db_session.scalars(
        select(LlmPromptTemplateRevision).where(
            LlmPromptTemplateRevision.template_id == row.id
        )
    ).all()
    assert [(rev.version, rev.body) for rev in revisions] == [(1, "v1")]


def test_get_active_prompt_preserves_customized_template(
    db_session: Session,
    clock: FrozenClock,
) -> None:
    get_active_prompt(db_session, "chat.manager", default="v1", clock=clock)
    row = db_session.scalar(
        select(LlmPromptTemplate).where(LlmPromptTemplate.capability == "chat.manager")
    )
    assert row is not None
    row.template = "custom"
    db_session.flush()

    body = get_active_prompt(db_session, "chat.manager", default="v2", clock=clock)

    assert body == "custom"
    assert row.template == "custom"
    assert row.version == 1
    revisions = db_session.scalars(
        select(LlmPromptTemplateRevision).where(
            LlmPromptTemplateRevision.template_id == row.id
        )
    ).all()
    assert revisions == []
