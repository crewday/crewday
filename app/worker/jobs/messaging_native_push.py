"""Native push delivery worker helpers.

The FCM/APNS send loop is provisioned separately from this slice. This
module owns the vendor-ack lifecycle that the loop calls once a provider
reports a terminal token failure.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.orm import Session

from app.adapters.db.identity.repositories import SqlAlchemyUserPushTokenRepository
from app.domain.identity.push_tokens import disable_after_vendor_ack
from app.util.clock import Clock

__all__ = [
    "TERMINAL_VENDOR_REASONS",
    "handle_native_push_vendor_ack",
]

TERMINAL_VENDOR_REASONS: Final[frozenset[str]] = frozenset(
    {"invalid", "uninstalled", "unregistered"}
)


def handle_native_push_vendor_ack(
    session: Session,
    *,
    token_id: str,
    vendor_reason: str,
    clock: Clock | None = None,
) -> bool:
    """Disable ``user_push_token`` after a terminal FCM/APNS ack.

    ``token_unauthenticated`` is deliberately not terminal by itself;
    the delivery loop retries credential refresh first and calls this
    helper only if the post-retry vendor reason is one of
    :data:`TERMINAL_VENDOR_REASONS`.
    """
    if vendor_reason not in TERMINAL_VENDOR_REASONS:
        return False
    repo = SqlAlchemyUserPushTokenRepository(session)
    return disable_after_vendor_ack(
        repo,
        token_id=token_id,
        vendor_reason=vendor_reason,
        clock=clock,
    )
