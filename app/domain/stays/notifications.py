"""Stay notification fanout helpers."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.messaging.notifications import NotificationKind

__all__ = ["StayNotificationSink", "StayUpcomingView", "notify_stay_upcoming"]

_log = logging.getLogger(__name__)


class StayNotificationSink(Protocol):
    def notify(
        self,
        *,
        recipient_user_id: str,
        kind: NotificationKind,
        payload: Mapping[str, object],
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class StayUpcomingView:
    id: str
    workspace_id: str
    property_id: str
    check_in: datetime
    check_out: datetime
    guest_name: str | None
    guest_count: int | None
    status: str
    source: str


def notify_stay_upcoming(
    *,
    stay: StayUpcomingView,
    recipient_user_ids: Sequence[str],
    sink: StayNotificationSink,
) -> None:
    payload = _stay_payload(stay)
    for user_id in recipient_user_ids:
        _notify(
            sink,
            recipient_user_id=user_id,
            kind=NotificationKind.STAY_UPCOMING,
            payload=payload,
        )


def _stay_payload(stay: StayUpcomingView) -> dict[str, object | None]:
    return {
        "stay_id": stay.id,
        "property_id": stay.property_id,
        "check_in": stay.check_in.isoformat(),
        "check_out": stay.check_out.isoformat(),
        "guest_name": stay.guest_name,
        "guest_count": stay.guest_count,
        "status": stay.status,
        "source": stay.source,
    }


def _notify(
    sink: StayNotificationSink,
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
            "stay notification fanout failed",
            extra={
                "event": "stays.notification.failed",
                "kind": kind.value,
                "recipient_user_id": recipient_user_id,
            },
        )
