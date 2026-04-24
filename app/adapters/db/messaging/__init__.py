"""messaging — notification / push_token / digest_record / chat_channel / chat_message.

All five tables in this package are workspace-scoped: each row carries
a ``workspace_id`` column and is registered in
:mod:`app.tenancy.registry` so the ORM tenant filter auto-injects a
``workspace_id`` predicate on every SELECT / UPDATE / DELETE. A bare
read without a :class:`~app.tenancy.WorkspaceContext` raises
:class:`~app.tenancy.orm_filter.TenantFilterMissing`.

This is the cd-pjm v1 slice: a minimum-viable shape for the notification
fanout (§10), the web-push token registry (§12 ``/me/push-tokens`` —
returns ``501 push_unavailable`` until the native app ships), the
daily / weekly email-digest ledger, and the chat-gateway substrate
(§23). The richer surfaces — full ``chat_thread`` model, agent-
dispatch state machine, WhatsApp ``chat_channel_binding`` rows,
per-category ``email_opt_out``, delivery-tracking ``email_delivery``,
etc. — land with follow-ups without breaking this migration's public
write contract.

FK hygiene mirrors the rest of the app:

* ``workspace_id`` → ``workspace.id`` with ``ondelete='CASCADE'`` on
  every row — sweeping a workspace sweeps its messaging history
  (§15 export worker snapshots first).
* ``recipient_user_id`` / ``user_id`` → ``user.id`` with
  ``ondelete='CASCADE'`` — a user's notifications / push tokens /
  digest ledger do not outlive the user. A revoked grant or
  archived user is a distinct concern handled in the domain layer.
* ``ChatMessage.author_user_id`` → ``user.id`` with
  ``ondelete='SET NULL'`` — gateway-inbound rows can have ``NULL``
  authors (the external sender has no user id), and a user delete
  must not nuke the thread history (audit trail survives).
* ``ChatMessage.channel_id`` → ``chat_channel.id`` with
  ``ondelete='CASCADE'`` — deleting a channel sweeps its messages;
  messages are not independently useful once the channel is gone.

See ``docs/specs/02-domain-model.md`` §"user_push_token",
``docs/specs/10-messaging-notifications.md`` for the consumer
contract that drives the indexes (unread fanout, channel scrollback,
daily digests), and ``docs/specs/23-chat-gateway.md`` for the
gateway-inbound semantics (``external_ref``,
``dispatched_to_agent_at``, the channel / message substrate shared
across web + WhatsApp + Telegram).
"""

from __future__ import annotations

from app.adapters.db.messaging.models import (
    ChatChannel,
    ChatMessage,
    DigestRecord,
    Notification,
    PushToken,
)
from app.tenancy.registry import register

for _table in (
    "notification",
    "push_token",
    "digest_record",
    "chat_channel",
    "chat_message",
):
    register(_table)

__all__ = [
    "ChatChannel",
    "ChatMessage",
    "DigestRecord",
    "Notification",
    "PushToken",
]
