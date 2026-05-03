"""Unit tests for the cross-worker event relay (cd-nusy).

These tests stay backend-agnostic: they exercise the relay's seam
contract (envelope round-trip, self-skip, fire-and-forget on backend
failure, bus-relay wiring) using :class:`NullRelay` and a hand-built
in-memory subclass that captures forwards. The Postgres concretion
(LISTEN/NOTIFY) is exercised by the integration suite, where a real
connection is available.

See ``app/events/relay.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.events.bus import EventBus
from app.events.relay import (
    CHANNEL_NAME,
    NullRelay,
    PostgresListenNotifyRelay,
    _decode_envelope,
    _envelope,
    build_relay,
)
from app.events.types import NotificationCreated, ShiftChanged

# Pinned-shape constants reused across cases.
_WS = "01HX00000000000000000WS0000"
_ACTOR = "01HX00000000000000000USR000"
_CORR = "01HX00000000000000000COR000"
_SHIFT = "01HX00000000000000000SHF000"
_NOTIF = "01HX00000000000000000NOT000"
_UTC = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Envelope round-trip
# ---------------------------------------------------------------------------


class TestEnvelopeRoundTrip:
    """The cd-nusy acceptance contract: typed event → JSON → typed event."""

    def test_notification_created_round_trips(self) -> None:
        original = NotificationCreated(
            workspace_id=_WS,
            actor_id=_ACTOR,
            correlation_id=_CORR,
            occurred_at=_UTC,
            notification_id=_NOTIF,
            kind="task_assigned",
            actor_user_id=_ACTOR,
        )
        wire = _envelope(original, worker_id="worker_a")
        # Sanity: parses as JSON with the documented envelope keys.
        decoded_raw = json.loads(wire)
        assert decoded_raw["kind"] == "notification.created"
        assert decoded_raw["worker_id"] == "worker_a"
        assert decoded_raw["payload"]["notification_id"] == _NOTIF

        decoded = _decode_envelope(wire)
        assert decoded is not None
        kind, worker_id, event = decoded
        assert kind == "notification.created"
        assert worker_id == "worker_a"
        assert isinstance(event, NotificationCreated)
        # Every field on the original instance must survive the trip
        # — losing one would silently corrupt the SSE wire shape on
        # the receiving worker.
        assert event.workspace_id == _WS
        assert event.actor_id == _ACTOR
        assert event.correlation_id == _CORR
        assert event.occurred_at == _UTC
        assert event.notification_id == _NOTIF
        assert event.kind == "task_assigned"
        assert event.actor_user_id == _ACTOR

    def test_shift_changed_round_trips(self) -> None:
        """The second cd-nusy acceptance event."""
        original = ShiftChanged(
            workspace_id=_WS,
            actor_id=_ACTOR,
            correlation_id=_CORR,
            occurred_at=_UTC,
            shift_id=_SHIFT,
            user_id=_ACTOR,
            action="closed",
        )
        decoded = _decode_envelope(_envelope(original, worker_id="worker_b"))
        assert decoded is not None
        _kind, _wid, event = decoded
        assert isinstance(event, ShiftChanged)
        assert event.shift_id == _SHIFT
        assert event.action == "closed"

    def test_unknown_kind_drops(self) -> None:
        wire = json.dumps({"kind": "no.such.event", "worker_id": "w", "payload": {}})
        assert _decode_envelope(wire) is None

    def test_malformed_json_drops(self) -> None:
        assert _decode_envelope("not json at all") is None

    def test_missing_keys_drops(self) -> None:
        assert _decode_envelope(json.dumps({"kind": "x"})) is None
        assert _decode_envelope(json.dumps({"worker_id": "x"})) is None
        assert _decode_envelope(json.dumps([1, 2, 3])) is None

    def test_payload_validation_failure_drops(self) -> None:
        """A NOTIFY whose payload doesn't satisfy the event model.

        For example a sibling worker on a stale build emitting a
        ``NotificationCreated`` without the now-required
        ``actor_user_id`` field. We log + drop rather than raise so
        the listener loop survives a rolling-deploy schema mismatch.
        """
        wire = json.dumps(
            {
                "kind": "notification.created",
                "worker_id": "w",
                "payload": {
                    "workspace_id": _WS,
                    # missing required fields like notification_id, kind,
                    # actor_user_id, etc.
                },
            }
        )
        assert _decode_envelope(wire) is None


# ---------------------------------------------------------------------------
# NullRelay
# ---------------------------------------------------------------------------


class TestNullRelay:
    """SQLite + test path: forward is a no-op, lifecycle returns cleanly."""

    def test_forward_is_no_op(self) -> None:
        relay = NullRelay()
        relay.forward(
            NotificationCreated(
                workspace_id=_WS,
                actor_id=_ACTOR,
                correlation_id=_CORR,
                occurred_at=_UTC,
                notification_id=_NOTIF,
                kind="task_assigned",
                actor_user_id=_ACTOR,
            )
        )

    def test_worker_id_is_per_instance(self) -> None:
        # A fresh process produces a fresh id; building two relays
        # in the same process also yields distinct ids.
        a = NullRelay().worker_id
        b = NullRelay().worker_id
        assert a != b
        assert isinstance(a, str)
        assert len(a) >= 16  # uuid4 hex is 32 chars

    async def test_start_and_stop_are_no_ops(self) -> None:
        relay = NullRelay()
        await relay.start()
        await relay.stop()


# ---------------------------------------------------------------------------
# Bus ↔ relay wiring
# ---------------------------------------------------------------------------


class _CapturingRelay:
    """Hand-built relay that records every forwarded event."""

    def __init__(self, *, worker_id: str = "test_worker") -> None:
        self._worker_id = worker_id
        self.forwarded: list = []
        self.fail = False

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def forward(self, event) -> None:  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("relay backend down")
        self.forwarded.append(event)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class TestBusRelayWiring:
    def _make_event(self) -> NotificationCreated:
        return NotificationCreated(
            workspace_id=_WS,
            actor_id=_ACTOR,
            correlation_id=_CORR,
            occurred_at=_UTC,
            notification_id=_NOTIF,
            kind="task_assigned",
            actor_user_id=_ACTOR,
        )

    def test_publish_forwards_after_local_delivery(self) -> None:
        bus = EventBus()
        relay = _CapturingRelay()
        bus.set_relay(relay)
        seen: list = []
        bus.subscribe(NotificationCreated)(lambda e: seen.append(e))

        event = self._make_event()
        bus.publish(event)

        # Local subscriber fired exactly once.
        assert len(seen) == 1
        # Relay forward fired exactly once.
        assert len(relay.forwarded) == 1
        assert relay.forwarded[0] is event

    def test_publish_local_skips_relay(self) -> None:
        """Receive path must NOT re-forward — that would loop."""
        bus = EventBus()
        relay = _CapturingRelay()
        bus.set_relay(relay)
        seen: list = []
        bus.subscribe(NotificationCreated)(lambda e: seen.append(e))

        event = self._make_event()
        bus.publish_local(event)

        assert len(seen) == 1
        assert relay.forwarded == []  # critical: no loop

    def test_publish_swallows_relay_failure(self) -> None:
        """Local subscribers must still fire when the relay raises.

        The relay is a fire-and-forget cross-worker mirror; a backend
        outage must never roll back the publishing UoW.
        """
        bus = EventBus()
        relay = _CapturingRelay()
        relay.fail = True
        bus.set_relay(relay)
        seen: list = []
        bus.subscribe(NotificationCreated)(lambda e: seen.append(e))

        event = self._make_event()
        bus.publish(event)  # should NOT raise

        assert len(seen) == 1

    def test_set_relay_none_returns_to_in_process_mode(self) -> None:
        bus = EventBus()
        relay = _CapturingRelay()
        bus.set_relay(relay)
        bus.set_relay(None)

        bus.publish(self._make_event())
        assert relay.forwarded == []


# ---------------------------------------------------------------------------
# Listener self-skip via worker_id
# ---------------------------------------------------------------------------


class TestSelfSkip:
    """The receive loop must drop NOTIFYs the same worker emitted.

    Otherwise every event would be delivered twice on the
    originating worker (once locally, once via the relay's own
    NOTIFY echo).
    """

    def test_dispatch_drops_self_originated_envelope(self) -> None:
        bus = EventBus()
        delivered: list = []
        bus.subscribe(NotificationCreated)(lambda e: delivered.append(e))

        # Build a relay without starting it; we drive ``_dispatch``
        # by hand so the test stays a pure unit and never touches
        # psycopg.
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        relay = PostgresListenNotifyRelay(engine=engine, bus=bus, worker_id="self")

        # Self-originated envelope: must NOT reach the local bus.
        own_payload = _envelope(
            NotificationCreated(
                workspace_id=_WS,
                actor_id=_ACTOR,
                correlation_id=_CORR,
                occurred_at=_UTC,
                notification_id=_NOTIF,
                kind="task_assigned",
                actor_user_id=_ACTOR,
            ),
            worker_id="self",
        )
        relay._dispatch(own_payload)
        assert delivered == []

        # Sibling-originated envelope: must reach the local bus.
        sibling_payload = _envelope(
            NotificationCreated(
                workspace_id=_WS,
                actor_id=_ACTOR,
                correlation_id=_CORR,
                occurred_at=_UTC,
                notification_id=_NOTIF,
                kind="task_assigned",
                actor_user_id=_ACTOR,
            ),
            worker_id="other_worker",
        )
        relay._dispatch(sibling_payload)
        assert len(delivered) == 1

    def test_dispatch_drops_malformed_payload_silently(self) -> None:
        """A garbage NOTIFY (e.g. external pg_notify probe) is logged + dropped."""
        bus = EventBus()
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        relay = PostgresListenNotifyRelay(engine=engine, bus=bus)
        # Should not raise, nothing to assert beyond "no exception".
        relay._dispatch("not a json envelope")
        relay._dispatch(json.dumps({"missing": "keys"}))

    def test_dispatch_swallows_subscriber_exception(self) -> None:
        """A buggy local subscriber must not tear down the receive loop."""
        bus = EventBus()
        bus.subscribe(NotificationCreated)(
            lambda e: (_ for _ in ()).throw(RuntimeError("subscriber broken"))
        )
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        relay = PostgresListenNotifyRelay(engine=engine, bus=bus)
        sibling_payload = _envelope(
            NotificationCreated(
                workspace_id=_WS,
                actor_id=_ACTOR,
                correlation_id=_CORR,
                occurred_at=_UTC,
                notification_id=_NOTIF,
                kind="task_assigned",
                actor_user_id=_ACTOR,
            ),
            worker_id="other",
        )
        # Must not raise — the receive loop survives one bad handler.
        relay._dispatch(sibling_payload)


# ---------------------------------------------------------------------------
# build_relay dispatcher
# ---------------------------------------------------------------------------


class TestBuildRelay:
    def test_in_process_mode_returns_null_relay_regardless_of_dialect(self) -> None:
        bus = EventBus()
        for dialect_name in ("sqlite", "postgresql", "mysql"):
            engine = MagicMock()
            engine.dialect.name = dialect_name
            relay = build_relay(engine=engine, bus=bus, mode="in_process")
            assert isinstance(relay, NullRelay)

    def test_auto_mode_picks_listen_notify_on_postgres(self) -> None:
        bus = EventBus()
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        relay = build_relay(engine=engine, bus=bus, mode="auto")
        assert isinstance(relay, PostgresListenNotifyRelay)

    def test_auto_mode_picks_null_on_sqlite(self) -> None:
        bus = EventBus()
        engine = MagicMock()
        engine.dialect.name = "sqlite"
        relay = build_relay(engine=engine, bus=bus, mode="auto")
        assert isinstance(relay, NullRelay)

    def test_postgres_mode_refuses_non_pg_dialect(self) -> None:
        bus = EventBus()
        engine = MagicMock()
        engine.dialect.name = "sqlite"
        with pytest.raises(ValueError, match="PostgreSQL"):
            build_relay(engine=engine, bus=bus, mode="postgres")

    def test_unknown_mode_raises(self) -> None:
        bus = EventBus()
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        with pytest.raises(ValueError, match="Unknown events_relay"):
            build_relay(engine=engine, bus=bus, mode="redis")


# ---------------------------------------------------------------------------
# Channel name pin (catches an accidental rename that would silently
# split clients across two channels in a rolling deploy).
# ---------------------------------------------------------------------------


def test_channel_name_is_stable() -> None:
    assert CHANNEL_NAME == "crewday_events"


# ---------------------------------------------------------------------------
# Forward serialise + size paths (no DB needed)
# ---------------------------------------------------------------------------


class TestForwardSendPath:
    def test_oversized_payload_is_dropped_without_calling_engine(self) -> None:
        bus = EventBus()
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        relay = PostgresListenNotifyRelay(engine=engine, bus=bus)

        # Cook a payload that, when serialised, exceeds the cap.
        # ``NotificationCreated.kind`` is plain text; pad it past the
        # 7900-byte limit so the relay short-circuits.
        oversized_kind = "x" * 8000
        event = NotificationCreated(
            workspace_id=_WS,
            actor_id=_ACTOR,
            correlation_id=_CORR,
            occurred_at=_UTC,
            notification_id=_NOTIF,
            kind=oversized_kind,
            actor_user_id=_ACTOR,
        )
        relay.forward(event)
        # Engine must not have been touched — the size guard fires
        # before any connection is opened.
        engine.connect.assert_not_called()

    def test_engine_failure_is_swallowed(self) -> None:
        """A DB failure mid-publish must not propagate to the caller."""
        bus = EventBus()
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        engine.connect.side_effect = RuntimeError("DB down")
        relay = PostgresListenNotifyRelay(engine=engine, bus=bus)

        event = NotificationCreated(
            workspace_id=_WS,
            actor_id=_ACTOR,
            correlation_id=_CORR,
            occurred_at=_UTC,
            notification_id=_NOTIF,
            kind="task_assigned",
            actor_user_id=_ACTOR,
        )
        # Should not raise.
        relay.forward(event)
