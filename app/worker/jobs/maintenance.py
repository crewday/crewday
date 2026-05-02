"""Deployment-scope scheduler job bodies."""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.config import get_settings
from app.util.clock import Clock

_log = logging.getLogger("app.worker.scheduler")


def _heartbeat_only_body() -> None:
    """No-op job body — the heartbeat upsert runs after it returns.

    Exists as a distinct function (rather than ``lambda: None``) so
    the scheduler's log output shows the module-qualified name in
    stack traces if something upstream instruments the call.
    """


def _make_approval_ttl_body(clock: Clock) -> Callable[[], None]:
    """Build the 15-min approval-request TTL sweep body (cd-9ghv).

    Factory rather than a bare module-level function so the body
    closes over the scheduler's injected :class:`Clock` — the
    cutoff (``expires_at <= clock.now()``) MUST be driven by the
    same clock the heartbeat uses. A
    :class:`~app.util.clock.FrozenClock` under test (or a future
    simulated-time deployment) would otherwise have a deterministic
    heartbeat timestamp and a non-deterministic sweep cutoff —
    easy to mis-diagnose, pointless to tolerate given how cheap
    the closure is.

    The returned body is a thin adapter around
    :func:`app.worker.tasks.approval_ttl.sweep_expired_approvals`,
    which opens its own UoW (the worker has no ambient session) and
    commits at the end. The sibling
    :func:`_make_idempotency_sweep_body` cites the same rationale.

    The task import is deferred into the closure body so module
    import order stays robust — the task module pulls in
    :mod:`app.domain.agent.approval` (which itself drags in the
    LLM models + the event bus), none of which the standalone
    ``python -m app.worker`` entrypoint otherwise needs at import
    time.
    """

    def _body() -> None:
        from app.worker.tasks.approval_ttl import sweep_expired_approvals

        # The task itself logs the per-tick summary at INFO with
        # ``event=approval.ttl.sweep``; the wrapper does not need to
        # re-emit. Discard the returned report — operators read it
        # off the structured-log stream, not the wrapper's return.
        sweep_expired_approvals(clock=clock)

    return _body


def _make_invite_ttl_body(clock: Clock) -> Callable[[], None]:
    """Build the 15-min invite TTL sweep body (cd-za45).

    Factory rather than a bare module-level function so the body
    closes over the scheduler's injected :class:`Clock` — the cutoff
    (``expires_at <= clock.now()``) MUST be driven by the same clock
    the heartbeat uses. A :class:`~app.util.clock.FrozenClock` under
    test (or a future simulated-time deployment) would otherwise have
    a deterministic heartbeat timestamp and a non-deterministic sweep
    cutoff — easy to mis-diagnose, pointless to tolerate given how
    cheap the closure is. Mirrors the sibling
    :func:`_make_approval_ttl_body` exactly.

    The returned body is a thin adapter around
    :func:`app.worker.tasks.invite_ttl.sweep_expired_invites`,
    which opens its own UoW (the worker has no ambient session) and
    commits at the end.

    The task import is deferred into the closure body so module
    import order stays robust — the task module pulls in
    :mod:`app.domain.identity.membership` (which itself drags in
    permission groups + the event bus), none of which the standalone
    ``python -m app.worker`` entrypoint otherwise needs at import
    time.
    """

    def _body() -> None:
        from app.worker.tasks.invite_ttl import sweep_expired_invites

        # The task itself logs the per-tick summary at INFO with
        # ``event=invite.ttl.sweep``; the wrapper does not need to
        # re-emit. Discard the returned report — operators read it
        # off the structured-log stream, not the wrapper's return.
        sweep_expired_invites(clock=clock)

    return _body


def _make_webhook_dispatch_body(clock: Clock) -> Callable[[], None]:
    """Build the 30 s outbound webhook dispatcher body (cd-q885).

    Factory rather than a bare module-level function so the body
    closes over the scheduler's injected :class:`Clock` — every retry
    cutoff and signature timestamp must be driven by the same clock
    the heartbeat uses, otherwise a :class:`~app.util.clock.FrozenClock`
    under test would have a deterministic heartbeat and a
    non-deterministic dispatch decision.

    The dispatcher itself is the canonical writer for
    ``webhook_delivery`` row state machines (§10 "Retries"); the
    wrapper opens its own UoWs per row, so the body is a thin
    adapter.

    The task import is deferred into the closure body so module
    import order stays robust — the dispatcher pulls in the cipher,
    the secret_envelope repository, and the integrations repository,
    none of which the standalone ``python -m app.worker`` entrypoint
    otherwise needs at import time.
    """

    def _body() -> None:
        from app.worker.tasks.webhook_dispatch import dispatch_due_webhooks

        # The task itself logs the per-tick summary at INFO with
        # ``event=webhook.dispatch.tick``; the wrapper does not need
        # to re-emit. Discard the report — operators read it off the
        # structured-log stream, not the wrapper's return.
        dispatch_due_webhooks(clock=clock)

    return _body


