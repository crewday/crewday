"""Fake-driven seam tests for :mod:`app.domain.expenses.approval`."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.domain.expenses import (
    ApprovalEdits,
    ApprovalPermissionDenied,
    ClaimNotApprovable,
    ReimburseBody,
    ReimbursePermissionDenied,
    approve_claim,
    list_pending,
    mark_reimbursed,
    reject_claim,
)
from app.domain.expenses.ports import ExpenseClaimRow
from app.util.clock import FrozenClock
from tests.unit.domain.expenses.test_claims_seam import (
    _ENG_ID,
    _PINNED,
    _PURCHASED,
    _WS_ID,
    _ctx,
    _engagement,
    _FakeChecker,
    _FakeRepo,
)


def _submitted_claim(*, claim_id: str = "claim_1") -> ExpenseClaimRow:
    return ExpenseClaimRow(
        id=claim_id,
        workspace_id=_WS_ID,
        work_engagement_id=_ENG_ID,
        vendor="Acme",
        purchased_at=_PURCHASED,
        currency="EUR",
        total_amount_cents=1000,
        category="supplies",
        property_id="prop_1",
        note_md="",
        state="submitted",
        submitted_at=_PINNED,
        decided_by=None,
        decided_at=None,
        decision_note_md=None,
        reimbursed_at=None,
        reimbursed_via=None,
        reimbursed_by=None,
        llm_autofill_json=None,
        autofill_confidence_overall=None,
        created_at=_PURCHASED,
        deleted_at=None,
    )


def _repo_with_claim(row: ExpenseClaimRow | None = None) -> _FakeRepo:
    repo = _FakeRepo()
    repo.engagements[_ENG_ID] = _engagement()
    claim = row or _submitted_claim()
    repo.claims[claim.id] = claim
    return repo


def test_approve_claim_uses_checker_and_repository_write_methods() -> None:
    repo = _repo_with_claim()
    checker = _FakeChecker(allowed_keys={"expenses.approve"})

    view = approve_claim(
        repo,
        checker,
        _ctx(grant_role="manager"),
        claim_id="claim_1",
        edits=ApprovalEdits(vendor="Adjusted", currency="usd"),
        clock=FrozenClock(_PINNED),
    )

    assert checker.required_keys == ["expenses.approve"]
    assert view.state == "approved"
    assert view.vendor == "Adjusted"
    assert view.currency == "USD"
    assert repo.claims["claim_1"].decided_by == _ctx().actor_id
    assert repo.audit_session.added


def test_approve_claim_translates_seam_permission_denial() -> None:
    repo = _repo_with_claim()

    with pytest.raises(ApprovalPermissionDenied):
        approve_claim(repo, _FakeChecker(), _ctx(), claim_id="claim_1")


def test_reject_claim_records_reason_through_repository() -> None:
    repo = _repo_with_claim()

    view = reject_claim(
        repo,
        _FakeChecker(allowed_keys={"expenses.approve"}),
        _ctx(grant_role="manager"),
        claim_id="claim_1",
        reason_md="Wrong receipt",
        clock=FrozenClock(_PINNED),
    )

    assert view.state == "rejected"
    assert view.decision_note_md == "Wrong receipt"
    assert repo.claims["claim_1"].decision_note_md == "Wrong receipt"


def test_mark_reimbursed_requires_reimburse_capability() -> None:
    approved = replace(
        _submitted_claim(),
        state="approved",
        decided_by="manager_1",
        decided_at=_PINNED,
    )
    repo = _repo_with_claim(approved)

    with pytest.raises(ReimbursePermissionDenied):
        mark_reimbursed(
            repo,
            _FakeChecker(),
            _ctx(),
            claim_id="claim_1",
            body=ReimburseBody(via="bank"),
        )


def test_mark_reimbursed_writes_reimbursement_fields() -> None:
    approved = replace(
        _submitted_claim(),
        state="approved",
        decided_by="manager_1",
        decided_at=_PINNED,
    )
    repo = _repo_with_claim(approved)

    view = mark_reimbursed(
        repo,
        _FakeChecker(allowed_keys={"expenses.reimburse"}),
        _ctx(grant_role="manager"),
        claim_id="claim_1",
        body=ReimburseBody(via="card"),
        clock=FrozenClock(_PINNED),
    )

    assert view.state == "reimbursed"
    assert repo.claims["claim_1"].reimbursed_via == "card"
    assert repo.claims["claim_1"].reimbursed_by == _ctx().actor_id


def test_list_pending_uses_repository_cursor_and_filters() -> None:
    newest = _submitted_claim(claim_id="claim_3")
    middle = replace(_submitted_claim(claim_id="claim_2"), category="fuel")
    oldest = _submitted_claim(claim_id="claim_1")
    repo = _repo_with_claim(newest)
    repo.claims[middle.id] = middle
    repo.claims[oldest.id] = oldest

    page, cursor = list_pending(
        repo,
        _FakeChecker(allowed_keys={"expenses.approve"}),
        _ctx(grant_role="manager"),
        limit=1,
    )
    assert [row.id for row in page] == ["claim_3"]
    assert cursor is not None

    page2, _ = list_pending(
        repo,
        _FakeChecker(allowed_keys={"expenses.approve"}),
        _ctx(grant_role="manager"),
        limit=10,
        cursor=cursor,
    )
    assert [row.id for row in page2] == ["claim_2", "claim_1"]

    filtered, _ = list_pending(
        repo,
        _FakeChecker(allowed_keys={"expenses.approve"}),
        _ctx(grant_role="manager"),
        category="fuel",
    )
    assert [row.id for row in filtered] == ["claim_2"]


def test_non_submitted_claim_still_rejected_by_state_machine() -> None:
    repo = _repo_with_claim(replace(_submitted_claim(), state="draft"))

    with pytest.raises(ClaimNotApprovable):
        approve_claim(
            repo,
            _FakeChecker(allowed_keys={"expenses.approve"}),
            _ctx(grant_role="manager"),
            claim_id="claim_1",
        )
