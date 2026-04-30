"""Web-push enqueue glue (cd-y60x).

The §10 web-push delivery worker
(:mod:`app.worker.jobs.messaging_web_push`) consumes the
``notification_push_queue`` staging table.
:class:`~app.domain.messaging.notifications.NotificationService` writes
to that table via the :data:`PushEnqueue` callable seam; this module
provides the production callable.

The enqueue path:

1. Reads every active :class:`~app.adapters.db.messaging.models.\
PushToken` row for the recipient in the workspace.
2. Inserts one ``notification_push_queue`` row per token in
   ``status='pending'`` with ``next_attempt_at = now`` so the next
   worker tick fires the send.

Returns silently when the recipient has no active tokens — the caller
(:class:`NotificationService`) already records that branch as a
distinct skip reason on the audit ledger.

The seam is **synchronous** and rides the caller's open
:class:`~sqlalchemy.orm.Session` so the whole notify call commits
atomically: a failed flush rolls back the inbox row, the SSE
publish, the audit rows, AND the push enqueue together. Once the
caller's UoW commits the worker tick can fire any time after that.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.domain.messaging.ports import (
    PushDeliveryRepository,
    PushTokenRepository,
    PushTokenRow,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "PushEnqueueAdapter",
]


class PushEnqueueAdapter:
    """Bind a :class:`PushDeliveryRepository` + token reader as ``push_enqueue``.

    Constructed at request-handling time (or wherever the
    :class:`NotificationService` is wired) and passed to the service
    via its ``push_enqueue`` field. The adapter is a callable matching
    :data:`~app.domain.messaging.notifications.PushEnqueue`.

    ``token_repo`` is a :class:`PushTokenRepository` reader (used to
    enumerate the recipient's active tokens for the workspace);
    ``delivery_repo`` is the :class:`PushDeliveryRepository` writer
    (used to insert the queue rows). Both repos must share the same
    underlying :class:`~sqlalchemy.orm.Session` as the
    :class:`NotificationService` so the whole notify call commits
    atomically — the audit + queue rows are visible together.
    """

    def __init__(
        self,
        *,
        delivery_repo: PushDeliveryRepository,
        token_repo: PushTokenRepository,
        clock: Clock | None = None,
    ) -> None:
        self._delivery = delivery_repo
        self._tokens = token_repo
        self._clock = clock if clock is not None else SystemClock()

    def __call__(
        self,
        ctx: WorkspaceContext,
        recipient_user_id: str,
        notification_id: str,
        kind: str,
        body: str,
        payload: Mapping[str, Any],
    ) -> None:
        # Snapshot the recipient's active tokens up-front. The service
        # already gated on ``has_tokens`` before reaching us, but the
        # narrow window between the gate and this call could see a
        # concurrent un-register; an empty list is a no-op.
        tokens: Sequence[PushTokenRow] = self._tokens.list_for_user(
            workspace_id=ctx.workspace_id,
            user_id=recipient_user_id,
        )
        if not tokens:
            return

        now = self._clock.now()
        for token in tokens:
            self._delivery.enqueue(
                delivery_id=new_ulid(),
                workspace_id=ctx.workspace_id,
                notification_id=notification_id,
                push_token_id=token.id,
                kind=kind,
                body=body,
                payload_json=dict(payload),
                created_at=now,
                next_attempt_at=now,
            )
