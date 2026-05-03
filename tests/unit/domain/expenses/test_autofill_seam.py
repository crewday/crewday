"""Fake-driven seam tests for :mod:`app.domain.expenses.autofill`."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.adapters.llm.ports import (
    ChatMessage,
    LLMCapabilityMissing,
    LLMResponse,
    LLMUsage,
)
from app.config import Settings
from app.domain.expenses.autofill import (
    AUTOFILL_CAPABILITY,
    AttachmentNotFound,
    ClaimNotFound,
    ExtractionTimeout,
    run_extraction,
)
from app.domain.expenses.ports import (
    ExpenseAttachmentRow,
    ExpenseClaimRow,
    LlmUsageStatus,
    PendingClaimsCursor,
    WorkEngagementRow,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.redact import ConsentSet
from app.util.ulid import new_ulid
from tests._fakes.storage import InMemoryStorage

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_PURCHASED = _PINNED - timedelta(days=2)
_WS_ID = "01HWA00000000000000000WS01"
_ACTOR_ID = "01HWA00000000000000000USR1"
_CLAIM_ID = "01HWA00000000000000000EXP1"
_ATTACHMENT_ID = "01HWA00000000000000000ATT1"
_BLOB_HASH = hashlib.sha256(b"receipt").hexdigest()
_OCR_MODEL = "test/gemma-vision"


class _FakeAuditSession(Session):
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, instance: object, _warn: bool = True) -> None:
        self.added.append(instance)

    def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        # ``run_extraction`` calls ``load_consent_set(repo.session, ...)``
        # which executes a SELECT against ``agent_preference``. The seam
        # test never seeds that row; mimic the production "no row"
        # branch so the loader returns ``ConsentSet.none()``.
        class _NoneResult:
            def scalar_one_or_none(self) -> None:
                return None

        return _NoneResult()


@dataclass
class _FakeRepo:
    claim: ExpenseClaimRow | None
    attachment: ExpenseAttachmentRow | None
    audit_session: _FakeAuditSession = field(default_factory=_FakeAuditSession)
    claim_reads: list[dict[str, object]] = field(default_factory=list)
    attachment_reads: list[dict[str, str]] = field(default_factory=list)
    claim_updates: list[dict[str, Any]] = field(default_factory=list)
    llm_usage_rows: list[dict[str, object]] = field(default_factory=list)

    @property
    def session(self) -> _FakeAuditSession:
        return self.audit_session

    def get_claim(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        include_deleted: bool = False,
        for_update: bool = False,
    ) -> ExpenseClaimRow | None:
        self.claim_reads.append(
            {
                "workspace_id": workspace_id,
                "claim_id": claim_id,
                "include_deleted": include_deleted,
                "for_update": for_update,
            }
        )
        if (
            self.claim is None
            or self.claim.workspace_id != workspace_id
            or self.claim.id != claim_id
        ):
            return None
        if not include_deleted and self.claim.deleted_at is not None:
            return None
        return self.claim

    def get_engagement(
        self, *, workspace_id: str, engagement_id: str
    ) -> WorkEngagementRow | None:
        raise NotImplementedError("autofill should not read engagements")

    def get_engagement_user_ids(
        self, *, workspace_id: str, engagement_ids: Sequence[str]
    ) -> dict[str, str]:
        raise NotImplementedError("autofill should not read engagement users")

    def list_claims_for_user(
        self,
        *,
        workspace_id: str,
        user_id: str,
        state: str | None,
        limit: int,
        cursor_id: str | None,
    ) -> list[ExpenseClaimRow]:
        raise NotImplementedError("autofill should not list claims")

    def list_claims_for_workspace(
        self,
        *,
        workspace_id: str,
        state: str | None,
        limit: int,
        cursor_id: str | None,
    ) -> list[ExpenseClaimRow]:
        raise NotImplementedError("autofill should not list claims")

    def list_pending_claims(
        self,
        *,
        workspace_id: str,
        claimant_user_id: str | None,
        property_id: str | None,
        category: str | None,
        limit: int,
        cursor: PendingClaimsCursor | None,
    ) -> list[ExpenseClaimRow]:
        raise NotImplementedError("autofill should not list pending claims")

    def list_pending_reimbursement_claims(
        self, *, workspace_id: str, user_id: str | None
    ) -> list[ExpenseClaimRow]:
        raise NotImplementedError("autofill should not list reimbursements")

    def list_attachments_for_claim(
        self, *, workspace_id: str, claim_id: str
    ) -> list[ExpenseAttachmentRow]:
        raise NotImplementedError("autofill should not list attachments")

    def get_attachment(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        attachment_id: str,
    ) -> ExpenseAttachmentRow | None:
        self.attachment_reads.append(
            {
                "workspace_id": workspace_id,
                "claim_id": claim_id,
                "attachment_id": attachment_id,
            }
        )
        if (
            self.attachment is None
            or self.attachment.workspace_id != workspace_id
            or self.attachment.claim_id != claim_id
            or self.attachment.id != attachment_id
        ):
            return None
        return self.attachment

    def insert_attachment(
        self,
        *,
        attachment_id: str,
        workspace_id: str,
        claim_id: str,
        blob_hash: str,
        kind: str,
        pages: int | None,
        created_at: datetime,
    ) -> ExpenseAttachmentRow:
        raise NotImplementedError("autofill should not insert attachments")

    def delete_attachment(
        self, *, workspace_id: str, claim_id: str, attachment_id: str
    ) -> None:
        raise NotImplementedError("autofill should not delete attachments")

    def insert_claim(
        self,
        *,
        claim_id: str,
        workspace_id: str,
        work_engagement_id: str,
        vendor: str,
        purchased_at: datetime,
        currency: str,
        total_amount_cents: int,
        category: str,
        property_id: str | None,
        note_md: str,
        created_at: datetime,
    ) -> ExpenseClaimRow:
        raise NotImplementedError("autofill should not insert claims")

    def update_claim_fields(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        fields: Mapping[str, Any],
    ) -> ExpenseClaimRow:
        if self.claim is None:
            raise RuntimeError("missing claim")
        if self.claim.workspace_id != workspace_id or self.claim.id != claim_id:
            raise RuntimeError("wrong claim")
        update = dict(fields)
        self.claim_updates.append(update)
        self.claim = replace(self.claim, **update)
        return self.claim

    def mark_claim_submitted(
        self, *, workspace_id: str, claim_id: str, submitted_at: datetime
    ) -> ExpenseClaimRow:
        raise NotImplementedError("autofill should not submit claims")

    def mark_claim_approved(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        decided_by: str,
        decided_at: datetime,
    ) -> ExpenseClaimRow:
        raise NotImplementedError("autofill should not approve claims")

    def mark_claim_rejected(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        decided_by: str,
        decided_at: datetime,
        decision_note_md: str,
    ) -> ExpenseClaimRow:
        raise NotImplementedError("autofill should not reject claims")

    def mark_claim_reimbursed(
        self,
        *,
        workspace_id: str,
        claim_id: str,
        reimbursed_at: datetime,
        reimbursed_via: str,
        reimbursed_by: str,
    ) -> ExpenseClaimRow:
        raise NotImplementedError("autofill should not reimburse claims")

    def mark_claim_deleted(
        self, *, workspace_id: str, claim_id: str, deleted_at: datetime
    ) -> ExpenseClaimRow:
        raise NotImplementedError("autofill should not delete claims")

    def get_user_display_names(self, *, user_ids: Sequence[str]) -> dict[str, str]:
        raise NotImplementedError("autofill should not read users")

    def insert_llm_usage(
        self,
        *,
        usage_id: str,
        workspace_id: str,
        capability: str,
        provider_model_id: str,
        tokens_in: int,
        tokens_out: int,
        cost_cents: int,
        latency_ms: int,
        status: LlmUsageStatus,
        correlation_id: str,
        actor_user_id: str,
        created_at: datetime,
    ) -> None:
        self.llm_usage_rows.append(
            {
                "usage_id": usage_id,
                "workspace_id": workspace_id,
                "capability": capability,
                "provider_model_id": provider_model_id,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_cents": cost_cents,
                "latency_ms": latency_ms,
                "status": status,
                "correlation_id": correlation_id,
                "actor_user_id": actor_user_id,
                "created_at": created_at,
            }
        )


class _StubLLMClient:
    def __init__(
        self,
        *,
        payload: dict[str, Any] | None = None,
        chat_error: Exception | None = None,
    ) -> None:
        self._payload = payload if payload is not None else _payload()
        self._chat_error = chat_error
        self.calls: list[tuple[str, str]] = []

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

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        consents: ConsentSet | None = None,
    ) -> LLMResponse:
        self.calls.append(("chat", model_id))
        if self._chat_error is not None:
            raise self._chat_error
        return LLMResponse(
            text=json.dumps(self._payload),
            usage=LLMUsage(
                prompt_tokens=42,
                completion_tokens=17,
                total_tokens=59,
            ),
            model_id=model_id,
            finish_reason="stop",
        )

    def ocr(
        self,
        *,
        model_id: str,
        image_bytes: bytes,
        consents: ConsentSet | None = None,
    ) -> str:
        self.calls.append(("ocr", model_id))
        return "Vendor: Bistro 42\nTotal: 27.50 EUR\n2026-04-17"

    def stream_chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        consents: ConsentSet | None = None,
    ) -> Iterator[str]:
        raise LLMCapabilityMissing("stream_chat")


def _ctx() -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=_WS_ID,
        workspace_slug="ws",
        actor_id=_ACTOR_ID,
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _claim(*, llm_autofill_json: Mapping[str, Any] | None = None) -> ExpenseClaimRow:
    return ExpenseClaimRow(
        id=_CLAIM_ID,
        workspace_id=_WS_ID,
        work_engagement_id="01HWA00000000000000000ENG1",
        vendor="Original Vendor",
        purchased_at=_PURCHASED,
        currency="EUR",
        total_amount_cents=1000,
        category="other",
        property_id=None,
        note_md="",
        state="draft",
        submitted_at=None,
        decided_by=None,
        decided_at=None,
        decision_note_md=None,
        reimbursed_at=None,
        reimbursed_via=None,
        reimbursed_by=None,
        llm_autofill_json=llm_autofill_json,
        autofill_confidence_overall=None,
        created_at=_PINNED,
        deleted_at=None,
    )


def _attachment() -> ExpenseAttachmentRow:
    return ExpenseAttachmentRow(
        id=_ATTACHMENT_ID,
        workspace_id=_WS_ID,
        claim_id=_CLAIM_ID,
        blob_hash=_BLOB_HASH,
        kind="receipt",
        pages=None,
        created_at=_PINNED,
    )


def _storage() -> InMemoryStorage:
    storage = InMemoryStorage()
    storage.put(_BLOB_HASH, io.BytesIO(b"receipt"), content_type="image/jpeg")
    return storage


def _payload(*, score: float = 0.95) -> dict[str, Any]:
    return {
        "vendor": "Bistro 42",
        "amount": "27.50",
        "currency": "EUR",
        "purchased_at": "2026-04-17T12:30:00+00:00",
        "category": "food",
        "confidence": {
            "vendor": score,
            "amount": score,
            "currency": score,
            "purchased_at": score,
            "category": score,
        },
    }


def test_run_extraction_reads_and_writes_through_repo_seam() -> None:
    repo = _FakeRepo(claim=_claim(), attachment=_attachment())
    clock = FrozenClock(_PINNED)

    result = run_extraction(
        repo,
        _ctx(),
        claim_id=_CLAIM_ID,
        attachment_id=_ATTACHMENT_ID,
        llm=_StubLLMClient(),
        storage=_storage(),
        clock=clock,
        settings=_settings(),
    )

    assert repo.claim_reads == [
        {
            "workspace_id": _WS_ID,
            "claim_id": _CLAIM_ID,
            "include_deleted": False,
            "for_update": True,
        }
    ]
    assert repo.attachment_reads == [
        {
            "workspace_id": _WS_ID,
            "claim_id": _CLAIM_ID,
            "attachment_id": _ATTACHMENT_ID,
        }
    ]
    assert result.autofilled is True
    assert repo.claim is not None
    assert repo.claim.vendor == "Bistro 42"
    assert repo.claim.total_amount_cents == 2750
    assert repo.claim.llm_autofill_json is not None
    assert repo.claim.autofill_confidence_overall == Decimal("0.95")
    assert repo.claim_updates[0]["currency"] == "EUR"
    assert repo.llm_usage_rows[0]["capability"] == AUTOFILL_CAPABILITY
    assert repo.llm_usage_rows[0]["tokens_in"] == 42
    assert repo.llm_usage_rows[0]["tokens_out"] == 17
    assert repo.llm_usage_rows[0]["status"] == "ok"


def test_followup_run_persists_payload_without_scalar_overwrite() -> None:
    repo = _FakeRepo(
        claim=_claim(llm_autofill_json={"previous": True}),
        attachment=_attachment(),
    )

    result = run_extraction(
        repo,
        _ctx(),
        claim_id=_CLAIM_ID,
        attachment_id=_ATTACHMENT_ID,
        llm=_StubLLMClient(),
        storage=_storage(),
        clock=FrozenClock(_PINNED),
        settings=_settings(),
    )

    assert result.autofilled is False
    assert repo.claim is not None
    assert repo.claim.vendor == "Original Vendor"
    assert repo.claim.total_amount_cents == 1000
    assert set(repo.claim_updates[0]) == {
        "llm_autofill_json",
        "autofill_confidence_overall",
    }


def test_timeout_records_failure_usage_through_repo() -> None:
    repo = _FakeRepo(claim=_claim(), attachment=_attachment())

    with pytest.raises(ExtractionTimeout):
        run_extraction(
            repo,
            _ctx(),
            claim_id=_CLAIM_ID,
            attachment_id=_ATTACHMENT_ID,
            llm=_StubLLMClient(chat_error=TimeoutError("read timeout")),
            storage=_storage(),
            clock=FrozenClock(_PINNED),
            settings=_settings(),
        )

    assert repo.claim_updates == []
    assert repo.llm_usage_rows[0]["status"] == "timeout"
    assert repo.llm_usage_rows[0]["tokens_in"] == 0
    assert repo.llm_usage_rows[0]["tokens_out"] == 0
    assert repo.audit_session.added[0].action == "receipt.ocr_failed"


def test_missing_claim_and_attachment_use_repo_not_found_results() -> None:
    with pytest.raises(ClaimNotFound):
        run_extraction(
            _FakeRepo(claim=None, attachment=_attachment()),
            _ctx(),
            claim_id=_CLAIM_ID,
            attachment_id=_ATTACHMENT_ID,
            llm=_StubLLMClient(),
            storage=_storage(),
            clock=FrozenClock(_PINNED),
            settings=_settings(),
        )

    with pytest.raises(AttachmentNotFound):
        run_extraction(
            _FakeRepo(claim=_claim(), attachment=None),
            _ctx(),
            claim_id=_CLAIM_ID,
            attachment_id=_ATTACHMENT_ID,
            llm=_StubLLMClient(),
            storage=_storage(),
            clock=FrozenClock(_PINNED),
            settings=_settings(),
        )


def _settings() -> Settings:
    return Settings(database_url="sqlite:///:memory:", llm_ocr_model=_OCR_MODEL)
