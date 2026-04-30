"""Null mailer used by demo deployments."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from app.adapters.mail.ports import Mailer
from app.util.ulid import new_ulid

__all__ = ["NullMailer"]

_log = logging.getLogger(__name__)


class NullMailer(Mailer):
    """Suppress outbound email while preserving Mailer semantics."""

    def send(
        self,
        *,
        to: Sequence[str],
        subject: str,
        body_text: str,
        body_html: str | None = None,
        headers: Mapping[str, str] | None = None,
        reply_to: str | None = None,
    ) -> str:
        del body_text, body_html, headers, reply_to
        message_id = f"demo:{new_ulid()}"
        _log.info(
            "demo email suppressed",
            extra={
                "event": "demo.email.suppressed",
                "recipient_count": len(to),
                "subject_length": len(subject),
                "provider_message_id": message_id,
            },
        )
        return message_id
