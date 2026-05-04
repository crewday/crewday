"""Provider webhook seam for ``email_delivery`` state updates."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.messaging.ports import EmailDeliveryRepository, EmailDeliveryRow

__all__ = [
    "EmailDeliveryWebhookEvent",
    "EmailDeliveryWebhookHandler",
    "ProviderEmailEventError",
    "map_provider_email_event",
]


class ProviderEmailEventError(ValueError):
    """Raised when a provider event name is not part of the supported seam."""


@dataclass(frozen=True, slots=True)
class EmailDeliveryWebhookEvent:
    workspace_id: str
    provider_message_id: str
    event: str
    error_text: str | None = None


_EVENT_STATE_MAP: dict[str, str] = {
    "delivered": "delivered",
    "delivery": "delivered",
    "bounce": "bounced",
    "bounced": "bounced",
    "complaint": "complaint",
    "spam_complaint": "complaint",
    "failed": "failed",
    "failure": "failed",
}


def map_provider_email_event(event: str) -> str:
    normalized = event.strip().lower().replace("-", "_")
    try:
        return _EVENT_STATE_MAP[normalized]
    except KeyError as exc:
        raise ProviderEmailEventError(
            f"unsupported provider email event: {event}"
        ) from exc


@dataclass(slots=True)
class EmailDeliveryWebhookHandler:
    email_deliveries: EmailDeliveryRepository

    def handle(self, event: EmailDeliveryWebhookEvent) -> EmailDeliveryRow | None:
        delivery_state = map_provider_email_event(event.event)
        error_text = event.error_text if delivery_state != "delivered" else None
        row = self.email_deliveries.find_by_provider_message_id(
            workspace_id=event.workspace_id,
            provider_message_id=event.provider_message_id,
        )
        if row is None:
            return None
        return self.email_deliveries.apply_provider_delivery_state(
            workspace_id=event.workspace_id,
            delivery_id=row.id,
            provider_message_id=event.provider_message_id,
            delivery_state=delivery_state,
            error_text=error_text,
        )
