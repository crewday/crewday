"""Deployment-scoped Server-Sent Events transport at ``/admin/events``.

This is the deployment twin of :mod:`app.api.transport.sse`: one
``EventSource('/admin/events')`` carries admin-console invalidation
signals that are not tied to a workspace context. The workspace event
model deliberately stays workspace-only; admin publishers call
:func:`publish_admin_event` with deployment-scope payloads.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import AsyncGenerator, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from starlette.responses import StreamingResponse

from app.api.admin.deps import current_deployment_admin_principal
from app.api.transport.sse import (
    HEARTBEAT_INTERVAL_S,
    MAX_CLIENT_QUEUE,
    REPLAY_WINDOW_S,
    _client_disconnected,
    _effective_replay_seq,
    _format_sse_frame,
    _parse_last_event_id,
    _ParsedLastEventId,
)
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import DeploymentAdminSSEEvent
from app.tenancy import DeploymentContext
from app.util.ulid import new_ulid

__all__ = [
    "AdminSSEFanOut",
    "default_admin_fanout",
    "publish_admin_event",
    "router",
]

_log = logging.getLogger(__name__)

_RECONNECT_MS: Final[int] = 3000
_DEPLOYMENT_ADMIN_WORKSPACE_ID: Final[str] = "__deployment_admin__"

_AdminCtx = Annotated[DeploymentContext, Depends(current_deployment_admin_principal)]


@dataclass(frozen=True)
class _BufferedAdminEvent:
    event_id: int
    kind: str
    user_scope: str | None
    emitted_at_monotonic: float
    wire_bytes: bytes


@dataclass
class _AdminSubscriber:
    user_id: str
    queue: asyncio.Queue[bytes]
    loop: asyncio.AbstractEventLoop
    dropped: bool = False

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return other is self


@dataclass
class _AdminState:
    next_id: int = 0
    buffer: deque[_BufferedAdminEvent] = field(default_factory=deque)
    subscribers: set[_AdminSubscriber] = field(default_factory=set)


class AdminSSEFanOut:
    """Per-process fan-out for the deployment admin SSE stream."""

    def __init__(self) -> None:
        self._state = _AdminState()
        self._lock = threading.Lock()
        self._stream_token = secrets.token_hex(4)

    def bind_to_bus(self, event_bus: EventBus) -> None:
        event_bus.subscribe(DeploymentAdminSSEEvent)(self._forward)

    def _forward(self, event: DeploymentAdminSSEEvent) -> None:
        self.publish(
            kind=event.admin_kind,
            payload=dict(event.payload),
            user_scope=event.user_scope,
        )

    def publish(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        user_scope: str | None = None,
    ) -> None:
        """Append one deployment event and wake matching subscribers."""
        now = time.monotonic()
        with self._lock:
            self._state.next_id += 1
            event_id = self._state.next_id
            payload.setdefault("kind", kind)
            payload.setdefault("invalidates", _default_admin_invalidates(kind))
            wire_bytes = _format_sse_frame(
                event_id=f"{self._stream_token}-{event_id}",
                kind=kind,
                payload=payload,
            )
            self._state.buffer.append(
                _BufferedAdminEvent(
                    event_id=event_id,
                    kind=kind,
                    user_scope=user_scope,
                    emitted_at_monotonic=now,
                    wire_bytes=wire_bytes,
                )
            )
            self._prune(now=now)
            targets = tuple(self._state.subscribers)

        try:
            publisher_loop = asyncio.get_running_loop()
        except RuntimeError:
            publisher_loop = None

        for sub in targets:
            if sub.dropped:
                continue
            if user_scope is not None and sub.user_id != user_scope:
                continue
            self._deliver(sub, wire_bytes, publisher_loop=publisher_loop)

    def subscribe(self, *, user_id: str) -> _AdminSubscriber:
        sub = _AdminSubscriber(
            user_id=user_id,
            queue=asyncio.Queue(maxsize=MAX_CLIENT_QUEUE),
            loop=asyncio.get_running_loop(),
        )
        with self._lock:
            self._state.subscribers.add(sub)
        return sub

    def unsubscribe(self, *, subscriber: _AdminSubscriber) -> None:
        with self._lock:
            self._state.subscribers.discard(subscriber)

    def replay_since(
        self,
        *,
        last_event_id: _ParsedLastEventId,
        user_id: str,
    ) -> Iterable[bytes]:
        seq_cutoff = _effective_replay_seq(last_event_id, self._stream_token)
        with self._lock:
            self._prune(now=time.monotonic())
            return tuple(
                event.wire_bytes
                for event in self._state.buffer
                if event.event_id > seq_cutoff
                and (event.user_scope is None or event.user_scope == user_id)
            )

    @property
    def stream_token(self) -> str:
        return self._stream_token

    @staticmethod
    def _deliver(
        sub: _AdminSubscriber,
        wire_bytes: bytes,
        *,
        publisher_loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        if publisher_loop is sub.loop:
            try:
                sub.queue.put_nowait(wire_bytes)
            except asyncio.QueueFull:
                sub.dropped = True
                _log.warning(
                    "admin sse client dropped (queue full)",
                    extra={"event": "admin_sse.client_dropped", "user_id": sub.user_id},
                )
            return

        def _put() -> None:
            try:
                sub.queue.put_nowait(wire_bytes)
            except asyncio.QueueFull:
                sub.dropped = True
                _log.warning(
                    "admin sse client dropped (queue full)",
                    extra={"event": "admin_sse.client_dropped", "user_id": sub.user_id},
                )

        try:
            sub.loop.call_soon_threadsafe(_put)
        except RuntimeError:
            sub.dropped = True

    def _prune(self, *, now: float) -> None:
        cutoff = now - REPLAY_WINDOW_S
        buf = self._state.buffer
        while buf and buf[0].emitted_at_monotonic < cutoff:
            buf.popleft()


_INVALIDATIONS: Final[dict[str, tuple[tuple[str, ...], ...]]] = {
    "admin.audit.appended": (("admin", "audit"),),
    "admin.usage.updated": (("admin", "usage"),),
    "admin.workspace.archived": (("admin", "workspaces"), ("admin", "usage")),
    "admin.workspace.trusted": (("admin", "workspaces"),),
    "admin.settings.updated": (("admin", "settings"),),
    "admin.admins.updated": (("admin", "admins"),),
    "admin.llm.assignment_updated": (("admin", "llm"),),
    "agent.message.appended": (),
    "agent.action.pending": (),
    "agent.turn.started": (),
    "agent.turn.finished": (),
}


def _default_admin_invalidates(kind: str) -> list[list[str]]:
    return [list(prefix) for prefix in _INVALIDATIONS.get(kind, ())]


default_admin_fanout = AdminSSEFanOut()
_bus_bound = False
_bus_bind_lock = threading.Lock()


def _ensure_bus_binding() -> None:
    global _bus_bound
    if _bus_bound:
        return
    with _bus_bind_lock:
        if _bus_bound:
            return
        default_admin_fanout.bind_to_bus(default_event_bus)
        _bus_bound = True


def publish_admin_event(
    *,
    kind: str,
    ctx: DeploymentContext,
    request: Request,
    payload: Mapping[str, Any] | None = None,
    user_scope: str | None = None,
) -> None:
    """Publish one JSON-safe deployment-scope SSE event."""
    correlation_id = (
        request.headers.get("X-Request-Id")
        or request.headers.get("X-Correlation-Id")
        or new_ulid()
    )
    occurred_at = datetime.now(UTC)
    body = dict(payload or {})
    body.setdefault("actor_id", ctx.user_id)
    body.setdefault("correlation_id", correlation_id)
    body.setdefault("occurred_at", occurred_at.isoformat())
    _ensure_bus_binding()
    default_event_bus.publish(
        DeploymentAdminSSEEvent(
            workspace_id=_DEPLOYMENT_ADMIN_WORKSPACE_ID,
            actor_id=ctx.user_id,
            correlation_id=correlation_id,
            occurred_at=occurred_at,
            admin_kind=kind,
            payload=body,
            user_scope=user_scope,
        )
    )


async def _stream_admin_events(
    *,
    request: Request,
    fanout: AdminSSEFanOut,
    user_id: str,
    last_event_id: _ParsedLastEventId,
    heartbeat_interval: float,
) -> AsyncGenerator[bytes]:
    subscriber = fanout.subscribe(user_id=user_id)
    try:
        yield f"retry: {_RECONNECT_MS}\n\n".encode()
        for frame in fanout.replay_since(
            last_event_id=last_event_id,
            user_id=user_id,
        ):
            yield frame

        while True:
            if subscriber.dropped:
                yield b"event: dropped\ndata: {}\n\n"
                return
            if await _client_disconnected(request):
                return

            try:
                frame = await asyncio.wait_for(
                    subscriber.queue.get(),
                    timeout=heartbeat_interval,
                )
            except TimeoutError:
                yield b": keepalive\n\n"
                continue

            yield frame
            if subscriber.dropped:
                yield b"event: dropped\ndata: {}\n\n"
                return
    finally:
        fanout.unsubscribe(subscriber=subscriber)


router = APIRouter(tags=["transport", "admin"])


@router.get(
    "/events",
    include_in_schema=True,
    summary="Deployment-admin Server-Sent Events stream",
    operation_id="transport.admin_events",
)
async def admin_events(request: Request, ctx: _AdminCtx) -> Response:
    _ensure_bus_binding()
    last_event_id = _parse_last_event_id(request.headers.get("last-event-id"))
    generator = _stream_admin_events(
        request=request,
        fanout=default_admin_fanout,
        user_id=ctx.user_id,
        last_event_id=last_event_id,
        heartbeat_interval=HEARTBEAT_INTERVAL_S,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
