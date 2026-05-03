"""Focused tests for ``digest.compose`` daily digest prose."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.db.llm.models import BudgetLedger
from app.adapters.db.llm.models import LlmUsage as LlmUsageRow
from app.adapters.llm.ports import ChatMessage, LLMResponse, LLMUsage
from app.domain.llm.budget import WINDOW_DAYS
from app.domain.llm.capabilities.digest_gen import (
    DIGEST_COMPOSE_CAPABILITY,
    DigestComposeContext,
    compose,
)
from app.tenancy import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from app.util.redact import ConsentSet
from app.util.ulid import new_ulid
from tests.domain.llm.conftest import (
    build_context,
    seed_assignment,
    seed_workspace,
)

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


class StubLLM:
    def __init__(self, responses: Sequence[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, Sequence[ChatMessage]]] = []

    def complete(
        self,
        *,
        model_id: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        consents: ConsentSet | None = None,
    ) -> LLMResponse:
        raise NotImplementedError

    def ocr(
        self,
        *,
        model_id: str,
        image_bytes: bytes,
        consents: ConsentSet | None = None,
    ) -> str:
        raise NotImplementedError

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        consents: ConsentSet | None = None,
    ) -> LLMResponse:
        self.calls.append((model_id, messages))
        text = self._responses.pop(0)
        return LLMResponse(
            text=text,
            usage=LLMUsage(prompt_tokens=90, completion_tokens=35, total_tokens=125),
            model_id=model_id,
            finish_reason="stop",
        )

    def stream_chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        consents: ConsentSet | None = None,
    ) -> Iterator[str]:
        raise NotImplementedError


def _seed_ledger(
    session: Session,
    *,
    workspace_id: str,
    cap_cents: int = 500,
    spent_cents: int = 0,
) -> BudgetLedger:
    row = BudgetLedger(
        id=new_ulid(),
        workspace_id=workspace_id,
        period_start=_PINNED - timedelta(days=WINDOW_DAYS),
        period_end=_PINNED,
        spent_cents=spent_cents,
        cap_cents=cap_cents,
        updated_at=_PINNED,
    )
    session.add(row)
    session.flush()
    return row


def _seed_capability(session: Session, *, workspace_id: str) -> None:
    seed_assignment(
        session,
        workspace_id=workspace_id,
        capability=DIGEST_COMPOSE_CAPABILITY,
        model_id="01HWA000000000000000DIGST",
        api_model_id="fake/digest-model",
        required_capabilities=["chat"],
        max_tokens=512,
    )


def _seed_user(
    session: Session,
    *,
    display_name: str = "Maria",
    locale: str | None = "en",
) -> User:
    user = User(
        id=new_ulid(),
        email=f"{new_ulid().lower()}@example.test",
        display_name=display_name,
        locale=locale,
        timezone="UTC",
        avatar_blob_hash=None,
        agent_approval_mode="strict",
        created_at=_PINNED,
        last_login_at=None,
        archived_at=None,
    )
    session.add(user)
    session.flush()
    return user


def _payload() -> Mapping[str, object]:
    return {
        "overdue_tasks": [
            {
                "task_name": "Patio Sweep",
                "assignee_name": "Maria",
                "due_local": "2026-04-19",
            }
        ],
        "anomalies": [],
        "upcoming_stays": [],
    }


def _make_context(
    session: Session,
    *,
    ctx: WorkspaceContext,
    llm: StubLLM,
    clock: FrozenClock,
    pricing: Mapping[str, tuple[int, int]] | None = None,
) -> DigestComposeContext:
    return DigestComposeContext(
        session=session,
        workspace_ctx=ctx,
        llm=llm,
        pricing=dict(pricing or {"fake/digest-model": (0, 0)}),
        clock=clock,
    )


def _usage_rows(session: Session, *, ctx: WorkspaceContext) -> list[LlmUsageRow]:
    token = set_current(ctx)
    try:
        return list(
            session.execute(
                select(LlmUsageRow)
                .where(LlmUsageRow.workspace_id == ctx.workspace_id)
                .order_by(LlmUsageRow.attempt.asc())
            )
            .scalars()
            .all()
        )
    finally:
        reset_current(token)


def test_no_anomalies_one_overdue_task_names_task_and_assignee_verbatim(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    recipient = _seed_user(db_session)
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(db_session, workspace_id=ws.id)
    ctx = build_context(ws.id)
    llm = StubLLM(["Patio Sweep is overdue for Maria. No anomalies."])

    token = set_current(ctx)
    try:
        prose = compose(
            _make_context(db_session, ctx=ctx, llm=llm, clock=clock),
            recipient.id,
            _payload(),
        )
    finally:
        reset_current(token)

    assert "Patio Sweep" in prose.body_md
    assert "Maria" in prose.body_md
    assert prose.used_fallback is False
    assert len(llm.calls) == 1
    assert len(_usage_rows(db_session, ctx=ctx)) == 1


def test_hallucinated_number_retries_and_accepts_second_output(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    recipient = _seed_user(db_session)
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(db_session, workspace_id=ws.id)
    ctx = build_context(ws.id)
    llm = StubLLM(
        [
            "Patio Sweep is overdue for Maria. There are 7 urgent issues.",
            "Patio Sweep is overdue for Maria. No anomalies.",
        ]
    )

    token = set_current(ctx)
    try:
        prose = compose(
            _make_context(db_session, ctx=ctx, llm=llm, clock=clock),
            recipient.id,
            _payload(),
        )
    finally:
        reset_current(token)

    assert prose.body_md == "Patio Sweep is overdue for Maria. No anomalies."
    assert prose.used_fallback is False
    assert prose.attempts == 2
    assert len(llm.calls) == 2
    assert len(_usage_rows(db_session, ctx=ctx)) == 2


def test_third_validation_failure_uses_template_fallback(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    recipient = _seed_user(db_session)
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(db_session, workspace_id=ws.id)
    ctx = build_context(ws.id)
    llm = StubLLM(
        [
            "Patio Sweep is overdue for Maria and Alex.",
            "Patio Sweep is overdue for Maria with 7 blockers.",
            "Patio Sweep is overdue for Maria at Sunrise Villa.",
        ]
    )

    token = set_current(ctx)
    try:
        prose = compose(
            _make_context(db_session, ctx=ctx, llm=llm, clock=clock),
            recipient.id,
            _payload(),
        )
    finally:
        reset_current(token)

    assert prose.used_fallback is True
    assert prose.attempts == 3
    assert "Patio Sweep - assignee: Maria" in prose.body_md
    assert len(llm.calls) == 3
    assert len(_usage_rows(db_session, ctx=ctx)) == 3


def test_french_locale_prompt_and_fallback(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    recipient = _seed_user(db_session, locale="fr-FR")
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(db_session, workspace_id=ws.id)
    ctx = build_context(ws.id)
    llm = StubLLM(
        [
            "Patio Sweep est en retard pour Maria et Alex.",
            "Patio Sweep est en retard pour Maria avec 7 problemes.",
            "Patio Sweep est en retard pour Maria a Sunrise Villa.",
        ]
    )

    token = set_current(ctx)
    try:
        prose = compose(
            _make_context(db_session, ctx=ctx, llm=llm, clock=clock),
            recipient.id,
            _payload(),
        )
    finally:
        reset_current(token)

    assert prose.locale == "fr-FR"
    assert prose.used_fallback is True
    assert prose.body_md.startswith("Bonjour")
    assert "Taches en retard" in prose.body_md
    assert "French" in llm.calls[0][1][0]["content"]


def test_budget_exceeded_fallback_skips_llm_and_usage_rows(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    recipient = _seed_user(db_session)
    _seed_ledger(db_session, workspace_id=ws.id, cap_cents=1, spent_cents=1)
    _seed_capability(db_session, workspace_id=ws.id)
    ctx = build_context(ws.id)
    llm = StubLLM(["Patio Sweep is overdue for Maria."])

    token = set_current(ctx)
    try:
        prose = compose(
            _make_context(
                db_session,
                ctx=ctx,
                llm=llm,
                clock=clock,
                pricing={"fake/digest-model": (1_000_000, 1_000_000)},
            ),
            recipient.id,
            _payload(),
        )
    finally:
        reset_current(token)

    assert prose.used_fallback is True
    assert "Patio Sweep - assignee: Maria" in prose.body_md
    assert llm.calls == []
    assert _usage_rows(db_session, ctx=ctx) == []
