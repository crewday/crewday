"""Expense notification fanout helpers."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Protocol

from app.domain.expenses.ports import ExpenseClaimRow, ExpensesRepository
from app.domain.messaging.notifications import NotificationKind
from app.tenancy import WorkspaceContext

__all__ = [
    "ExpenseNotificationSink",
    "notify_expense_approved",
    "notify_expense_rejected",
    "notify_expense_submitted",
]

_log = logging.getLogger(__name__)


class ExpenseNotificationSink(Protocol):
    def notify(
        self,
        *,
        recipient_user_id: str,
        kind: NotificationKind,
        payload: Mapping[str, object],
    ) -> str: ...


def notify_expense_submitted(
    repo: ExpensesRepository,
    ctx: WorkspaceContext,
    *,
    claim: ExpenseClaimRow,
    submitter_user_id: str,
    sink: ExpenseNotificationSink,
) -> None:
    payload = _expense_payload(claim, submitter_user_id=submitter_user_id)
    for user_id in repo.list_expense_approver_user_ids(workspace_id=ctx.workspace_id):
        _notify(
            sink,
            recipient_user_id=user_id,
            kind=NotificationKind.EXPENSE_SUBMITTED,
            payload=payload,
        )


def notify_expense_approved(
    *,
    claim: ExpenseClaimRow,
    submitter_user_id: str,
    sink: ExpenseNotificationSink,
) -> None:
    _notify(
        sink,
        recipient_user_id=submitter_user_id,
        kind=NotificationKind.EXPENSE_APPROVED,
        payload=_expense_payload(claim, submitter_user_id=submitter_user_id),
    )


def notify_expense_rejected(
    *,
    claim: ExpenseClaimRow,
    submitter_user_id: str,
    sink: ExpenseNotificationSink,
) -> None:
    _notify(
        sink,
        recipient_user_id=submitter_user_id,
        kind=NotificationKind.EXPENSE_REJECTED,
        payload=_expense_payload(claim, submitter_user_id=submitter_user_id),
    )


def _expense_payload(
    claim: ExpenseClaimRow,
    *,
    submitter_user_id: str,
) -> dict[str, object]:
    return {
        "claim_id": claim.id,
        "work_engagement_id": claim.work_engagement_id,
        "submitter_user_id": submitter_user_id,
        "vendor": claim.vendor,
        "currency": claim.currency,
        "total_amount_cents": claim.total_amount_cents,
        "category": claim.category,
        "purchased_at": claim.purchased_at.isoformat(),
        "state": claim.state,
    }


def _notify(
    sink: ExpenseNotificationSink,
    *,
    recipient_user_id: str,
    kind: NotificationKind,
    payload: Mapping[str, object],
) -> None:
    try:
        sink.notify(
            recipient_user_id=recipient_user_id,
            kind=kind,
            payload=payload,
        )
    except Exception:
        _log.exception(
            "expense notification fanout failed",
            extra={
                "event": "expenses.notification.failed",
                "kind": kind.value,
                "recipient_user_id": recipient_user_id,
            },
        )
