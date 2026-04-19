"""In-memory :class:`~app.adapters.mail.ports.Mailer` fake.

Records every ``send(...)`` call into ``self.sent`` and returns
deterministic message ids (``msg-1``, ``msg-2``, ...). Satisfies the
``Mailer`` port structurally.

See ``docs/specs/17-testing-quality.md`` §"Unit".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = ["InMemoryMailer", "SentMessage"]


@dataclass(frozen=True, slots=True)
class SentMessage:
    """One recorded ``send`` call. Tuple fields are frozen for easy equality."""

    to: tuple[str, ...]
    subject: str
    body_text: str
    body_html: str | None
    headers: Mapping[str, str]
    reply_to: str | None


class InMemoryMailer:
    """List-backed :class:`~app.adapters.mail.ports.Mailer`.

    ``send`` returns ``msg-<N>`` where ``N`` is the 1-based index of the
    record in :attr:`sent`. Any non-string headers are coerced via
    :class:`dict` so downstream code can mutate without affecting the
    record.
    """

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []

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
        self.sent.append(
            SentMessage(
                to=tuple(to),
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                headers=dict(headers or {}),
                reply_to=reply_to,
            )
        )
        return f"msg-{len(self.sent)}"
