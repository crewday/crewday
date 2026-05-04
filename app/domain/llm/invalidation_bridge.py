"""Postgres LISTEN/NOTIFY bridge for LLM assignment invalidation.

The router cache in :mod:`app.domain.llm.router` already invalidates
itself when a local :class:`~app.events.types.LlmAssignmentChanged`
event reaches its in-process :class:`~app.events.bus.EventBus`.
Multi-worker Postgres deployments need the same event to reach sibling
workers. This module keeps that bridge narrow: it listens only for LLM
assignment changes on the ``llm_assignment`` Postgres channel
and republishes the typed event onto each worker's local bus.

SQLite remains the single-process development path and uses
:class:`NullLlmAssignmentInvalidationBridge`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import Final, Protocol

from sqlalchemy import Engine, text

from app.events.bus import EventBus
from app.events.types import LlmAssignmentChanged

__all__ = [
    "CHANNEL_NAME",
    "LlmAssignmentInvalidationBridge",
    "NullLlmAssignmentInvalidationBridge",
    "PostgresLlmAssignmentInvalidationBridge",
    "build_llm_assignment_invalidation_bridge",
]

_log = logging.getLogger(__name__)

CHANNEL_NAME: Final[str] = "llm_assignment"
_NOTIFY_PAYLOAD_LIMIT: Final[int] = 7900
_SUPPRESS_FORWARD: ContextVar[bool] = ContextVar(
    "llm_assignment_bridge_suppress_forward", default=False
)


class LlmAssignmentInvalidationBridge(Protocol):
    """Lifecycle seam for cross-worker LLM cache invalidation."""

    @property
    def worker_id(self) -> str:
        """Process-unique id used to drop this worker's NOTIFY echo."""
        ...

    async def start(self) -> None:
        """Begin forwarding local changes and listening for remote ones."""
        ...

    async def stop(self) -> None:
        """Stop listening and release resources."""
        ...


class NullLlmAssignmentInvalidationBridge:
    """No-op bridge for SQLite, tests, and explicit single-process paths."""

    def __init__(self) -> None:
        self._worker_id = uuid.uuid4().hex

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _payload(event: LlmAssignmentChanged, *, worker_id: str) -> str:
    """Serialize the workspace-scoped invalidation event for NOTIFY."""
    return json.dumps(
        {
            "worker_id": worker_id,
            "payload": event.model_dump(mode="json"),
        },
        separators=(",", ":"),
    )


def _event_from_payload(raw_payload: str) -> tuple[str, LlmAssignmentChanged] | None:
    try:
        decoded = json.loads(raw_payload)
    except json.JSONDecodeError:
        _log.warning(
            "llm.assignment_bridge: dropping non-JSON NOTIFY payload",
            extra={"event": "llm.assignment_bridge.malformed"},
        )
        return None

    if not isinstance(decoded, dict):
        return None
    worker_id = decoded.get("worker_id")
    payload = decoded.get("payload")
    if not isinstance(worker_id, str) or not isinstance(payload, dict):
        return None

    try:
        occurred_at = payload.get("occurred_at")
        if isinstance(occurred_at, str):
            payload = {**payload, "occurred_at": datetime.fromisoformat(occurred_at)}
        event = LlmAssignmentChanged(**payload)
    except (TypeError, ValueError) as exc:
        _log.warning(
            "llm.assignment_bridge: dropping malformed NOTIFY payload: %s",
            exc,
            extra={"event": "llm.assignment_bridge.invalid_payload"},
        )
        return None
    return worker_id, event


