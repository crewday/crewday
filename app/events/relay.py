"""Cross-worker event relay (cd-nusy).

The in-process :mod:`app.events.bus` only reaches subscribers on the
publishing worker. Under a multi-worker deploy (``uvicorn --workers N``
or horizontally scaled replicas) an event published on worker A never
reaches an SSE subscriber connected to worker B — the bell-menu
invalidation, the schedule refresh, the agent turn indicator all stop
at the publishing worker's process boundary.

The relay closes that gap by mirroring every locally published event
out to the other workers. The contract is intentionally narrow:

* **Always-local-first.** :meth:`EventBus.publish` calls every local
  subscriber first; the relay only kicks in afterwards. A relay
  failure (DB unreachable, channel closed mid-write) never affects
  the publisher's own UoW — at worst sibling workers miss one SSE
  invalidation, which their clients pick up on the next reconnect.
* **One transport per process.** The factory chooses a relay at
  startup based on the active SQLAlchemy dialect. SQLite is
  single-worker by definition (file-locked, dev-only) → no relay
  needed. Postgres → ``LISTEN/NOTIFY`` on a dedicated channel.
* **No external infrastructure.** Spec §16 explicitly rules out
  Redis / NATS for v1; the relay must use what we already have.
* **Best-effort, fire-and-forget.** SSE invalidation is by nature
  recoverable (the SPA refetches on reconnect). The relay logs
  failures and moves on.

The :class:`EventRelay` Protocol is the boundary every backend
honours. :class:`NullRelay` is the SQLite / test no-op;
:class:`PostgresListenNotifyRelay` is the Postgres concretion.

Lifecycle is owned by :func:`build_relay` (selection +
construction) and the application factory's lifespan
(:meth:`EventRelay.start` / :meth:`stop`).

See also:

* ``docs/specs/12-rest-api.md`` §"REST API → SSE" — wire-side
  consumer.
* ``docs/specs/16-deployment-operations.md`` §"Deployment recipes" —
  multi-worker behaviour depends on Postgres LISTEN/NOTIFY.
* ``docs/specs/01-architecture.md`` §"Boundary rules" #3 — the
  bus is the single in-process delivery seam; the relay sits *behind*
  it (after local fan-out) rather than in front.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from typing import Final, Protocol

from sqlalchemy import Engine, text

from app.events.bus import EventBus
from app.events.registry import Event, EventNotFound, get_event_type

__all__ = [
    "CHANNEL_NAME",
    "EventRelay",
    "NullRelay",
    "PostgresListenNotifyRelay",
    "build_relay",
]

_log = logging.getLogger(__name__)


# Single Postgres NOTIFY channel for every event kind. The payload is
# a JSON envelope that carries the kind name, the originating worker
# id (for self-skip), and the typed event payload. One channel keeps
# ``LISTEN`` setup trivial — adding a second would only fan in to the
# same handler anyway.
CHANNEL_NAME: Final[str] = "crewday_events"

# Postgres' NOTIFY payload size cap is 8000 bytes. Events on the bus
# carry foreign-key IDs + small scalars (no rendered chat text, no
# attachments — those are fetched via REST), so realistic payloads
# stay under 1 KB. The check below logs and drops oversized payloads
# defensively rather than letting psycopg raise mid-publish — losing
# one cross-worker SSE invalidation is preferable to a crash, and
# the local subscriber path already fired.
_NOTIFY_PAYLOAD_LIMIT: Final[int] = 7900  # leave headroom for PG envelope


def _new_worker_id() -> str:
    """Generate a fresh per-process worker identifier.

    UUID4 hex (32 chars) — short enough for the JSON envelope, large
    enough that two workers never collide in a deploy lifetime.
    """
    return uuid.uuid4().hex


class EventRelay(Protocol):
    """Cross-worker event relay seam.

    Implementations must be safe to call ``forward`` from any thread
    (the bus is synchronous and may fire from APScheduler workers).
    ``start`` and ``stop`` are async-context lifecycle hooks the
    application factory invokes from its lifespan.
    """

    @property
    def worker_id(self) -> str:
        """Process-unique id used to skip self-originated NOTIFYs.

        Stamped onto every outgoing envelope. The receive loop drops
        envelopes whose ``worker_id`` matches its own — the publisher
        already fired local subscribers before calling
        :meth:`forward`, so a re-fan-out would double-deliver every
        event in the single-worker case.
        """
        ...

    def forward(self, event: Event) -> None:
        """Best-effort cross-worker mirror of ``event``.

        Called by :meth:`EventBus.publish` after every local
        subscriber has fired. Must never raise — a failure here is
        logged and dropped so the publisher's UoW keeps progressing.
        """
        ...

    async def start(self) -> None:
        """Begin the receive loop (no-op for the null relay)."""
        ...

    async def stop(self) -> None:
        """Cancel the receive loop and release resources."""
        ...


class NullRelay:
    """No-op relay. Used on SQLite, in unit tests, and as a safe default.

    The :attr:`worker_id` is still unique per process so the bus dedup
    contract holds even when a future test exercises the receive path
    against this relay (it never delivers anything, but a recipient
    comparing against this id would still see a match).
    """

    def __init__(self) -> None:
        self._worker_id = _new_worker_id()

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def forward(self, event: Event) -> None:
        del event  # in-process bus already delivered it locally

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _envelope(event: Event, *, worker_id: str) -> str:
    """Serialise ``event`` into the JSON envelope sent over NOTIFY.

    Shape::

        {
            "kind": "<event.name>",
            "worker_id": "<originator>",
            "payload": { …model_dump(mode="json")… }
        }

    The ``mode="json"`` dump makes :class:`datetime` fields ISO-8601
    strings — matching the SSE wire shape so the receiver can call
    ``cls(**payload)`` straight back into a typed event without a
    second normalisation pass.
    """
    return json.dumps(
        {
            "kind": type(event).name,
            "worker_id": worker_id,
            "payload": event.model_dump(mode="json"),
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _decode_envelope(raw: str) -> tuple[str, str, Event] | None:
    """Parse a NOTIFY payload into ``(kind, worker_id, event)``.

    Returns ``None`` if the payload is malformed or names an unknown
    event class — both are treated as "drop and log" rather than
    raised so a botched message from a sibling worker (mid-deploy
    schema mismatch, accidental external NOTIFY) doesn't kill the
    receive loop.
    """
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        _log.warning(
            "events.relay: dropping non-JSON NOTIFY payload",
            extra={"event": "events.relay.malformed"},
        )
        return None

    if not isinstance(decoded, dict):
        return None
    kind = decoded.get("kind")
    worker_id = decoded.get("worker_id")
    payload = decoded.get("payload")
    if not isinstance(kind, str) or not isinstance(worker_id, str):
        return None
    if not isinstance(payload, dict):
        return None

    try:
        cls = get_event_type(kind)
    except EventNotFound:
        _log.warning(
            "events.relay: dropping NOTIFY for unknown event kind %r",
            kind,
            extra={"event": "events.relay.unknown_kind", "kind": kind},
        )
        return None

    try:
        event = cls(**payload)
    except (TypeError, ValueError) as exc:
        _log.warning(
            "events.relay: dropping malformed NOTIFY payload for %r: %s",
            kind,
            exc,
            extra={"event": "events.relay.invalid_payload", "kind": kind},
        )
        return None

    return kind, worker_id, event


class PostgresListenNotifyRelay:
    """Postgres ``LISTEN/NOTIFY`` based relay.

    Architecture:

    * **Send.** :meth:`forward` opens a short-lived autocommit
      connection from the supplied :class:`Engine` and issues
      ``SELECT pg_notify(:channel, :payload)``. We deliberately do
      NOT reuse the listener's dedicated connection — psycopg's async
      connection is bound to the receive loop, and writing from the
      publisher thread would race the loop. Pool checkout cost on
      Postgres is in the sub-ms range; the simplicity is worth it.
    * **Receive.** :meth:`start` opens a separate psycopg async
      connection in autocommit mode, runs ``LISTEN crewday_events``,
      and spawns an asyncio task that drains notifications and
      republishes them on the local bus via
      :meth:`EventBus.publish_local` (the path that skips the relay
      to avoid loops). Self-originated NOTIFYs are skipped by
      ``worker_id`` match.

    The receive connection is held forever (LISTEN holds the socket).
    Closing it on :meth:`stop` is the only way to release it; the
    application factory's lifespan owns that call.

    psycopg async support: ``psycopg[binary]>=3`` ships
    :mod:`psycopg` with native asyncio. A polling fallback would be
    feasible but the async LISTEN path is the documented happy path
    in psycopg 3 and exercised by their own test suite.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        bus: EventBus,
        worker_id: str | None = None,
    ) -> None:
        self._engine = engine
        self._bus = bus
        self._worker_id = worker_id or _new_worker_id()
        self._listen_task: asyncio.Task[None] | None = None
        # The listener connection is opened inside :meth:`_run`. We
        # stash a reference here so :meth:`stop` can close it from
        # the lifespan thread; psycopg's async connection is not
        # safe to close from outside its loop, but cancelling the
        # task and awaiting it lets the ``finally`` close path run
        # on the right loop.
        self._listen_loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        # The send path can be invoked from any thread; serialise
        # short-lived send connections so a burst of publishes
        # doesn't open dozens of psycopg connections in flight.
        self._send_lock = threading.Lock()

    @property
    def worker_id(self) -> str:
        return self._worker_id

    # ---- Send path ---------------------------------------------------

    def forward(self, event: Event) -> None:
        try:
            payload = _envelope(event, worker_id=self._worker_id)
        except (TypeError, ValueError) as exc:
            # ``model_dump`` rarely fails, but a custom ``__init__``
            # overriding it could. Log + drop — the local fan-out
            # already happened.
            _log.warning(
                "events.relay: failed to serialise %s for relay: %s",
                type(event).__name__,
                exc,
                extra={
                    "event": "events.relay.serialise_failed",
                    "kind": type(event).name,
                },
            )
            return

        if len(payload.encode("utf-8")) > _NOTIFY_PAYLOAD_LIMIT:
            _log.warning(
                "events.relay: dropping oversized NOTIFY (%d bytes) for %s",
                len(payload),
                type(event).name,
                extra={
                    "event": "events.relay.oversized",
                    "kind": type(event).name,
                    "size": len(payload),
                },
            )
            return

        try:
            with self._send_lock, self._engine.connect() as conn:
                # ``pg_notify`` lets us pass the channel + payload
                # as bind parameters — safer than an interpolated
                # ``NOTIFY <channel>, '<payload>'`` literal which
                # would need careful escaping.
                conn.execution_options(isolation_level="AUTOCOMMIT").execute(
                    text("SELECT pg_notify(:channel, :payload)"),
                    {"channel": CHANNEL_NAME, "payload": payload},
                )
        except Exception as exc:
            # Any DB error — connection lost, NOTIFY size cap hit
            # despite the pre-check, transient failure — must NOT
            # propagate to the publisher. The local subscribers
            # already fired; the cross-worker fan-out for this one
            # event is forfeit.
            _log.warning(
                "events.relay: pg_notify failed for %s: %s",
                type(event).name,
                exc,
                extra={
                    "event": "events.relay.send_failed",
                    "kind": type(event).name,
                },
            )

    # ---- Receive path ------------------------------------------------

    async def start(self) -> None:
        if self._listen_task is not None:
            return
        self._stopping = False
        self._listen_loop = asyncio.get_running_loop()
        self._listen_task = asyncio.create_task(
            self._run(), name="crewday-events-relay"
        )

    async def stop(self) -> None:
        self._stopping = True
        task = self._listen_task
        self._listen_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception) as exc:
            # Receive task already logs its own failures; we swallow
            # here so shutdown stays tidy.
            if not isinstance(exc, asyncio.CancelledError):
                _log.warning(
                    "events.relay: listener exited with error: %s",
                    exc,
                    extra={"event": "events.relay.stop_error"},
                )

    async def _run(self) -> None:
        """Long-lived receive loop. One iteration = one notification.

        psycopg's async connection is opened from the asyncio loop
        the application factory's lifespan runs on. We wrap the body
        in a ``while not self._stopping`` so a transient failure
        (server restart, network blip) reconnects rather than
        leaving the worker permanently deaf to sibling events.
        """
        # Imported here so the relay module stays importable on
        # machines that haven't installed psycopg's binary wheel.
        # The application factory only constructs this class when
        # the active dialect is Postgres, where the dependency is
        # guaranteed (see :func:`build_relay`).
        import psycopg

        # SQLAlchemy URL → psycopg DSN. The ``+driver`` tag is
        # SQLAlchemy syntax that psycopg's own connect doesn't
        # parse; rebuild the URL with a bare ``postgresql`` driver so
        # ``psycopg.AsyncConnection.connect`` gets a libpq-shaped
        # ``postgresql://`` DSN regardless of which sync driver
        # SQLAlchemy is using (``+psycopg``, ``+psycopg2``, …).
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
                        "events.relay: listening on channel %s",
                        CHANNEL_NAME,
                        extra={
                            "event": "events.relay.listening",
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
                    "events.relay: listener errored, retrying in %.1fs: %s",
                    backoff,
                    exc,
                    extra={"event": "events.relay.listen_error"},
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                # Exponential backoff capped at 30s — long enough to
                # avoid spinning on a hard outage, short enough that
                # a transient blip recovers within one heartbeat.
                backoff = min(backoff * 2, 30.0)

    def _dispatch(self, raw_payload: str) -> None:
        """Decode + republish one NOTIFY payload onto the local bus.

        Self-originated NOTIFYs are dropped here (the publisher's
        own subscribers already fired). Decode failures are logged
        + dropped (see :func:`_decode_envelope`).
        """
        decoded = _decode_envelope(raw_payload)
        if decoded is None:
            return
        _kind, worker_id, event = decoded
        if worker_id == self._worker_id:
            return
        # ``publish_local`` skips the relay re-entry that
        # ``publish`` would otherwise trigger, breaking the loop.
        try:
            self._bus.publish_local(event)
        except Exception as exc:
            # A subscriber that raises on a relayed event would
            # otherwise tear down the receive loop. Log + drop —
            # the originating worker's subscribers already ran on
            # the publishing side, so this branch only handles
            # this worker's own subscribers refusing the payload.
            _log.warning(
                "events.relay: local subscriber raised on relayed %s: %s",
                type(event).name,
                exc,
                extra={
                    "event": "events.relay.subscriber_raised",
                    "kind": type(event).name,
                },
            )


def build_relay(*, engine: Engine, bus: EventBus, mode: str) -> EventRelay:
    """Construct the relay appropriate for ``mode`` and the active dialect.

    ``mode`` accepts:

    * ``"auto"`` (default in production) — pick by dialect: Postgres
      → :class:`PostgresListenNotifyRelay`; everything else (SQLite)
      → :class:`NullRelay`.
    * ``"in_process"`` — force :class:`NullRelay` regardless of
      dialect. The default in tests; an operator escape hatch for a
      single-worker Postgres deploy that wants to skip the LISTEN
      connection cost.
    * ``"postgres"`` — force :class:`PostgresListenNotifyRelay`.
      Raises :class:`ValueError` if the active dialect is not
      Postgres — the listener can't speak any other dialect's
      pub/sub primitives.

    The factory caller wires the returned relay into the bus via
    :meth:`EventBus.set_relay` and starts the listen loop from the
    lifespan.
    """
    normalised = mode.strip().lower()
    if normalised == "in_process":
        return NullRelay()
    if normalised == "postgres":
        if engine.dialect.name != "postgresql":
            raise ValueError(
                "events_relay='postgres' requires a PostgreSQL engine; got "
                f"dialect {engine.dialect.name!r}."
            )
        return PostgresListenNotifyRelay(engine=engine, bus=bus)
    if normalised == "auto":
        if engine.dialect.name == "postgresql":
            return PostgresListenNotifyRelay(engine=engine, bus=bus)
        return NullRelay()
    raise ValueError(
        f"Unknown events_relay mode {mode!r}; expected 'auto', "
        "'in_process', or 'postgres'."
    )
