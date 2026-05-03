"""Integration test: Postgres LISTEN/NOTIFY relay (cd-nusy).

Spins up two :class:`PostgresListenNotifyRelay` instances against the
shared session-scoped Postgres engine, simulating two uvicorn workers
on the same DB. An event published on bus A must reach a subscriber
attached to bus B via the relay's LISTEN connection. Self-skip means
bus A's own subscriber never re-fires from the NOTIFY echo.

Two-uvicorn-subprocess test infrastructure is deferred — this case
covers the full LISTEN/NOTIFY contract end-to-end (real psycopg
connection, real Postgres pg_notify) without paying the subprocess
orchestration cost. See ``docs/specs/16-deployment-operations.md``
"Multi-worker behaviour" for the production wiring.

Marker: ``pg_only`` — SQLite cannot speak ``LISTEN/NOTIFY``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine

from app.events.bus import EventBus
from app.events.relay import PostgresListenNotifyRelay
from app.events.types import NotificationCreated, ShiftChanged

pytestmark = [pytest.mark.integration, pytest.mark.pg_only]

_WS = "01HX00000000000000000WS0000"
_ACTOR = "01HX00000000000000000USR000"
_CORR = "01HX00000000000000000COR000"
_SHIFT = "01HX00000000000000000SHF000"
_NOTIF = "01HX00000000000000000NOT000"
_UTC = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _make_notification() -> NotificationCreated:
    return NotificationCreated(
        workspace_id=_WS,
        actor_id=_ACTOR,
        correlation_id=_CORR,
        occurred_at=_UTC,
        notification_id=_NOTIF,
        kind="task_assigned",
        actor_user_id=_ACTOR,
    )


def _make_shift_changed() -> ShiftChanged:
    return ShiftChanged(
        workspace_id=_WS,
        actor_id=_ACTOR,
        correlation_id=_CORR,
        occurred_at=_UTC,
        shift_id=_SHIFT,
        user_id=_ACTOR,
        action="closed",
    )


async def _wait_for(predicate, *, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    """Poll ``predicate`` until it returns truthy or ``timeout`` elapses."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(0.05)


@pytest.fixture
async def two_workers(
    engine: Engine,
) -> AsyncIterator[
    tuple[EventBus, EventBus, PostgresListenNotifyRelay, PostgresListenNotifyRelay]
]:
    """Stand up two bus + relay pairs on the same engine.

    Mimics two uvicorn workers sharing the production Postgres. Each
    relay has its own ``worker_id`` (auto-generated), so the
    self-skip path is exercised naturally.
    """
    bus_a = EventBus()
    bus_b = EventBus()
    relay_a = PostgresListenNotifyRelay(engine=engine, bus=bus_a)
    relay_b = PostgresListenNotifyRelay(engine=engine, bus=bus_b)
    bus_a.set_relay(relay_a)
    bus_b.set_relay(relay_b)
    await relay_a.start()
    await relay_b.start()
    # ``LISTEN`` runs inside the listener task before its first
    # ``conn.notifies()`` await — give psycopg a beat to register
    # so a NOTIFY fired immediately after this fixture yields is
    # actually observed. A short polling loop on a marker NOTIFY
    # would be tighter, but a small constant delay keeps the test
    # readable and the cost is below 100 ms.
    await asyncio.sleep(0.5)
    try:
        yield bus_a, bus_b, relay_a, relay_b
    finally:
        await relay_a.stop()
        await relay_b.stop()


async def test_notification_created_reaches_sibling_worker(
    two_workers: tuple[
        EventBus, EventBus, PostgresListenNotifyRelay, PostgresListenNotifyRelay
    ],
) -> None:
    """The cd-nusy acceptance test, event #1."""
    bus_a, bus_b, _ra, _rb = two_workers
    received_b: list[NotificationCreated] = []
    received_a: list[NotificationCreated] = []
    bus_b.subscribe(NotificationCreated)(lambda e: received_b.append(e))
    bus_a.subscribe(NotificationCreated)(lambda e: received_a.append(e))

    event = _make_notification()
    bus_a.publish(event)

    # Bus A's local subscriber fires immediately (synchronous publish).
    assert len(received_a) == 1

    # Bus B receives via the relay — wait for the LISTEN loop to
    # drain the NOTIFY into the subscriber list.
    await _wait_for(lambda: len(received_b) == 1)

    # Pin the field round-trip on the wire so a future event-class
    # change that drops a field is caught here.
    relayed = received_b[0]
    assert relayed.notification_id == event.notification_id
    assert relayed.workspace_id == event.workspace_id
    assert relayed.actor_user_id == event.actor_user_id
    assert relayed.correlation_id == event.correlation_id

    # A double-fire on bus A would mean the self-skip is broken.
    # Give the relay one extra polling window to fail.
    await asyncio.sleep(0.3)
    assert len(received_a) == 1


async def test_time_shift_changed_reaches_sibling_worker(
    two_workers: tuple[
        EventBus, EventBus, PostgresListenNotifyRelay, PostgresListenNotifyRelay
    ],
) -> None:
    """The cd-nusy acceptance test, event #2."""
    bus_a, bus_b, _ra, _rb = two_workers
    received_b: list[ShiftChanged] = []
    bus_b.subscribe(ShiftChanged)(lambda e: received_b.append(e))

    event = _make_shift_changed()
    bus_a.publish(event)

    await _wait_for(lambda: len(received_b) == 1)
    relayed = received_b[0]
    assert relayed.shift_id == event.shift_id
    assert relayed.action == event.action


async def test_self_originated_notify_does_not_double_fire(
    two_workers: tuple[
        EventBus, EventBus, PostgresListenNotifyRelay, PostgresListenNotifyRelay
    ],
) -> None:
    """``worker_id`` match must drop the NOTIFY echo on the originator."""
    bus_a, _bus_b, _ra, _rb = two_workers
    received_a: list[NotificationCreated] = []
    bus_a.subscribe(NotificationCreated)(lambda e: received_a.append(e))

    bus_a.publish(_make_notification())

    # Local fire is immediate.
    assert len(received_a) == 1
    # A relay-loop bug would deliver the same event a second time
    # within ~100 ms once Postgres routes the NOTIFY back. Give it
    # plenty of time and confirm exactly one delivery.
    await asyncio.sleep(0.5)
    assert len(received_a) == 1