class PostgresLlmAssignmentInvalidationBridge:
    """Postgres LISTEN/NOTIFY bridge for ``LlmAssignmentChanged`` only."""

    def __init__(
        self,
        *,
        engine: Engine,
        bus: EventBus,
        worker_id: str | None = None,
    ) -> None:
        self._engine = engine
        self._bus = bus
        self._worker_id = worker_id or uuid.uuid4().hex
        self._send_lock = threading.Lock()
        self._listen_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._active = False
        self._subscribed = False

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def start(self) -> None:
        if not self._subscribed:
            self._bus.subscribe(LlmAssignmentChanged)(self._forward_local_change)
            self._subscribed = True
        self._active = True
        if self._listen_task is not None:
            return
        self._stopping = False
        self._listen_task = asyncio.create_task(
            self._run(), name="crewday-llm-assignment-bridge"
        )

    async def stop(self) -> None:
        self._stopping = True
        self._active = False
        task = self._listen_task
        self._listen_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception) as exc:
            if not isinstance(exc, asyncio.CancelledError):
                _log.warning(
                    "llm.assignment_bridge: listener exited with error: %s",
                    exc,
                    extra={"event": "llm.assignment_bridge.stop_error"},
                )

    def _forward_local_change(self, event: LlmAssignmentChanged) -> None:
        if not self._active:
            return
        if _SUPPRESS_FORWARD.get():
            return
        try:
            payload = _payload(event, worker_id=self._worker_id)
        except (TypeError, ValueError) as exc:
            _log.warning(
                "llm.assignment_bridge: failed to serialize invalidation: %s",
                exc,
                extra={"event": "llm.assignment_bridge.serialize_failed"},
            )
            return
        if len(payload.encode("utf-8")) > _NOTIFY_PAYLOAD_LIMIT:
            _log.warning(
                "llm.assignment_bridge: dropping oversized NOTIFY (%d bytes)",
                len(payload),
                extra={
                    "event": "llm.assignment_bridge.oversized",
                    "size": len(payload),
                },
            )
            return
        try:
            with self._send_lock, self._engine.connect() as conn:
                conn.execution_options(isolation_level="AUTOCOMMIT").execute(
                    text("SELECT pg_notify(:channel, :payload)"),
                    {"channel": CHANNEL_NAME, "payload": payload},
                )
        except Exception as exc:
            _log.warning(
                "llm.assignment_bridge: pg_notify failed: %s",
                exc,
                extra={"event": "llm.assignment_bridge.send_failed"},
            )

    async def _run(self) -> None:
        import psycopg

        dsn = self._engine.url.set(drivername="postgresql").render_as_string(
            hide_password=False
        )

        backoff = 1.0
        while not self._stopping:
            try:
                async with await psycopg.AsyncConnection.connect(
                    dsn, autocommit=True
                ) as conn:
                    await conn.execute(f"LISTEN {CHANNEL_NAME}")
                    _log.info(
                        "llm.assignment_bridge: listening on channel %s",
                        CHANNEL_NAME,
                        extra={
                            "event": "llm.assignment_bridge.listening",
                            "worker_id": self._worker_id,
                        },
                    )
                    backoff = 1.0
                    async for notify in conn.notifies():
                        if self._stopping:
                            break
                        self._dispatch(notify.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopping:
                    return
                _log.warning(
                    "llm.assignment_bridge: listener errored, retrying in %.1fs: %s",
                    backoff,
                    exc,
                    extra={"event": "llm.assignment_bridge.listen_error"},
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * 2, 30.0)

    def _dispatch(self, raw_payload: str) -> None:
        decoded = _event_from_payload(raw_payload)
        if decoded is None:
            return
        worker_id, event = decoded
        if worker_id == self._worker_id:
            return

        token = _SUPPRESS_FORWARD.set(True)
        try:
            self._bus.publish_local(event)
        except Exception as exc:
            _log.warning(
                "llm.assignment_bridge: local subscriber raised: %s",
                exc,
                extra={"event": "llm.assignment_bridge.subscriber_raised"},
            )
        finally:
            _SUPPRESS_FORWARD.reset(token)


def build_llm_assignment_invalidation_bridge(
    *, engine: Engine, bus: EventBus
) -> LlmAssignmentInvalidationBridge:
    """Pick the LLM invalidation bridge for the active database dialect."""
    if engine.dialect.name == "postgresql":
        return PostgresLlmAssignmentInvalidationBridge(engine=engine, bus=bus)
    return NullLlmAssignmentInvalidationBridge()