def _make_extract_document_body(clock: Clock) -> Callable[[], None]:
    """Build the 30 s document text-extraction worker body (cd-mo9e).

    Factory rather than a bare module-level function so the body
    closes over the scheduler's injected :class:`Clock` — every
    extraction's ``extracted_at`` + audit ``created_at`` MUST be
    driven by the same clock the heartbeat uses. A
    :class:`~app.util.clock.FrozenClock` under test (or a future
    simulated-time deployment) would otherwise have a deterministic
    heartbeat timestamp and a non-deterministic extraction cutoff.

    The returned body is a thin adapter around
    :func:`app.worker.tasks.extract_document.extract_pending_documents`,
    which opens its own UoW per row (the sweep is deployment-scope,
    cross-tenant) and commits at each row's boundary. Mirrors the
    sibling :func:`_make_webhook_dispatch_body` shape.

    The task import is deferred into the closure body so module
    import order stays robust — the task module pulls in the storage
    factory + the domain extraction service (which itself drags in
    the event types + audit writer), none of which the standalone
    ``python -m app.worker`` entrypoint otherwise needs at import
    time.
    """

    def _body() -> None:
        from app.worker.tasks.extract_document import extract_pending_documents

        # The task itself logs the per-tick summary at INFO with
        # ``event=extract_document.tick``; the wrapper does not need
        # to re-emit. Discard the returned report — operators read it
        # off the structured-log stream, not the wrapper's return.
        extract_pending_documents(clock=clock)

    return _body


def _make_inventory_reorder_body(clock: Clock) -> Callable[[], None]:
    """Build the hourly inventory reorder-point check body."""

    def _body() -> None:
        from app.worker.tasks.inventory_reorder import (
            check_reorder_points_for_all_workspaces,
        )

        report = check_reorder_points_for_all_workspaces(clock=clock)
        _log.info(
            "worker.inventory_reorder.tick.summary",
            extra={
                "event": "worker.inventory_reorder.tick.summary",
                "total_workspaces": report.total_workspaces,
                "total_workspaces_failed": report.total_workspaces_failed,
                "checked_items": report.checked_items,
                "tasks_created": report.tasks_created,
                "events_emitted": report.events_emitted,
            },
        )

    return _body


def _make_retention_rotation_body(clock: Clock) -> Callable[[], None]:
    """Build the daily operational-log retention body."""

    def _body() -> None:
        from app.worker.tasks.privacy import run_retention_rotation

        results = run_retention_rotation(data_dir=get_settings().data_dir, clock=clock)
        _log.info(
            "worker.retention.tick",
            extra={
                "event": "worker.retention.tick",
                "tables": [result.table for result in results],
                "archived_rows": sum(result.archived_rows for result in results),
            },
        )

    return _body


def _make_chat_gateway_sweep_body(clock: Clock) -> Callable[[], None]:
    """Build the 30 s chat-gateway dispatch safety-net body (cd-0gaa).

    Factory rather than a bare module-level function so the body
    closes over the scheduler's injected :class:`Clock` — the cutoff
    (``created_at < clock.now() - 30 s``) MUST be driven by the same
    clock the heartbeat uses; otherwise a
    :class:`~app.util.clock.FrozenClock` under test would have a
    deterministic heartbeat and a non-deterministic sweep cutoff.

    The returned body is a thin adapter around
    :func:`app.worker.tasks.chat_gateway_sweep.sweep_undispatched_messages`,
    which opens its own UoW (the worker has no ambient session) and
    re-publishes ``chat.message.received`` per straggler row. The
    sibling :func:`_make_webhook_dispatch_body` cites the same
    rationale.

    The task import is deferred into the closure body so module
    import order stays robust — the sweep module pulls in the event
    bus singleton + the audit writer, neither of which the
    standalone ``python -m app.worker`` entrypoint otherwise needs at
    import time.
    """

    def _body() -> None:
        from app.worker.tasks.chat_gateway_sweep import sweep_undispatched_messages

        # The task itself logs the per-tick summary at INFO with
        # ``event=chat_gateway.sweep.tick``; the wrapper does not need
        # to re-emit. Discard the report — operators read the counts
        # off the structured-log stream, not the wrapper's return.
        sweep_undispatched_messages(clock=clock)

    return _body


def _make_idempotency_sweep_body(clock: Clock) -> Callable[[], None]:
    """Build the daily ``idempotency_key`` TTL-sweep body (cd-j9l7).

    Factory rather than a bare module-level function so the body
    closes over the scheduler's injected :class:`Clock` — the cutoff
    is ``clock.now() - TTL``, which MUST be driven by the same clock
    the heartbeat uses. Otherwise a :class:`~app.util.clock.FrozenClock`
    under test (or a future simulated-time deployment) would have a
    deterministic heartbeat timestamp and a non-deterministic sweep
    cutoff — easy to mis-diagnose and pointless to tolerate given how
    cheap the closure is.

    The returned body is a thin adapter around
    :func:`app.api.middleware.idempotency.prune_expired_idempotency_keys`:
    the callable opens its own UoW (and therefore its own
    transaction) when no session is passed, so the scheduler wrapper
    does not need to thread one through. The sweeper returns the
    number of rows deleted; we log it at INFO with
    ``event=idempotency.sweep`` so operators can correlate the
    table's steady-state size with the sweep cadence.

    The middleware import is deferred into the closure body so module
    import order stays robust — the middleware module drags in
    :mod:`starlette.middleware`, :mod:`app.api.errors`, and
    :mod:`app.tenancy.middleware`, none of which the standalone
    ``python -m app.worker`` entrypoint otherwise needs.
    """

    def _body() -> None:
        from app.api.middleware.idempotency import prune_expired_idempotency_keys

        deleted = prune_expired_idempotency_keys(now=clock.now())
        _log.info(
            "idempotency sweep completed",
            extra={"event": "idempotency.sweep", "deleted": deleted},
        )

    return _body
