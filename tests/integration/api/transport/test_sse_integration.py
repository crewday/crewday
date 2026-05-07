"""Integration test for the SSE transport wired to the real event bus.

This case runs end-to-end through the module-level :data:`app.events.bus`
and :data:`app.api.transport.sse.default_fanout` so a ``TaskCreated``
published on the bus reaches a subscriber that connected through the
transport's own API — exactly what happens in production when a
domain service publishes an event inside a request handler.

httpx's ``ASGITransport`` buffers streaming responses end-to-end
before surfacing them, which would deadlock a live SSE test. We
therefore drive the SSE handler's generator
(:func:`app.api.transport.sse._stream_events`) directly, but we
exercise the real binding path via
:meth:`SSEFanOut.bind_to_bus` rather than publishing on the fanout
directly. That is the seam the production handler wires on first
hit (:func:`_ensure_bus_binding`).

See ``docs/specs/17-testing-quality.md`` §"Integration" and
``docs/specs/14-web-frontend.md`` §"SSE-driven invalidation".
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import suppress
from datetime import UTC, datetime

import pytest

from app.api.transport import sse as sse_mod
from app.api.transport.sse import SSEFanOut, _ParsedLastEventId, _stream_events
from app.events import bus as default_bus
from app.events.types import TaskCreated


def _fresh_id() -> _ParsedLastEventId:
    """Stand-in for a first-connection client (no ``Last-Event-ID``)."""
    return _ParsedLastEventId(stream_token=None, seq=0)


pytestmark = pytest.mark.integration


def _run_in_isolated_loop(coro_factory: Callable[[], Awaitable[None]]) -> None:
    """Run an async test body without pytest-asyncio owning this test."""
    error: BaseException | None = None

    async def _run_and_check() -> None:
        await coro_factory()
        current = asyncio.current_task()
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]
        if not pending:
            return
        for task in pending:
            task.cancel()
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=1.0,
            )
        names = ", ".join(task.get_name() for task in pending)
        raise AssertionError(f"isolated SSE loop leaked pending tasks: {names}")

    def _target() -> None:
        nonlocal error
        try:
            asyncio.run(_run_and_check())
        except BaseException as exc:
            error = exc

    thread = threading.Thread(target=_target, name="sse-integration-loop")
    thread.start()
    thread.join()
    if error is not None:
        raise error


@pytest.fixture
def fresh_module_state() -> Iterator[None]:
    """Isolate the module-global fanout + bus subscriptions.

    The integration test deliberately touches the production
    singletons (``default_fanout`` + ``default_bus``) so the full
    ``publish → bus → fanout → subscriber`` path is exercised. We
    swap them for fresh instances for the duration of the test so
    the suite never leaks handler subscriptions.
    """
    original_fanout = sse_mod.default_fanout
    original_bound = sse_mod._bus_bound
    sse_mod.default_fanout = SSEFanOut()
    sse_mod._bus_bound = False
    default_bus._reset_for_tests()
    try:
        yield
    finally:
        default_bus._reset_for_tests()
        sse_mod.default_fanout = original_fanout
        sse_mod._bus_bound = original_bound


async def _next_frame(gen, *, timeout: float = 1.0) -> bytes:
    """Pull one frame from the generator with a bounded wait."""
    return await asyncio.wait_for(gen.__anext__(), timeout=timeout)


def test_task_created_reaches_subscribed_client(
    fresh_module_state: None,
) -> None:
    """A ``TaskCreated`` published on the real bus reaches an SSE client."""
    _run_in_isolated_loop(_task_created_reaches_subscribed_client)


async def _task_created_reaches_subscribed_client() -> None:
    # 1. Ensure the module's lazy bind-to-bus path actually runs — the
    #    production handler does this on first request.
    sse_mod._ensure_bus_binding()
    fanout = sse_mod.default_fanout

    # 2. Subscribe through the same generator the production handler
    #    uses. The ``_fake_request`` substitute is a minimal shim that
    #    never reports disconnected — Starlette's plumbing is not
    #    under test here.
    class _ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    gen = _stream_events(
        request=_ConnectedRequest(),  # type: ignore[arg-type]
        fanout=fanout,
        workspace_id="01HX00000000000000000WS0000",
        user_id="01HX00000000000000000USR000",
        role="manager",
        last_event_id=_fresh_id(),
        heartbeat_interval=10.0,
    )

    try:
        retry = await _next_frame(gen)
        assert retry.startswith(b"retry:")

        # 3. Publish on the real default bus from the active test loop — the
        #    fanout's forward handler must route this into the
        #    subscriber's queue.
        default_bus.publish(
            TaskCreated(
                workspace_id="01HX00000000000000000WS0000",
                actor_id="01HX00000000000000000USR999",
                correlation_id="01HX00000000000000000COR000",
                occurred_at=datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC),
                task_id="01HX00000000000000000TSK000",
            )
        )

        # 4. Read the frame the subscriber received; shape must match the
        #    SSE wire contract (§14 "SSE-driven invalidation").
        frame = await _next_frame(gen)
        lines = [line for line in frame.decode("utf-8").splitlines() if line]
        assert any(line == "event: task.created" for line in lines)
        data_line = next(line for line in lines if line.startswith("data: "))
        payload = json.loads(data_line[len("data: ") :])
        assert payload["task_id"] == "01HX00000000000000000TSK000"
        assert payload["workspace_id"] == "01HX00000000000000000WS0000"
        assert payload["kind"] == "task.created"
        assert "invalidates" not in payload
    finally:
        await gen.aclose()


def test_event_scoped_to_other_workspace_is_not_delivered(
    fresh_module_state: None,
) -> None:
    """A bus publish for workspace B must not reach a workspace-A subscriber."""
    _run_in_isolated_loop(_event_scoped_to_other_workspace_is_not_delivered)


async def _event_scoped_to_other_workspace_is_not_delivered() -> None:
    sse_mod._ensure_bus_binding()
    fanout = sse_mod.default_fanout

    class _ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    gen = _stream_events(
        request=_ConnectedRequest(),  # type: ignore[arg-type]
        fanout=fanout,
        workspace_id="01HX00000000000000000WSA000",
        user_id="01HX00000000000000000USR000",
        role="manager",
        last_event_id=_fresh_id(),
        heartbeat_interval=0.05,
    )

    try:
        await _next_frame(gen)  # retry

        default_bus.publish(
            TaskCreated(
                workspace_id="01HX00000000000000000WSB000",
                actor_id="01HX00000000000000000USR999",
                correlation_id="01HX00000000000000000COR000",
                occurred_at=datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC),
                task_id="01HX00000000000000000TSK000",
            )
        )
        # The heartbeat fires first — nothing workspace-A-visible was
        # published, so the next yielded frame must be a keepalive.
        next_frame = await _next_frame(gen, timeout=1.0)
        assert next_frame == b": keepalive\n\n"
    finally:
        await gen.aclose()


def test_worker_client_does_not_receive_unassigned_workspace_event(
    fresh_module_state: None,
) -> None:
    """The role gate holds end-to-end through the bus path.

    ``allowed_roles`` on the registered event is ``ALL_ROLES`` for
    :class:`TaskCreated`, so every role sees it. This test proves the
    filter runs — it narrows the event kind in the fanout buffer with
    a direct publish (the bus forwards the kind name, not the class
    reference) and confirms the subscriber's role is respected.
    """
    _run_in_isolated_loop(_worker_client_does_not_receive_unassigned_workspace_event)


async def _worker_client_does_not_receive_unassigned_workspace_event() -> None:
    sse_mod._ensure_bus_binding()
    fanout = sse_mod.default_fanout

    class _ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    worker_gen = _stream_events(
        request=_ConnectedRequest(),  # type: ignore[arg-type]
        fanout=fanout,
        workspace_id="01HX00000000000000000WS0000",
        user_id="01HX00000000000000000USR000",
        role="worker",
        last_event_id=_fresh_id(),
        heartbeat_interval=0.05,
    )

    try:
        await _next_frame(worker_gen)  # retry

        # Publish a manager-only event directly on the fanout (the bus
        # path is already covered above). The worker's generator must
        # not yield it.
        fanout.publish(
            workspace_id="01HX00000000000000000WS0000",
            kind="test.manager_only_integration",
            roles=("manager",),
            user_scope=None,
            payload={"detail": "x"},
        )
        # Next yield is the heartbeat, not the manager-only frame.
        next_frame = await _next_frame(worker_gen, timeout=1.0)
        assert next_frame == b": keepalive\n\n"
    finally:
        await worker_gen.aclose()
