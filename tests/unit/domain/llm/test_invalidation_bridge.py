"""Backend-independent unit tests for the LLM invalidation bridge helpers.

The Postgres LISTEN/NOTIFY bridge in
:mod:`app.domain.llm.invalidation_bridge` carries two pure functions on
its hot path — :func:`_payload` (serialise a ``LlmAssignmentChanged``
into the NOTIFY wire body) and :func:`_event_from_payload` (parse a
received NOTIFY body back into ``(worker_id, event)`` or drop it) — plus
the self-echo guard in
:meth:`PostgresLlmAssignmentInvalidationBridge._dispatch` that keeps a
worker from re-applying its own NOTIFY.

These paths have no SQL: the only Postgres-only coverage lives in
:mod:`tests.domain.llm.test_router` behind a ``pg_only`` skip, so on the
default SQLite gate the malformed-payload and self-echo branches ran
untested. This module drives them directly with an in-memory
:class:`~app.events.bus.EventBus`; the throwaway SQLite engine is never
connected — the bridge only stores it — so every case runs on the
default gate.

See ``docs/specs/11-llm-and-agents.md`` §"Router cache" and
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine

from app.domain.llm.invalidation_bridge import (
    PostgresLlmAssignmentInvalidationBridge,
    _event_from_payload,
    _payload,
)
from app.events.bus import EventBus
from app.events.types import LlmAssignmentChanged
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)


def _make_event() -> LlmAssignmentChanged:
    return LlmAssignmentChanged(
        workspace_id=new_ulid(),
        actor_id=new_ulid(),
        correlation_id=new_ulid(),
        occurred_at=_PINNED,
    )


class TestPayloadRoundTrip:
    """``_payload`` → ``_event_from_payload`` reconstructs the event."""

    def test_round_trip_preserves_worker_id_and_event(self) -> None:
        event = _make_event()
        worker_id = "worker-abc"

        raw = _payload(event, worker_id=worker_id)
        decoded = _event_from_payload(raw)

        assert decoded is not None
        got_worker_id, got_event = decoded
        assert got_worker_id == worker_id
        assert got_event == event
        # ``occurred_at`` survives the ISO-string hop as an aware UTC
        # datetime rather than a bare string — the parser re-hydrates it.
        assert got_event.occurred_at == _PINNED
        assert got_event.occurred_at.tzinfo is not None


class TestMalformedPayloadDropped:
    """Every malformed NOTIFY body drops to ``None`` (never raises)."""

    def test_non_json_returns_none(self) -> None:
        assert _event_from_payload("not-json{{{") is None

    def test_empty_string_returns_none(self) -> None:
        assert _event_from_payload("") is None

    def test_json_but_not_object_returns_none(self) -> None:
        # A JSON array / scalar decodes cleanly but isn't the expected
        # ``{"worker_id": ..., "payload": {...}}`` envelope.
        assert _event_from_payload("[1, 2, 3]") is None
        assert _event_from_payload("42") is None

    def test_missing_worker_id_returns_none(self) -> None:
        event = _make_event()
        assert (
            _event_from_payload('{"payload": ' + event.model_dump_json() + "}") is None
        )

    def test_non_string_worker_id_returns_none(self) -> None:
        event = _make_event()
        raw = '{"worker_id": 7, "payload": ' + event.model_dump_json() + "}"
        assert _event_from_payload(raw) is None

    def test_payload_not_a_dict_returns_none(self) -> None:
        assert _event_from_payload('{"worker_id": "w", "payload": "nope"}') is None

    def test_payload_failing_event_validation_returns_none(self) -> None:
        # Well-formed envelope, but the inner payload is missing the
        # required ``workspace_id`` / ``actor_id`` fields, so the
        # ``LlmAssignmentChanged(**payload)`` construction raises and the
        # helper swallows it into ``None``.
        raw = (
            '{"worker_id": "w", "payload": '
            '{"occurred_at": "2026-04-27T12:00:00+00:00"}}'
        )
        assert _event_from_payload(raw) is None


class TestSelfWorkerEchoSuppressed:
    """``_dispatch`` re-applies foreign NOTIFYs but drops its own echo."""

    def _bridge_with_spy(
        self, *, worker_id: str
    ) -> tuple[PostgresLlmAssignmentInvalidationBridge, list[LlmAssignmentChanged]]:
        bus = EventBus()
        captured: list[LlmAssignmentChanged] = []
        bus.subscribe(LlmAssignmentChanged)(captured.append)
        # The engine is stored but never connected on this path —
        # ``_dispatch`` only touches ``self._worker_id`` and ``self._bus``.
        engine = create_engine("sqlite://")
        bridge = PostgresLlmAssignmentInvalidationBridge(
            engine=engine, bus=bus, worker_id=worker_id
        )
        return bridge, captured

    def test_foreign_worker_notify_is_republished(self) -> None:
        bridge, captured = self._bridge_with_spy(worker_id="self")
        event = _make_event()
        raw = _payload(event, worker_id="other-worker")

        bridge._dispatch(raw)

        assert captured == [event]

    def test_own_worker_notify_is_suppressed(self) -> None:
        bridge, captured = self._bridge_with_spy(worker_id="self")
        event = _make_event()
        # A NOTIFY carrying our own worker id is our own echo: the local
        # bus already applied it at publish time, so re-applying would
        # double-invalidate. The guard drops it.
        raw = _payload(event, worker_id="self")

        bridge._dispatch(raw)

        assert captured == []

    def test_malformed_notify_is_dropped_without_publish(self) -> None:
        bridge, captured = self._bridge_with_spy(worker_id="self")

        bridge._dispatch("not-json")

        assert captured == []
