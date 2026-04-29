"""Focused tests for ``expenses.autofill`` receipt OCR capability."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import BudgetLedger
from app.adapters.db.llm.models import LlmUsage as LlmUsageRow
from app.adapters.llm.ports import ChatMessage, LLMResponse, LLMUsage
from app.domain.llm.budget import WINDOW_DAYS, BudgetExceeded
from app.domain.llm.capabilities.receipt_ocr import (
    AUTOFILL_CAPABILITY,
    ReceiptOcrContext,
    ReceiptParseError,
    extract,
)
from app.domain.llm.router import CapabilityUnassignedError
from app.tenancy import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.domain.llm.conftest import (
    build_context,
    seed_assignment,
    seed_workspace,
)

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


class StubLLM:
    def __init__(
        self,
        *,
        payload: dict[str, Any] | str,
        payloads_by_model: dict[str, dict[str, Any] | str] | None = None,
    ) -> None:
        self.payload = payload
        self.payloads_by_model = payloads_by_model or {}
        self.calls: list[tuple[str, str]] = []

    def complete(
        self,
        *,
        model_id: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        raise NotImplementedError

    def ocr(self, *, model_id: str, image_bytes: bytes) -> str:
        self.calls.append(("ocr", model_id))
        return "Vendor: Monoprix\nTotal: 34.12 EUR\n2026-04-15"

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append(("chat", model_id))
        payload = self.payloads_by_model.get(model_id, self.payload)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return LLMResponse(
            text=text,
            usage=LLMUsage(prompt_tokens=80, completion_tokens=30, total_tokens=110),
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


def _seed_capability(
    session: Session,
    *,
    workspace_id: str,
    model_id: str = "01HWA00000000000000000RCPT",
    api_model_id: str = "fake/receipt-model",
    priority: int = 0,
) -> None:
    seed_assignment(
        session,
        workspace_id=workspace_id,
        capability=AUTOFILL_CAPABILITY,
        model_id=model_id,
        api_model_id=api_model_id,
        priority=priority,
        required_capabilities=["vision", "json_mode"],
        max_tokens=512,
    )


def _usage_rows(session: Session, *, ctx: WorkspaceContext) -> list[LlmUsageRow]:
    token = set_current(ctx)
    try:
        return list(
            session.execute(
                select(LlmUsageRow)
                .where(LlmUsageRow.workspace_id == ctx.workspace_id)
                .order_by(LlmUsageRow.created_at.asc(), LlmUsageRow.attempt.asc())
            )
            .scalars()
            .all()
        )
    finally:
        reset_current(token)


def test_canonical_receipt_parses_and_records_usage(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(db_session, workspace_id=ws.id)
    workspace_ctx = build_context(ws.id)
    llm = StubLLM(
        payload={
            "vendor": "Monoprix",
            "amount_cents": 3412,
            "currency_iso4217": "EUR",
            "occurred_on": "2026-04-15",
            "category": "food",
            "confidence_pct": 92,
            "is_receipt": True,
        }
    )
    token = set_current(workspace_ctx)
    try:
        draft = extract(
            ReceiptOcrContext(
                session=db_session,
                workspace_ctx=workspace_ctx,
                llm=llm,
                workspace_currency_iso4217="USD",
                workspace_timezone="UTC",
                pricing={"fake/receipt-model": (0, 0)},
                clock=clock,
            ),
            b"receipt-bytes",
        )
    finally:
        reset_current(token)

    assert draft.vendor == "Monoprix"
    assert draft.amount_cents == 3412
    assert draft.currency_iso4217 == "EUR"
    assert draft.occurred_on.isoformat() == "2026-04-15"
    assert draft.category == "food"
    assert draft.confidence_pct == 92
    assert llm.calls == [
        ("ocr", "fake/receipt-model"),
        ("chat", "fake/receipt-model"),
    ]
    rows = _usage_rows(db_session, ctx=workspace_ctx)
    assert len(rows) == 1
    assert rows[0].capability == AUTOFILL_CAPABILITY
    assert rows[0].model_id == "fake/receipt-model"
    assert rows[0].tokens_in == 80
    assert rows[0].tokens_out == 30
    assert rows[0].status == "ok"


def test_missing_vendor_uses_no_fabrication_and_low_confidence(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(db_session, workspace_id=ws.id)
    workspace_ctx = build_context(ws.id)
    llm = StubLLM(
        payload={
            "vendor": None,
            "amount_cents": 1299,
            "category": "supplies",
            "confidence_pct": 99,
            "is_receipt": True,
        }
    )
    token = set_current(workspace_ctx)
    try:
        draft = extract(
            ReceiptOcrContext(
                session=db_session,
                workspace_ctx=workspace_ctx,
                llm=llm,
                workspace_currency_iso4217="usd",
                workspace_timezone="America/New_York",
                pricing={"fake/receipt-model": (0, 0)},
                clock=clock,
            ),
            b"receipt-bytes",
        )
    finally:
        reset_current(token)

    assert draft.vendor is None
    assert draft.currency_iso4217 == "USD"
    assert draft.occurred_on.isoformat() == "2026-04-19"
    assert draft.confidence_pct == 40


def test_spec_field_objects_parse_with_min_field_confidence(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(db_session, workspace_id=ws.id)
    workspace_ctx = build_context(ws.id)
    llm = StubLLM(
        payload={
            "vendor": {"value": "Monoprix", "confidence": 0.94},
            "total_amount_cents": {"value": 3412, "confidence": 0.72},
            "currency": {"value": "EUR", "confidence": 0.99},
            "purchased_at": {"value": "2026-04-15", "confidence": 0.88},
            "category": {"value": "food", "confidence": 0.55},
            "is_receipt": True,
        }
    )
    token = set_current(workspace_ctx)
    try:
        draft = extract(
            ReceiptOcrContext(
                session=db_session,
                workspace_ctx=workspace_ctx,
                llm=llm,
                workspace_currency_iso4217="USD",
                workspace_timezone="UTC",
                pricing={"fake/receipt-model": (0, 0)},
                clock=clock,
            ),
            b"receipt-bytes",
        )
    finally:
        reset_current(token)

    assert draft.vendor == "Monoprix"
    assert draft.amount_cents == 3412
    assert draft.currency_iso4217 == "EUR"
    assert draft.occurred_on.isoformat() == "2026-04-15"
    assert draft.category == "food"
    assert draft.confidence_pct == 55


def test_parse_failure_falls_through_assignment_chain(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(
        db_session,
        workspace_id=ws.id,
        model_id="01HWA00000000000000000BAD",
        api_model_id="fake/bad-receipt-model",
        priority=0,
    )
    _seed_capability(
        db_session,
        workspace_id=ws.id,
        model_id="01HWA00000000000000000GOOD",
        api_model_id="fake/good-receipt-model",
        priority=1,
    )
    workspace_ctx = build_context(ws.id)
    llm = StubLLM(
        payload={},
        payloads_by_model={
            "fake/bad-receipt-model": "not json",
            "fake/good-receipt-model": {
                "vendor": "Monoprix",
                "amount_cents": 3412,
                "currency_iso4217": "EUR",
                "occurred_on": "2026-04-15",
                "category": "food",
                "confidence_pct": 92,
                "is_receipt": True,
            },
        },
    )
    token = set_current(workspace_ctx)
    try:
        draft = extract(
            ReceiptOcrContext(
                session=db_session,
                workspace_ctx=workspace_ctx,
                llm=llm,
                workspace_currency_iso4217="USD",
                workspace_timezone="UTC",
                pricing={
                    "fake/bad-receipt-model": (0, 0),
                    "fake/good-receipt-model": (0, 0),
                },
                clock=clock,
            ),
            b"receipt-bytes",
        )
    finally:
        reset_current(token)

    assert draft.vendor == "Monoprix"
    assert llm.calls == [
        ("ocr", "fake/bad-receipt-model"),
        ("chat", "fake/bad-receipt-model"),
        ("ocr", "fake/good-receipt-model"),
        ("chat", "fake/good-receipt-model"),
    ]
    rows = _usage_rows(db_session, ctx=workspace_ctx)
    assert len(rows) == 2
    assert rows[0].model_id == "fake/bad-receipt-model"
    assert rows[0].attempt == 0
    assert rows[0].fallback_attempts == 0
    assert rows[1].model_id == "fake/good-receipt-model"
    assert rows[1].attempt == 1
    assert rows[1].fallback_attempts == 1


def test_non_receipt_classification_does_not_fall_through(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(
        db_session,
        workspace_id=ws.id,
        model_id="01HWA00000000000000000BAD",
        api_model_id="fake/bad-receipt-model",
        priority=0,
    )
    _seed_capability(
        db_session,
        workspace_id=ws.id,
        model_id="01HWA00000000000000000GOOD",
        api_model_id="fake/good-receipt-model",
        priority=1,
    )
    workspace_ctx = build_context(ws.id)
    llm = StubLLM(
        payload={},
        payloads_by_model={
            "fake/bad-receipt-model": {"is_receipt": False, "confidence_pct": 98},
            "fake/good-receipt-model": {
                "vendor": "Monoprix",
                "amount_cents": 3412,
                "currency_iso4217": "EUR",
                "occurred_on": "2026-04-15",
                "category": "food",
                "confidence_pct": 92,
                "is_receipt": True,
            },
        },
    )
    token = set_current(workspace_ctx)
    try:
        with pytest.raises(ReceiptParseError):
            extract(
                ReceiptOcrContext(
                    session=db_session,
                    workspace_ctx=workspace_ctx,
                    llm=llm,
                    workspace_currency_iso4217="USD",
                    workspace_timezone="UTC",
                    pricing={
                        "fake/bad-receipt-model": (0, 0),
                        "fake/good-receipt-model": (0, 0),
                    },
                    clock=clock,
                ),
                b"not-a-receipt",
            )
    finally:
        reset_current(token)

    assert llm.calls == [
        ("ocr", "fake/bad-receipt-model"),
        ("chat", "fake/bad-receipt-model"),
    ]
    rows = _usage_rows(db_session, ctx=workspace_ctx)
    assert len(rows) == 1
    assert rows[0].model_id == "fake/bad-receipt-model"


@pytest.mark.parametrize(
    "payload",
    [
        {"is_receipt": False, "confidence_pct": 98},
        "this is not json",
    ],
)
def test_non_receipt_or_invalid_json_raises_parse_error(
    db_session: Session, clock: FrozenClock, payload: dict[str, Any] | str
) -> None:
    ws = seed_workspace(db_session)
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(db_session, workspace_id=ws.id)
    workspace_ctx = build_context(ws.id)
    token = set_current(workspace_ctx)
    try:
        with pytest.raises(ReceiptParseError):
            extract(
                ReceiptOcrContext(
                    session=db_session,
                    workspace_ctx=workspace_ctx,
                    llm=StubLLM(payload=payload),
                    workspace_currency_iso4217="USD",
                    workspace_timezone="UTC",
                    pricing={"fake/receipt-model": (0, 0)},
                    clock=clock,
                ),
                b"not-a-receipt",
            )
    finally:
        reset_current(token)


def test_budget_exceeded_propagates_and_skips_llm_call(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    _seed_ledger(db_session, workspace_id=ws.id, cap_cents=1)
    _seed_capability(db_session, workspace_id=ws.id)
    workspace_ctx = build_context(ws.id)
    llm = StubLLM(payload={})
    token = set_current(workspace_ctx)
    try:
        with pytest.raises(BudgetExceeded):
            extract(
                ReceiptOcrContext(
                    session=db_session,
                    workspace_ctx=workspace_ctx,
                    llm=llm,
                    workspace_currency_iso4217="USD",
                    workspace_timezone="UTC",
                    pricing={"fake/receipt-model": (100_000, 100_000)},
                    clock=clock,
                ),
                b"receipt-bytes",
            )
    finally:
        reset_current(token)

    assert llm.calls == []
    assert _usage_rows(db_session, ctx=workspace_ctx) == []


def test_capability_unassigned_propagates_and_skips_llm_call(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    _seed_ledger(db_session, workspace_id=ws.id)
    workspace_ctx = build_context(ws.id)
    llm = StubLLM(payload={})
    token = set_current(workspace_ctx)
    try:
        with pytest.raises(CapabilityUnassignedError):
            extract(
                ReceiptOcrContext(
                    session=db_session,
                    workspace_ctx=workspace_ctx,
                    llm=llm,
                    workspace_currency_iso4217="USD",
                    workspace_timezone="UTC",
                    pricing={"fake/receipt-model": (0, 0)},
                    clock=clock,
                ),
                b"receipt-bytes",
            )
    finally:
        reset_current(token)

    assert llm.calls == []
    assert _usage_rows(db_session, ctx=workspace_ctx) == []
