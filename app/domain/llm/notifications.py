"""LLM-domain notification fanout helpers."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

from app.adapters.notifications.ports import NotificationKind
from app.events.bus import EventBus
from app.tenancy import WorkspaceContext
from app.util.clock import Clock

__all__ = [
    "AnomalyDetectedView",
    "LlmNotificationSink",
    "notify_anomaly_detected",
]

_log = logging.getLogger(__name__)


class LlmNotificationSink(Protocol):
    def notify(
        self,
        *,
        recipient_user_id: str,
        kind: NotificationKind,
        payload: Mapping[str, object],
    ) -> str: ...

    def exists(
        self,
        *,
        recipient_user_id: str,
        kind: NotificationKind,
        payload_key: str,
        payload_value: object,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class AnomalyDetectedView:
    anomaly_kind: str
    subject_kind: str
    subject_id: str
    window_start: datetime
    window_end: datetime
    detected_at: datetime
    title: str
    explanation: str
    severity: str


def notify_anomaly_detected(
    session: Session,
    ctx: WorkspaceContext,
    *,
    anomaly: AnomalyDetectedView,
    clock: Clock,
    bus: EventBus,
    recipient_user_ids: Sequence[str] | None = None,
    sink: LlmNotificationSink | None = None,
) -> None:
    _ = session, clock, bus
    if sink is None:
        return
    recipients = tuple(recipient_user_ids) if recipient_user_ids is not None else ()
    dedupe_key = ":".join(
        (
            anomaly.anomaly_kind,
            anomaly.subject_kind,
            anomaly.subject_id,
            anomaly.window_start.isoformat(),
            anomaly.window_end.isoformat(),
        )
    )
    payload = {
        "anomaly_kind": anomaly.anomaly_kind,
        "subject_kind": anomaly.subject_kind,
        "subject_id": anomaly.subject_id,
        "window_start": anomaly.window_start.isoformat(),
        "window_end": anomaly.window_end.isoformat(),
        "detected_at": anomaly.detected_at.isoformat(),
        "title": anomaly.title,
        "explanation": anomaly.explanation,
        "severity": anomaly.severity,
        "dedupe_key": dedupe_key,
    }
    for user_id in sorted(set(recipients)):
        if _notification_exists(
            sink,
            recipient_user_id=user_id,
            dedupe_key=dedupe_key,
        ):
            continue
        _notify(
            sink,
            recipient_user_id=user_id,
            kind=NotificationKind.ANOMALY_DETECTED,
            payload=payload,
        )


def _notification_exists(
    sink: LlmNotificationSink,
    *,
    recipient_user_id: str,
    dedupe_key: str,
) -> bool:
    return sink.exists(
        recipient_user_id=recipient_user_id,
        kind=NotificationKind.ANOMALY_DETECTED,
        payload_key="dedupe_key",
        payload_value=dedupe_key,
    )


def _notify(
    sink: LlmNotificationSink,
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
            "LLM notification fanout failed",
            extra={
                "event": "llm.notification.failed",
                "kind": kind.value,
                "recipient_user_id": recipient_user_id,
            },
        )
