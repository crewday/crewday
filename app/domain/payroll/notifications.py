"""Payslip notification fanout helpers."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Protocol

from app.domain.messaging.notifications import NotificationKind
from app.domain.payroll.ports import PayslipReadRow

__all__ = ["PayslipNotificationSink", "notify_payslip_issued"]

_log = logging.getLogger(__name__)


class PayslipNotificationSink(Protocol):
    def notify(
        self,
        *,
        recipient_user_id: str,
        kind: NotificationKind,
        payload: Mapping[str, object],
    ) -> str: ...


def notify_payslip_issued(
    *,
    payslip: PayslipReadRow,
    sink: PayslipNotificationSink,
) -> None:
    _notify(
        sink,
        recipient_user_id=payslip.user_id,
        kind=NotificationKind.PAYSLIP_ISSUED,
        payload=_payslip_payload(payslip),
    )


def _payslip_payload(payslip: PayslipReadRow) -> dict[str, object | None]:
    return {
        "payslip_id": payslip.id,
        "pay_period_id": payslip.pay_period_id,
        "user_id": payslip.user_id,
        "currency": payslip.currency,
        "gross_cents": payslip.gross_cents,
        "expense_reimbursements_cents": payslip.expense_reimbursements_cents,
        "net_cents": payslip.net_cents,
        "status": payslip.status,
        "issued_at": payslip.issued_at.isoformat() if payslip.issued_at else None,
        "paid_at": payslip.paid_at.isoformat() if payslip.paid_at else None,
        "created_at": payslip.created_at.isoformat(),
    }


def _notify(
    sink: PayslipNotificationSink,
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
            "payslip notification fanout failed",
            extra={
                "event": "payroll.payslip.notification.failed",
                "kind": kind.value,
                "recipient_user_id": recipient_user_id,
            },
        )
